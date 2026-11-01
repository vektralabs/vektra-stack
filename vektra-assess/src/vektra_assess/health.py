"""Assess readiness check for /health/assess (plan assess-foundations 8.2).

Registered as ``("health", "assess")`` by the API's startup step 12 whether assess is
enabled or not, so ``GET /health/assess`` always answers. It is excluded from the
shallow ``/health`` (``vektra_admin.health.SHALLOW_EXCLUDED``): the worker is another
container. When enabled it never reports ``healthy`` without evidence: until the worker
heartbeat table exists (plan assess-data-crypto) it reports ``degraded`` with reason
``heartbeat_source_missing``.

Owning plan: .s2s/plans/20261101-154950-assess-foundations.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from vektra_shared.types import HealthStatus

DISABLED = "disabled"
HEARTBEAT_SOURCE_MISSING = "heartbeat_source_missing"


def make_readiness_check(*, enabled: bool) -> Callable[[], Awaitable[HealthStatus]]:
    async def assess_readiness() -> HealthStatus:
        if not enabled:
            return HealthStatus(status=DISABLED, message="VEKTRA_ASSESS_ENABLED=false")
        return HealthStatus(status="degraded", message=HEARTBEAT_SOURCE_MISSING)

    return assess_readiness
