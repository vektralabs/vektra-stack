"""STARTING reaper tick (REQ-071, ARCH-068): a Start that never committed is rolled back.

A STARTING row whose window T (``created_at + VEKTRA_ASSESS_START_T_S``, database clock) has passed
and whose ``preflight`` job holds no live lease belongs to a pre-flight that died (worker killed,
handler cancelled past its budget, job never claimed). The reaper rolls it back with
``start_timeout`` through the same ``rollback_start`` the handler uses, so the row counts are
verified the same way.

A live lease means a handler may still be running: it is left alone (the handler's own commit is
guarded by T, so it cannot commit late). The race with a handler that commits at the last moment is
closed by the row lock: the commit transaction holds ``FOR UPDATE`` on the session row and the
rollback's ``DELETE ... WHERE state = 'STARTING'`` waits for it, then finds an ACTIVE row and deletes
nothing.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy import text

from vektra_assess.worker.preflight import Reason, rollback_start
from vektra_assess.worker.runner import AcquireDb
from vektra_assess.worker.start_guard import StartLimits

log = structlog.get_logger(__name__)

_EXPIRED = text(
    "SELECT s.id FROM assess_sessions s "
    "WHERE s.state = 'STARTING' AND s.created_at < now() - make_interval(secs => :t) "
    "AND NOT EXISTS (SELECT 1 FROM assess_jobs j WHERE j.kind = 'preflight' "
    "AND j.args->>'session_id' = s.id::text AND j.state = 'leased' AND j.lease_until > now()) "
    "ORDER BY s.created_at LIMIT :n"
)


async def reap_starting(
    acquire: AcquireDb,
    start_t_s: int,
    *,
    batch: int = 100,
    limits: StartLimits | None = None,
) -> int:
    """Roll back every expired, unleased STARTING session (one transaction each)."""
    async with acquire() as conn:
        ids: list[UUID] = [
            r.id
            for r in (await conn.execute(_EXPIRED, {"t": start_t_s, "n": batch})).all()
        ]
    reaped = 0
    for sid in ids:
        async with acquire() as conn:
            async with conn.begin():
                reaped += await rollback_start(
                    conn, sid, Reason.START_TIMEOUT, limits=limits
                )
    if reaped:
        log.info("assess_starting_reaped", count=reaped)
    return reaped
