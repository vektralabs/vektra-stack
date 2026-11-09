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
    Connect,
    leftover,
    shred_sessions,
    write_tombstones,
)

log = structlog.get_logger(__name__)

#: per-subject rows keyed (site_id, [cmid], student_userid), deleted and counted
SUBJECT_TABLES: tuple[str, ...] = (
    "assess_start_attempts",
    "assess_token_jti",
    "assess_gate_inputs",
    "assess_render_state",
    "assess_render_checks",
    "assess_extra_time",
    "assess_reset_grants",
    "assess_sessions",
)


class ErasureVerificationFailed(RuntimeError):
    """Rows were left after the deletion: the transaction is rolled back."""


class SessionWentLive(RuntimeError):
    """A session of the scope was live at the deletion: retried by the job runner."""


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
_COMPLETE = text(
    "UPDATE assess_erasure_records SET completed_at = now(), outcome = :o, error_code = :e, "
    "rows_deleted = CAST(:counts AS jsonb), subject_userids = NULL WHERE id = :i"
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
    transcripts = {
        r.session_id: r.id
        for r in (
            await conn.execute(
                text(
                    "SELECT id, session_id FROM assess_transcripts WHERE session_id = ANY(:s)"
                ),
                {"s": sids},
            )
        ).all()
    }
    shredded = await shred_sessions(
        conn, sids, abandon_why="subject_erased", notify=False
    )
    result.transcripts = shredded.deleted.get("assess_transcripts", 0)
    result.deliveries_abandoned = shredded.abandoned
    result.deleted = dict(shredded.deleted)
    left = await leftover(conn, sids, shredded.transcript_ids)
    await write_tombstones(
        conn, sessions, transcripts, reason="erasure", counts=shredded.deleted
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
    if left:
        raise ErasureVerificationFailed(json.dumps(left, sort_keys=True))
    return result


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
