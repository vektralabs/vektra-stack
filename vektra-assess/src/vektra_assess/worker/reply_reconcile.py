"""``reconcile_reply`` (REQ-070, REQ-086): answer a student turn whose reply was lost.

A student turn is pending (``pending_reply_since``) and no live reply holds the claim: the API
crashed mid-reply, or its persist failed (database or hand-off unavailable). The sweeper tick
queues one job per such turn older than the outage timeout + the reply grace; the handler, under
the KEK (it never needs the API's hand-off key):

1. locks the row and re-checks it (ACTIVE, the same pending turn, old enough);
2. takes the live-reply claim with ``pg_try_advisory_xact_lock``: held means a driver is still
   working on it, and the job is a no-op;
3. unwraps the session DEK, encrypts the fixed ``[risposta interrotta]`` turn and appends it
   through ``assess_append_turn`` (kind ``interrupted``, the same rules as the API: hash chain,
   seq, state), which clears the pending marker, so the student can write again.

Exactly one marked turn per lost reply: the append is chained on the last turn, and a second job
finds no pending student turn.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text

from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.crypto import envelope, hashing
from vektra_assess.crypto.kek import unwrap_dek
from vektra_assess.jobs import JobKind, enqueue_job
from vektra_assess.keys import AssessKeyring
from vektra_assess.turn_markers import (
    INTERRUPTED_REPLY,
    REPLY_CLAIM_KEY_SQL,
    reply_claimed_sql,
)
from vektra_assess.worker.runner import AcquireDb, JobContext

log = structlog.get_logger(__name__)

_ABANDONED = text(
    "SELECT id, extract(epoch FROM pending_reply_since)::bigint AS since FROM assess_sessions "
    "WHERE state = 'ACTIVE' AND pending_reply_since IS NOT NULL "
    "AND pending_reply_since < now() - make_interval(secs => :timeout + reply_grace_s) "
    # a live claim (a reply in flight in some API process) is skipped here, not at the job:
    # the batch then reaches the later abandoned turns
    "AND NOT " + reply_claimed_sql("id") + " "
    "ORDER BY pending_reply_since LIMIT :n"
)
_ROW = text(
    "SELECT state, epoch, seq, site_id, pending_reply_since, "
    "pending_reply_since < now() - make_interval(secs => :timeout + reply_grace_s) AS old "
    "FROM assess_sessions WHERE id = :sid FOR UPDATE"
)
_CLAIM = text(f"SELECT pg_try_advisory_xact_lock({REPLY_CLAIM_KEY_SQL})")
_KEY = text(
    "SELECT kek_wrapped, key_version FROM assess_session_keys WHERE session_id = :sid "
    "AND kek_wrapped IS NOT NULL"
)
_LAST = text(
    "SELECT turn_no, role, turn_hash FROM assess_turns WHERE session_id = :sid "
    "ORDER BY turn_no DESC LIMIT 1"
)
_APPEND = text(
    "SELECT assess_append_turn(:sid, :epoch, :seq, 'bot', 'interrupted', :ct, :h, :prev)"
)


async def queue_reconciles(
    acquire: AcquireDb, outage_timeout_s: float, *, batch: int = 100
) -> int:
    """Queue one ``reconcile_reply`` job per abandoned pending turn (deduped per turn)."""
    queued = 0
    async with acquire() as conn:
        async with conn.begin():
            rows = (
                await conn.execute(
                    _ABANDONED, {"timeout": outage_timeout_s, "n": batch}
                )
            ).all()
            for r in rows:
                queued += await enqueue_job(
                    conn,
                    JobKind.RECONCILE_REPLY,
                    {"session_id": str(r.id)},
                    dedupe_key=f"reconcile_reply:{r.id}:{r.since}",
                )
    return queued


async def reconcile_one(
    acquire: AcquireDb, kek: AssessKeyring, sid: UUID, outage_timeout_s: float
) -> bool:
    """Append the marked turn for ``sid`` if it is still abandoned; True when it did."""
    async with acquire() as conn:
        async with conn.begin():
            row = (
                await conn.execute(_ROW, {"sid": sid, "timeout": outage_timeout_s})
            ).first()
            if row is None or row.state != "ACTIVE" or not row.old:
                return False  # closed, answered or not old enough: a stale job
            if not (await conn.execute(_CLAIM, {"sid": str(sid)})).scalar():
                return False  # a live driver still holds the claim
            last = (await conn.execute(_LAST, {"sid": sid})).first()
            key = (await conn.execute(_KEY, {"sid": sid})).first()
            if last is None or last.role != "student" or key is None:
                return False
            dek = unwrap_dek(kek, bytes(key.kek_wrapped), sid, int(key.key_version))
            turn_no = int(last.turn_no) + 1
            plaintext = INTERRUPTED_REPLY.encode("utf-8")
            got = (
                await conn.execute(
                    _APPEND,
                    {
                        "sid": sid,
                        "epoch": row.epoch,
                        "seq": row.seq,
                        "ct": envelope.encrypt_turn(dek, sid, turn_no, plaintext),
                        "h": hashing.turn_hash(
                            last.turn_hash, turn_no, "bot", "interrupted", plaintext
                        ),
                        "prev": last.turn_hash,
                    },
                )
            ).scalar()
            del dek
            if got is None:
                return False
            await write_event(
                conn,
                AssessAuditEvent.REPLY_RECONCILED,
                actor="worker",
                site_id=row.site_id,
                session_id=sid,
                data={"turn_no": int(got)},
            )
    log.info("assess_reply_reconciled", session_id=str(sid))
    return True


def make_reconcile_handler(
    kek: AssessKeyring, outage_timeout_s: float
) -> Callable[[JobContext], Awaitable[None]]:
    async def handle(ctx: JobContext) -> None:
        try:
            sid = UUID(str(ctx.args.get("session_id")))
        except ValueError:
            return
        await reconcile_one(ctx.acquire, kek, sid, outage_timeout_s)

    return handle


__all__: list[Any] = ["make_reconcile_handler", "queue_reconciles", "reconcile_one"]
