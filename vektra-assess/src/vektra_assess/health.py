"""Assess readiness check for /health/assess (plan assess-foundations 8.2, data-crypto 8.1).

Registered as ``("health", "assess")`` by the API's startup step 12 whether assess is
enabled or not, so ``GET /health/assess`` always answers. It is excluded from the shallow
``/health`` (``vektra_admin.health.SHALLOW_EXCLUDED``): the worker is another container.

When enabled it never reports ``healthy`` without evidence. The evidence is the worker's
row in ``assess_heartbeat``, read over the API role (which has SELECT on it) and aged by
the database clock, so container clock skew cannot make a dead worker look alive:

- no engine wired            -> degraded, ``heartbeat_source_missing``
- no row                     -> degraded, ``no_heartbeat``
- older than ``max_age_s``   -> degraded, ``heartbeat_stale``
- database unreachable       -> unhealthy, ``heartbeat_unreadable``
- otherwise                  -> healthy

Owning plan: .s2s/plans/20261101-154950-assess-data-crypto.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text

from vektra_shared.types import HealthStatus

DISABLED = "disabled"
HEARTBEAT_SOURCE_MISSING = "heartbeat_source_missing"
NO_HEARTBEAT = "no_heartbeat"
HEARTBEAT_STALE = "heartbeat_stale"
HEARTBEAT_UNREADABLE = "heartbeat_unreadable"
WORKER_COMPONENT = "worker"

_AGE_SQL = text(
    "SELECT extract(epoch FROM now() - beat_at) FROM assess_heartbeat "
    "WHERE component = :c"
)


def make_readiness_check(
    *,
    enabled: bool,
    get_engine: Callable[[], Any | None] | None = None,
    max_age_s: int = 60,
) -> Callable[[], Awaitable[HealthStatus]]:
    async def assess_readiness() -> HealthStatus:
        if not enabled:
            return HealthStatus(status=DISABLED, message="VEKTRA_ASSESS_ENABLED=false")
        engine = get_engine() if get_engine is not None else None
        if engine is None:
            return HealthStatus(status="degraded", message=HEARTBEAT_SOURCE_MISSING)
        try:
            async with engine.connect() as conn:
                age = (await conn.execute(_AGE_SQL, {"c": WORKER_COMPONENT})).scalar()
        except Exception:
            return HealthStatus(status="unhealthy", message=HEARTBEAT_UNREADABLE)
        if age is None:
            return HealthStatus(status="degraded", message=NO_HEARTBEAT)
        if float(age) > max_age_s:
            return HealthStatus(status="degraded", message=HEARTBEAT_STALE)
        return HealthStatus(status="healthy")

    return assess_readiness
