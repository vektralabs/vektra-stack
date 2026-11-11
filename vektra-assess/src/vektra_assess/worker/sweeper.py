"""Sweeper tick (REQ-070): close what nobody is looking at (plan assess-session-lifecycle M3.5).

Every 10-15 s it closes ``timed_out`` each ACTIVE session whose deadline + reply grace has passed
on the database clock, through the plan-2 close function (state, reason, epoch bump, hand-off
delete, transcript job, audit, one transaction). It NEVER touches an OUTAGE session: its clock is
frozen, the outage probe ends it or closes it ``service_failure``. The API closes the same
sessions lazily on any request (M3.4); both paths go through the same idempotent function, so a
race between them closes once and enqueues one transcript job.
"""

from __future__ import annotations

import structlog
from sqlalchemy import text

from vektra_assess.worker.runner import AcquireDb

log = structlog.get_logger(__name__)

_DUE = text(
    "SELECT id, epoch FROM assess_sessions WHERE state = 'ACTIVE' AND deadline_at IS NOT NULL "
    "AND deadline_at + make_interval(secs => reply_grace_s) < now() "
    "ORDER BY deadline_at LIMIT :n"
)
#: the timeout re-checked under the row lock: a session that went into
#: OUTAGE, or got a later deadline from an outage credit, after the scan is left alone
_RECHECK = text(
    "SELECT epoch FROM assess_sessions WHERE id = :sid AND state = 'ACTIVE' "
    "AND deadline_at IS NOT NULL "
    "AND deadline_at + make_interval(secs => reply_grace_s) < now() FOR UPDATE"
)
_CLOSE = text("SELECT assess_close_session(:sid, :epoch, 'timed_out')")


async def sweep_timed_out(acquire: AcquireDb, *, batch: int = 200) -> int:
    async with acquire() as conn:
        due = (await conn.execute(_DUE, {"n": batch})).all()
    closed = 0
    for row in due:
        async with acquire() as conn:
            async with conn.begin():
                epoch = (await conn.execute(_RECHECK, {"sid": row.id})).scalar()
                ok = (
                    epoch is not None
                    and (
                        await conn.execute(_CLOSE, {"sid": row.id, "epoch": epoch})
                    ).scalar()
                )
        closed += bool(ok)
    if closed:
        log.info("assess_sessions_timed_out", count=closed)
    return closed
