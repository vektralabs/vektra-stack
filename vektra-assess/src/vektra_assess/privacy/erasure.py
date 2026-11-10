"""Subject and activity erasure (plan assess-privacy-retention, OQ-033 default, ARCH-077).

The API never deletes: ``DELETE /privacy/subjects/{userid}`` and ``DELETE
/privacy/activities/{cmid}/subjects`` insert an ``assess_erasure_records`` request and queue an
``erasure`` job; this worker handler completes it, and the API answers from the completed
record (or 503 ``pending`` when the worker has not finished: the same request again is
idempotent). The mode switch (``VEKTRA_ASSESS_ERASURE_MODE=retain_exam_evidence``) is decided
at the API, which then queues nothing.

First, in its own transaction, every live session of the scope (ACTIVE / OUTAGE) is closed
through the single close path (``assess_close_session``, reason ``access_revoked``: epoch bump,
hand-off delete, audited). It must commit before the deletion: the REQ-067 constraint trigger
checks a CLOSED session's opening turn at COMMIT, from the row the close wrote, so a close and
the deletion of that turn cannot share a transaction. A STARTING session is removed with the
rest (its pre-flight finds nothing and completes). Then, in ONE transaction:

1. (the live sessions are closed already);
2. the deliveries still PENDING / RETRYING become ABANDONED, audited (no notice is sent);
3. the content is crypto-shredded then deleted as in the retention purge, a tombstone per
   session is written (reason ``erasure``; an earlier retention tombstone is kept), and the
   subject's own rows go: the sessions, the Start attempts and minted tokens, the worker's
   per-student caches (gate inputs, render state and checks), extra time and resets;
4. every table is counted again for the scope and must hold 0 rows, else the transaction rolls
   back and the request is recorded ``failed`` (alert ``erasure_verification_failed``);
5. the record is completed with its counts and the user ids it carried are cleared.

Teacher-entered recipient addresses are not a subject's data in this model (REQ-082 as amended:
the teacher types them, nothing maps a student id to an address), so nothing is removed from
the sealed configuration. Audit rows stay (content-free, append-only: REQ-085); they name the
session ids that no longer resolve to a subject.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.privacy.purge import (
    IN_FLIGHT,
    Connect,
    leftover,
    shred_sessions,
    write_tombstones,
)
from vektra_assess.privacy.subject import ERASED_REF, SUBJECT_TABLES, subject_ref

log = structlog.get_logger(__name__)


class ErasureVerificationFailed(RuntimeError):
    """Rows were left after the deletion: the transaction is rolled back."""


class SessionWentLive(RuntimeError):
    """A session of the scope was live at the deletion, or work on it was in flight (a leased
    job, a send): the transaction rolls back and the job runner retries the erasure."""


async def close_live(
    conn: AsyncConnection, site_id: UUID, *, cmid: int | None, userids: list[int] | None
) -> int:
    """Close the scope's ACTIVE / OUTAGE sessions (``access_revoked``), in the caller's
    transaction, which must COMMIT before the deletion (see the module docstring)."""
    params = {"site": site_id, "cmid": cmid, "users": userids}
    live = (
        await conn.execute(
            text(
                f"SELECT s.id, s.epoch FROM assess_sessions s WHERE {_scope_where('s', cmid, userids)} "
                "AND s.state IN ('ACTIVE', 'OUTAGE') ORDER BY s.id FOR UPDATE"
            ),
            params,
        )
    ).all()
    for s in live:
        await conn.execute(_CLOSE, {"sid": s.id, "epoch": int(s.epoch)})
    return len(live)


@dataclass
class ErasureResult:
    sessions: int = 0
    transcripts: int = 0
    deliveries_abandoned: int = 0
    deleted: dict[str, int] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {
            "sessions": self.sessions,
            "transcripts": self.transcripts,
            "deliveries_abandoned": self.deliveries_abandoned,
        }


_RECORD = text(
    "SELECT id, site_id, scope, cmid, subject_userids, completed_at FROM "
    "assess_erasure_records WHERE id = :i FOR UPDATE"
)
#: completion clears what named the subject: the ids and their hash
_COMPLETE = text(
    "UPDATE assess_erasure_records SET completed_at = now(), outcome = :o, error_code = :e, "
    "rows_deleted = CAST(:counts AS jsonb), subject_userids = NULL, subject_userid_hash = NULL "
    "WHERE id = :i"
)
#: a transcript copy whose attempt started recently and has no outcome yet: maybe on the wire
_SENDING = text(
    "SELECT count(*) FROM assess_deliveries d JOIN assess_transcripts t "
    "ON t.id = d.transcript_id WHERE t.session_id = ANY(:s) AND d.sent_at IS NULL "
    "AND d.status IN ('PENDING', 'RETRYING') "
    "AND d.attempt_started_at > now() - make_interval(secs => :flight)"
)
_CLOSE = text("SELECT assess_close_session(:sid, :epoch, 'access_revoked')")


def _scope_where(alias: str, cmid: int | None, userids: list[int] | None) -> str:
    """The scope predicate on a table with (site_id, cmid, student_userid)."""
    where = f"{alias}.site_id = :site"
    if cmid is not None:
        where += f" AND {alias}.cmid = :cmid"
    if userids is not None:
        where += f" AND {alias}.student_userid = ANY(:users)"
    return where


async def erase_scope(
    conn: AsyncConnection,
    site_id: UUID,
    *,
    cmid: int | None,
    userids: list[int] | None,
) -> ErasureResult:
    """Steps 1-4 for one scope, in the caller's transaction. ``userids=None`` with a ``cmid``:
    every subject of that activity. Raises ``ErasureVerificationFailed`` when a row is left."""
    if cmid is None and not userids:
        raise ValueError("an erasure names a subject or an activity")
    params = {"site": site_id, "cmid": cmid, "users": userids}
    where = _scope_where("s", cmid, userids)
    sessions = (
        await conn.execute(
            text(
                "SELECT s.id, s.site_id, s.cmid, s.student_userid, s.session_no, s.state, "
                f"s.epoch FROM assess_sessions s WHERE {where} ORDER BY s.id FOR UPDATE"
            ),
            params,
        )
    ).all()
    result = ErasureResult(sessions=len(sessions))
    if any(s.state in ("ACTIVE", "OUTAGE") for s in sessions):
        # a session went live after ``close_live`` committed: the next run of the job erases
        # it (raising here rolls this transaction back and the job is retried)
        raise SessionWentLive("a live session appeared in the scope")
    sids = [s.id for s in sessions]
    # work in flight on the scope would outlive the erasure: a leased job (a render check can
    # write the student's caches back) or a transcript copy on the wire (its bytes are already
    # decrypted). The erasure waits for them: raising rolls back and the job is retried
    jp = _jobs_params(site_id, cmid, userids, sids)
    busy = (
        await conn.execute(
            text(
                "SELECT count(*) FROM assess_jobs WHERE "
                f"{_jobs_where(cmid, userids, leased=True)}"
            ),
            jp,
        )
    ).scalar_one()
    sending = (
        await conn.execute(_SENDING, {"s": sids, "flight": IN_FLIGHT.total_seconds()})
    ).scalar_one()
    if busy or sending:
        raise SessionWentLive(
            f"work in flight on the scope: {busy} job(s), {sending} send(s)"
        )
    shredded = await shred_sessions(
        conn, sids, abandon_why="subject_erased", notify=False
    )
    result.transcripts = shredded.deleted.get("assess_transcripts", 0)
    result.deliveries_abandoned = shredded.abandoned
    result.deleted = dict(shredded.deleted)
    left = await leftover(conn, sids, shredded.transcript_ids)
    # the tombstones of an erased subject lead back to nobody: theirs and any earlier
    # retention tombstone lose the subject reference (a hash of small integers is no secret)
    await write_tombstones(
        conn,
        sessions,
        shredded.transcripts,
        reason="erasure",
        counts=shredded.deleted,
        subject=ERASED_REF,
    )
    await _forget_tombstones(conn, site_id, cmid=cmid, userids=userids, sessions=sids)
    result.deleted["assess_jobs"] = await _drop_pending_jobs(
        conn, site_id, cmid=cmid, userids=userids, sessions=sids
    )
    for table in SUBJECT_TABLES:
        res = await conn.execute(
            text(f"DELETE FROM {table} t WHERE {_scope_where('t', cmid, userids)}"),
            params,
        )
        result.deleted[table] = result.deleted.get(table, 0) + max(res.rowcount, 0)
    for table in SUBJECT_TABLES:
        n = (
            await conn.execute(
                text(
                    f"SELECT count(*) FROM {table} t WHERE {_scope_where('t', cmid, userids)}"
                ),
                params,
            )
        ).scalar_one()
        if n:
            left[table] = int(n)
    jobs = await _pending_jobs(conn, site_id, cmid=cmid, userids=userids, sessions=sids)
    if jobs:
        left["assess_jobs"] = jobs
    if left:
        raise ErasureVerificationFailed(json.dumps(left, sort_keys=True))
    return result


def _jobs_where(
    cmid: int | None, userids: list[int] | None, *, leased: bool = False
) -> str:
    """Jobs of the scope: a per-student job of the site (render check, gate refresh, notice) or
    a job of one of its sessions. Not leased (``leased=False``): every one that still names the
    subject, ready or finished (done, failed, dead: their arguments and dedupe keys carry the
    ids), deleted with the rows. Leased: a run in flight that could write rows back after the
    erasure commits; the erasure waits for it (CodeRabbit 4516399770 / 4516399779)."""
    student = "args->>'site_id' = :site_text AND args ? 'student_userid'"
    if cmid is not None:
        student += " AND args->>'cmid' = :cmid_text"
    if userids is not None:
        student += " AND args->>'student_userid' = ANY(:users_text)"
    return (
        f"state {'=' if leased else '<>'} 'leased' AND (({student}) "
        "OR args->>'session_id' = ANY(:sids_text) "
        "OR args->>'transcript_id' = ANY(:sids_text))"
    )


def _jobs_params(
    site_id: UUID, cmid: int | None, userids: list[int] | None, sessions: list[UUID]
) -> dict[str, Any]:
    return {
        "site_text": str(site_id),
        "cmid_text": str(cmid) if cmid is not None else None,
        "users_text": [str(u) for u in userids] if userids is not None else None,
        "sids_text": [str(s) for s in sessions],
    }


async def _drop_pending_jobs(
    conn: AsyncConnection,
    site_id: UUID,
    *,
    cmid: int | None,
    userids: list[int] | None,
    sessions: list[UUID],
) -> int:
    res = await conn.execute(
        text(f"DELETE FROM assess_jobs WHERE {_jobs_where(cmid, userids)}"),
        _jobs_params(site_id, cmid, userids, sessions),
    )
    return max(res.rowcount, 0)


async def _pending_jobs(
    conn: AsyncConnection,
    site_id: UUID,
    *,
    cmid: int | None,
    userids: list[int] | None,
    sessions: list[UUID],
) -> int:
    n = (
        await conn.execute(
            text(
                f"SELECT count(*) FROM assess_jobs WHERE {_jobs_where(cmid, userids)}"
            ),
            _jobs_params(site_id, cmid, userids, sessions),
        )
    ).scalar_one()
    return int(n)


async def _forget_tombstones(
    conn: AsyncConnection,
    site_id: UUID,
    *,
    cmid: int | None,
    userids: list[int] | None,
    sessions: list[UUID],
) -> None:
    """Earlier tombstones of the scope (a retention purge) keep ids and counts, never whose."""
    where = "site_id = :site AND (session_id = ANY(:sids)"
    params: dict[str, Any] = {"site": site_id, "sids": sessions, "erased": ERASED_REF}
    if userids is not None:
        where += " OR subject_ref = ANY(:refs)"
        params["refs"] = [subject_ref(site_id, u) for u in userids]
        if cmid is not None:
            where = (
                where.replace("OR subject_ref", "OR (cmid = :cmid AND subject_ref")
                + ")"
            )
            params["cmid"] = cmid
    elif cmid is not None:
        where += " OR cmid = :cmid"
        params["cmid"] = cmid
    where += ")"
    await conn.execute(
        text(
            f"UPDATE assess_purge_records SET subject_ref = :erased WHERE {where} "
            "AND subject_ref <> :erased"
        ),
        params,
    )


async def run_erasure(connect: Connect, erasure_id: int) -> str:
    """Complete one queued request. Returns ``erased``, ``failed`` or ``done`` (already
    complete: a retried job)."""
    async with connect() as conn, conn.begin():
        rec = (await conn.execute(_RECORD, {"i": erasure_id})).first()
        if rec is None or rec.completed_at is not None:
            return "done"
        users = list(rec.subject_userids) if rec.subject_userids else None
        cmid = int(rec.cmid) if rec.cmid is not None else None
        if not (rec.scope == "subject" and not users):
            await close_live(conn, rec.site_id, cmid=cmid, userids=users)
    try:
        async with connect() as conn, conn.begin():
            rec = (await conn.execute(_RECORD, {"i": erasure_id})).first()
            if rec is None or rec.completed_at is not None:
                return "done"
            if rec.scope == "subject" and not users:
                raise ErasureVerificationFailed("subject request without its user id")
            result = await erase_scope(
                conn,
                rec.site_id,
                cmid=int(rec.cmid) if rec.cmid is not None else None,
                userids=users,
            )
            counts = {**result.counts(), **result.deleted}
            await conn.execute(
                _COMPLETE,
                {
                    "i": erasure_id,
                    "o": "erased",
                    "e": None,
                    "counts": json.dumps(counts, sort_keys=True),
                },
            )
            await write_event(
                conn,
                AssessAuditEvent.ERASURE,
                actor="worker",
                site_id=rec.site_id,
                reason_code=str(rec.scope),
                data={
                    "erasure_id": int(erasure_id),
                    "cmid": int(rec.cmid) if rec.cmid is not None else None,
                    **result.counts(),
                },
            )
            return "erased"
    except ErasureVerificationFailed as exc:
        log.error(
            "assess.erasure.alert",
            alert="erasure_verification_failed",
            erasure_id=int(erasure_id),
            left=str(exc)[:200],
        )
    # the deletion rolled back: record the failure (and drop the ids it carried) on its own
    async with connect() as conn, conn.begin():
        await conn.execute(
            _COMPLETE,
            {
                "i": erasure_id,
                "o": "failed",
                "e": "verification_failed",
                "counts": "{}",
            },
        )
    return "failed"


def make_erasure_handler(connect: Connect) -> Callable[[Any], Any]:
    """The ``erasure`` job: ``{erasure_id}``."""

    async def handle(ctx: Any) -> None:
        raw = ctx.args.get("erasure_id")
        if not isinstance(raw, int) or isinstance(raw, bool):
            ctx.log.warning("assess_erasure_job_malformed")
            return
        await run_erasure(connect, raw)

    return handle
