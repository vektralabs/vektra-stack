"""The heartbeat row, written as the worker and read as the API role (plan 8.1)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from vektra_assess.health import make_readiness_check

pytestmark = pytest.mark.integration

# the exact statement the worker's heartbeat loop runs
WORKER_BEAT = (
    "INSERT INTO assess_heartbeat (component, beat_at) VALUES ('worker', now()) "
    "ON CONFLICT (component) DO UPDATE SET beat_at = now()"
)


async def test_api_role_reads_the_worker_heartbeat(pg):
    await pg.run("super", "DELETE FROM assess_heartbeat")
    engine = create_async_engine(pg.url("api"))
    try:
        check = make_readiness_check(
            enabled=True, get_engine=lambda: engine, max_age_s=60
        )
        assert (await check()).message == "no_heartbeat"
        await pg.run("worker", WORKER_BEAT)
        await pg.run("worker", WORKER_BEAT)  # idempotent upsert
        assert (await check()).status == "healthy"
        assert (await pg.run("super", "SELECT count(*) FROM assess_heartbeat"))[0][
            0
        ] == 1
        await pg.run(
            "super",
            "UPDATE assess_heartbeat SET beat_at = now() - interval '5 minutes'",
        )
        stale = await check()
        assert (stale.status, stale.message) == ("degraded", "heartbeat_stale")
    finally:
        await engine.dispose()
