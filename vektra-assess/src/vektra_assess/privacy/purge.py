"""The retention purge (plan assess-privacy-retention, ARCH-077 Flow 5, ARCH-079).

The daily ``purge`` tick (and the ``purge`` job, the same body on demand) removes the content of
every CLOSED session whose ``closed_at`` is older than its site's ``retention_days``:

1. select a batch (``BATCH`` sessions, ``FOR UPDATE SKIP LOCKED``) per configured site; a
   session with a delivery attempt still in flight (started within ``IN_FLIGHT``) waits for the
   next run, so a send is never cut from under the relay;
2. deliveries still PENDING / RETRYING are abandoned, each audited
   ``assess_delivery_abandoned_on_purge`` (the rows themselves go in step 3), and the activity's recipients get a content-free
   ``teacher_notify`` (``reason: delivery_abandoned_on_purge``: activity, student id, session
   number) queued in the same transaction;
3. crypto-shredding first: the wrapped session key and the wrapped transcript key are nulled,
   then the rows go (turns, their notes and flags, snapshots, hand-off remnants, transcripts,
   their deliveries and download tokens, outage and enrolment records, the session keys); the
   teacher's reason on the reset that followed the session is overwritten (``reason_kid =
   'purged'``, no bytes left: the grant itself stays, the START gate counts it);
4. count: every content table is counted again for the batch's ids and must be 0, else the
   transaction raises ``PurgeVerificationFailed`` and rolls back (no tombstone, no audit row
   claiming a deletion; the alert is a log line). Only then the tombstones (one per session,
   ids and counts) and the ``assess_purge`` audit row (counts only) are written, in the same
   transaction.

What stays: the session row itself (ids, numbers, timestamps, state: the metadata the START gate
and the teacher's list count, with no content) and the tombstone. "Purged" is never a status
field: the tests count rows, and so does step 4.

The same tick deletes notification rows (``teacher_notify`` / ``operator_notify``, the purge
notice included) in a final state and older than ``NOTIFY_RETENTION``, and download tokens a day
past their expiry. Done jobs have their own tick (``job_cleanup``).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.jobs import JobKind, enqueue_job
from vektra_assess.keys import RESERVED_KID
from vektra_assess.privacy import config as privacy_config
from vektra_assess.privacy.subject import subject_ref

log = structlog.get_logger(__name__)

Connect = Callable[[], AbstractAsyncContextManager[AsyncConnection]]

BATCH = 100
#: batches per site per run: a backlog is worked off over several days rather than in one
#: transaction-heavy run (the tick has a hard timeout)
MAX_BATCHES = 20
#: a delivery attempt started this recently may still be on the wire
IN_FLIGHT = timedelta(minutes=15)
#: a backlog run: this many batches per site per ``purge`` job (the maintenance budget)
JOB_BATCHES = 3
BACKLOG_DELAY = timedelta(minutes=5)
BACKLOG_KEY = "purge:backlog"
#: notification rows carry ids and codes only; kept this long for the operator, then deleted
NOTIFY_RETENTION = timedelta(days=30)
ABANDONED_NOTICE = "delivery_abandoned_on_purge"
PURGED_KID = RESERVED_KID  # refused as a real kid by load_keyring

#: per-session content tables, keyed on ``session_id`` (counted before and after)
SESSION_TABLES: tuple[str, ...] = (
    "assess_session_handoff",
    "assess_turn_notes",
    "assess_turn_flags",
    "assess_turns",
    "assess_snapshots",
    "assess_outage_incidents",
    "assess_enrolment_checks",
    "assess_session_keys",
)
#: tables keyed on the transcript id
TRANSCRIPT_TABLES: tuple[str, ...] = (
    "assess_transcript_download_tokens",
    "assess_deliveries",
)


class PurgeVerificationFailed(RuntimeError):
    """Rows were left after the deletion: the transaction is rolled back."""


@dataclass
class Shredded:
    """Per-table deleted counts (and the transcripts of the sessions)."""

    deleted: dict[str, int] = field(default_factory=dict)
    transcript_ids: list[UUID] = field(default_factory=list)
    #: session id -> its transcript id, for the tombstones
    transcripts: dict[UUID, UUID] = field(default_factory=dict)
    abandoned: int = 0

    def add(self, table: str, count: int) -> None:
        self.deleted[table] = self.deleted.get(table, 0) + max(count, 0)


@dataclass
class PurgeReport:
    sessions: int = 0
    deleted: dict[str, int] = field(default_factory=dict)
    abandoned: int = 0
    notify_rows: int = 0
    tokens: int = 0
    stale_erasures: int = 0
    #: a site reached the batch cap: a follow-up ``purge`` job was queued
    backlog: bool = False

    def merge(self, s: Shredded, sessions: int) -> None:
        self.sessions += sessions
        self.abandoned += s.abandoned
        for table, n in s.deleted.items():
            self.deleted[table] = self.deleted.get(table, 0) + n


# --- the shared shredding step ----------------------------------------------------------------

_TRANSCRIPTS_OF = text(
    "SELECT id, session_id FROM assess_transcripts WHERE session_id = ANY(:s) ORDER BY id"
)
_NULL_KEK = text(
    "UPDATE assess_session_keys SET kek_wrapped = NULL, shredded_at = now() "
    "WHERE session_id = ANY(:s)"
)
_NULL_DEK = text(
    "UPDATE assess_transcripts SET dek_wrapped = NULL, ciphertext = NULL, shredded_at = now() "
    "WHERE id = ANY(:t)"
)
_LIVE_DELIVERIES = text(
    "SELECT d.id, d.kind, s.site_id, s.cmid, s.student_userid, s.session_no, s.id AS sid "
    "FROM assess_deliveries d JOIN assess_transcripts t ON t.id = d.transcript_id "
    "JOIN assess_sessions s ON s.id = t.session_id "
    "WHERE d.transcript_id = ANY(:t) AND d.status IN ('PENDING', 'RETRYING') ORDER BY d.id"
)
_REASONS_OUT = text(
    "UPDATE assess_reset_grants g SET reason_ct = ''::bytea, reason_kid = :kid "
    "FROM assess_sessions s WHERE s.id = ANY(:s) AND g.site_id = s.site_id "
    "AND g.cmid = s.cmid AND g.student_userid = s.student_userid "
    "AND g.session_no = s.session_no AND g.reason_kid <> :kid"
)
_REASONS_LEFT = text(
    "SELECT count(*) FROM assess_reset_grants g JOIN assess_sessions s ON s.id = ANY(:s) "
    "AND g.site_id = s.site_id AND g.cmid = s.cmid AND g.student_userid = s.student_userid "
    "AND g.session_no = s.session_no WHERE g.reason_kid <> :kid OR length(g.reason_ct) > 0"
)


async def abandon_live_deliveries(
    conn: AsyncConnection, transcript_ids: Sequence[UUID], *, why: str, notify: bool
) -> int:
    """PENDING / RETRYING copies of these transcripts -> ABANDONED, each audited; with
    ``notify`` one content-free notice per session to the activity's recipients."""
    if not transcript_ids:
        return 0
    rows = (await conn.execute(_LIVE_DELIVERIES, {"t": list(transcript_ids)})).all()
    if not rows:
        return 0
    # no status UPDATE: these rows are deleted a few statements later in this transaction, so
    # the abandonment is recorded where it survives, in the audit (and the notice)
    notified: set[UUID] = set()
    for r in rows:
        await write_event(
            conn,
            AssessAuditEvent.DELIVERY_ABANDONED_ON_PURGE,
            actor="worker",
            site_id=r.site_id,
            session_id=r.sid,
            reason_code=why,
            data={"delivery_id": r.id, "kind": r.kind},
        )
        if notify and r.sid not in notified:
            notified.add(r.sid)
            await enqueue_job(
                conn,
                JobKind.TEACHER_NOTIFY,
                {
                    "site_id": str(r.site_id),
                    "cmid": int(r.cmid),
                    "student_userid": int(r.student_userid),
                    "session_no": int(r.session_no),
                    "reason": ABANDONED_NOTICE,
                },
                dedupe_key=f"teacher_notify:{ABANDONED_NOTICE}:{r.sid}",
            )
    return len(rows)


async def shred_sessions(
    conn: AsyncConnection,
    session_ids: Sequence[UUID],
    *,
    abandon_why: str,
    notify: bool,
) -> Shredded:
    """Steps 2 and 3 for these sessions, in the caller's transaction (keys first, rows second).
    The session rows themselves are left to the caller (the retention purge keeps them, the
    erasure deletes them)."""
    out = Shredded()
    sids = list(session_ids)
    if not sids:
        return out
    rows = (await conn.execute(_TRANSCRIPTS_OF, {"s": sids})).all()
    out.transcripts = {r.session_id: r.id for r in rows}
    tids = [r.id for r in rows]
    out.transcript_ids = tids
    out.abandoned = await abandon_live_deliveries(
        conn, tids, why=abandon_why, notify=notify
    )
    # crypto-shredding: nothing that could decrypt survives the statement, then the rows go
    await conn.execute(_NULL_KEK, {"s": sids})
    if tids:
        await conn.execute(_NULL_DEK, {"t": tids})
        for table in TRANSCRIPT_TABLES:
            res = await conn.execute(
                text(f"DELETE FROM {table} WHERE transcript_id = ANY(:t)"), {"t": tids}
            )
            out.add(table, res.rowcount)
        res = await conn.execute(
            text("DELETE FROM assess_transcripts WHERE id = ANY(:t)"), {"t": tids}
        )
        out.add("assess_transcripts", res.rowcount)
    for table in SESSION_TABLES:
        res = await conn.execute(
            text(f"DELETE FROM {table} WHERE session_id = ANY(:s)"), {"s": sids}
        )
        out.add(table, res.rowcount)
    res = await conn.execute(_REASONS_OUT, {"s": sids, "kid": PURGED_KID})
    out.add("assess_reset_grants.reason", res.rowcount)
    return out


async def leftover(
    conn: AsyncConnection, session_ids: Sequence[UUID], transcript_ids: Sequence[UUID]
) -> dict[str, int]:
    """Rows of the content tables still present for these ids (step 4). Empty dict: none."""
    sids, tids = list(session_ids), list(transcript_ids)
    left: dict[str, int] = {}
    for table in SESSION_TABLES:
        n = (
            await conn.execute(
                text(f"SELECT count(*) FROM {table} WHERE session_id = ANY(:s)"),
                {"s": sids},
            )
        ).scalar_one()
        if n:
            left[table] = int(n)
    n = (
        await conn.execute(
            text(
                "SELECT count(*) FROM assess_transcripts WHERE session_id = ANY(:s) "
                "OR id = ANY(:t)"
            ),
            {"s": sids, "t": tids},
        )
    ).scalar_one()
    if n:
        left["assess_transcripts"] = int(n)
    for table in TRANSCRIPT_TABLES:
        n = (
            await conn.execute(
                text(f"SELECT count(*) FROM {table} WHERE transcript_id = ANY(:t)"),
                {"t": tids},
            )
        ).scalar_one()
        if n:
            left[table] = int(n)
    n = (await conn.execute(_REASONS_LEFT, {"s": sids, "kid": PURGED_KID})).scalar_one()
    if n:
        left["assess_reset_grants.reason"] = int(n)
    return left


_TOMBSTONE = text(
    "INSERT INTO assess_purge_records (site_id, subject_ref, session_id, transcript_id, "
    "session_no, cmid, reason, counts) VALUES (:site, :ref, :sid, :tid, :no, :cmid, :why, "
    "CAST(:counts AS jsonb)) ON CONFLICT (session_id) DO NOTHING"
)


async def write_tombstones(
    conn: AsyncConnection,
    sessions: Sequence[Any],
    transcripts: dict[UUID, UUID],
    *,
    reason: str,
    counts: dict[str, int],
    subject: str | None = None,
) -> None:
    """One tombstone per session (ids and the batch's counts); an existing one is kept.
    ``subject`` replaces the subject reference (the erasure writes ``ERASED_REF``)."""
    payload = json.dumps(counts, sort_keys=True)
    for s in sessions:
        await conn.execute(
            _TOMBSTONE,
            {
                "site": s.site_id,
                "ref": subject
                if subject is not None
                else subject_ref(s.site_id, int(s.student_userid)),
                "sid": s.id,
                "tid": transcripts.get(s.id),
                "no": int(s.session_no),
                "cmid": int(s.cmid),
                "why": reason,
                "counts": payload,
            },
        )


# --- the retention purge ----------------------------------------------------------------------

#: every stored settings row; only those that pass the same validation as ``is_configured``
#: authorize a purge (a row the application calls unconfigured never deletes anything)
_SITES = text(
    "SELECT site_id, retention_days, legal_basis_label, legal_basis_text, notice_url, "
    "notice_text, notice_version FROM assess_privacy_config ORDER BY site_id"
)
#: CLOSED past retention, not purged yet, no delivery attempt on the wire
_DUE = text(
    "SELECT s.id, s.site_id, s.cmid, s.student_userid, s.session_no FROM assess_sessions s "
    "WHERE s.site_id = :site AND s.state = 'CLOSED' AND s.closed_at IS NOT NULL "
    "AND s.closed_at < now() - make_interval(days => :days) "
    "AND NOT EXISTS (SELECT 1 FROM assess_purge_records p WHERE p.session_id = s.id) "
    "AND NOT EXISTS (SELECT 1 FROM assess_transcripts t JOIN assess_deliveries d "
    "  ON d.transcript_id = t.id WHERE t.session_id = s.id "
    "  AND d.attempt_started_at > now() - make_interval(secs => :flight)) "
    "ORDER BY s.closed_at, s.id LIMIT :n FOR UPDATE OF s SKIP LOCKED"
)
_NOTIFY_ROWS = text(
    "DELETE FROM assess_deliveries WHERE kind IN ('teacher_notify', 'operator_notify') "
    "AND status IN ('SENT', 'FAILED', 'ABANDONED', 'REPLACED') "
    "AND created_at < now() - make_interval(days => :days)"
)
_STALE_ERASURES = text(
    "UPDATE assess_erasure_records SET completed_at = now(), outcome = 'failed', "
    "error_code = 'abandoned', subject_userids = NULL, subject_userid_hash = NULL "
    "WHERE completed_at IS NULL AND requested_at < now() - interval '1 day'"
)
_OLD_TOKENS = text(
    "DELETE FROM assess_transcript_download_tokens WHERE expires_at < now() - interval '1 day'"
)


async def purge_batch(
    conn: AsyncConnection, site_id: UUID, days: int
) -> tuple[int, Shredded]:
    """One batch of one site, in the caller's transaction. Raises
    ``PurgeVerificationFailed`` (the caller's transaction must roll back) when a row is left."""
    due = (
        await conn.execute(
            _DUE,
            {
                "site": site_id,
                "days": int(days),
                "flight": IN_FLIGHT.total_seconds(),
                "n": BATCH,
            },
        )
    ).all()
    if not due:
        return 0, Shredded()
    sids = [r.id for r in due]
    shredded = await shred_sessions(
        conn, sids, abandon_why="retention_expired", notify=True
    )
    left = await leftover(conn, sids, shredded.transcript_ids)
    if left:
        raise PurgeVerificationFailed(json.dumps(left, sort_keys=True))
    await write_tombstones(
        conn, due, shredded.transcripts, reason="retention", counts=shredded.deleted
    )
    await write_event(
        conn,
        AssessAuditEvent.PURGE,
        actor="worker",
        site_id=site_id,
        reason_code="retention",
        data={
            "sessions": len(due),
            "transcripts": shredded.deleted.get("assess_transcripts", 0),
            "turns": shredded.deleted.get("assess_turns", 0),
            "deliveries": shredded.deleted.get("assess_deliveries", 0),
            "abandoned": shredded.abandoned,
            "retention_days": int(days),
        },
    )
    return len(due), shredded


async def run_purge(
    connect: Connect, *, max_batches: int = MAX_BATCHES, generation: str = "tick"
) -> PurgeReport:
    """The daily purge: every configured site, batch by batch, each batch its own transaction.

    A batch that fails its count rolls back alone (alert ``purge_verification_failed``) and the
    run stops for that site; the other sites still run, and the run raises at the end so the
    tick records a failure (a purge that left rows never reads as a success)."""
    report = PurgeReport()
    async with connect() as conn, conn.begin():
        sites = [
            r for r in (await conn.execute(_SITES)).all() if privacy_config.passes(r)
        ]
    failed: list[str] = []
    for site in sites:
        for batch_no in range(max_batches):
            try:
                async with connect() as conn, conn.begin():
                    n, shredded = await purge_batch(
                        conn, site.site_id, int(site.retention_days)
                    )
            except PurgeVerificationFailed as exc:
                log.error(
                    "assess.purge.alert",
                    alert="purge_verification_failed",
                    site_id=str(site.site_id),
                    left=str(exc)[:200],
                )
                failed.append(str(site.site_id))
                break
            report.merge(shredded, n)
            if n < BATCH:
                break
            if batch_no == max_batches - 1:
                report.backlog = True  # more is due than one run may take
    async with connect() as conn, conn.begin():
        res = await conn.execute(_NOTIFY_ROWS, {"days": NOTIFY_RETENTION.days})
        report.notify_rows = max(res.rowcount, 0)
        res = await conn.execute(_OLD_TOKENS)
        report.tokens = max(res.rowcount, 0)
        # an erasure request whose job died never completes: after a day it is recorded failed
        # and the ids it carried are dropped (the plugin's retry makes a new request)
        res = await conn.execute(_STALE_ERASURES)
        report.stale_erasures = max(res.rowcount, 0)
        if report.backlog:
            # a site with more due than a run's cap: another bounded run soon, as a ``purge``
            # job, instead of letting content outlive its retention until tomorrow
            # (CodeRabbit outside-diff on run_purge)
            await enqueue_job(
                conn,
                JobKind.PURGE,
                {},
                # one key per generation: a follow-up queued by the ``purge`` job that is running
                # (leased, holding its own key) must not collide with it (CodeRabbit 4517939449)
                dedupe_key=f"{BACKLOG_KEY}:{generation}",
                not_before=datetime.now(UTC) + BACKLOG_DELAY,
            )
    log.info(
        "assess.purge.done",
        sessions=report.sessions,
        abandoned=report.abandoned,
        notify_rows=report.notify_rows,
        tokens=report.tokens,
        failed_sites=len(failed),
    )
    if failed:
        raise PurgeVerificationFailed(f"{len(failed)} site(s) left rows")
    return report


def make_purge_handler(connect: Connect) -> Callable[[Any], Any]:
    """The ``purge`` job: a bounded run on demand or for a backlog (no arguments). It runs
    fewer batches than the daily tick, to fit the maintenance budget, and queues the next run
    itself while a backlog remains."""

    async def handle(ctx: Any) -> None:
        await run_purge(connect, max_batches=JOB_BATCHES, generation=str(ctx.job_id))

    return handle
