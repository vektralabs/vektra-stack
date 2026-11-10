"""The heartbeat and the reapers never queue behind job work (plan 13a review, finding 3).

The worker's composition gives the heartbeat, the lease reaper and the STARTING reaper their own
one-connection engine. Here every connection of the job pool is checked out and held (what a
burst of slow handlers does to it), and the three liveness bodies, taken from the composed tick
scheduler, still run at once: the ``assess_heartbeat`` row is counted fresh. The control composes
the same worker without the liveness engine: with the job pool exhausted the heartbeat cannot get
a connection at all, which proves the measurement tells the two apart.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest
from invariants import assert_invariants
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url

pytestmark = pytest.mark.integration

LIVENESS_TICKS = ("heartbeat", "lease_reaper", "starting_reaper")


def _worker_url(pg: Any) -> str:
    u = make_url(pg.url("worker"))
    return URL.create(
        u.drivername, u.username, u.password, u.host, u.port, u.database
    ).render_as_string(hide_password=False)


def _compose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pg: Any, *, liveness: bool
) -> tuple[Any, Any, Any]:
    from vektra_app import assess_worker
    from vektra_assess.config import VektraAssessSettings

    monkeypatch.setenv("VEKTRA_ENV", "development")
    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setenv("VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND", "true")
    monkeypatch.setenv("VEKTRA_ASSESS_WORKER_HEARTBEAT_FILE", str(tmp_path / "hb"))
    settings = VektraAssessSettings()
    url = _worker_url(pg)
    jobs, live = assess_worker.make_engines(url, settings.worker_pool_size, 30.0)
    checked = assess_worker.Checked(
        jobs, settings, url, {}, liveness=live if liveness else None
    )
    _runtime, scheduler, _hb, _control, _acquire, _audit = assess_worker._compose(
        checked
    )
    bodies = {t.name: t.body for t in scheduler._ticks if t.name in LIVENESS_TICKS}
    assert set(bodies) == set(LIVENESS_TICKS) and all(bodies.values())
    return jobs, live, bodies


async def _hold_all(engine: Any, stack: contextlib.AsyncExitStack) -> int:
    held = 0
    for _ in range(engine.pool.size()):
        conn = await stack.enter_async_context(engine.connect())
        await conn.execute(text("SELECT 1"))
        held += 1
    return held


async def _heartbeat_age(pg: Any) -> float | None:
    rows = await pg.run(
        "super", "SELECT extract(epoch FROM now() - beat_at) FROM assess_heartbeat"
    )
    return float(rows[0][0]) if rows else None


async def test_with_the_job_pool_exhausted_the_liveness_ticks_still_run(
    pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    await pg.run("super", "DELETE FROM assess_heartbeat")
    jobs, live, bodies = _compose(monkeypatch, tmp_path, pg, liveness=True)
    try:
        async with contextlib.AsyncExitStack() as stack:
            held = await _hold_all(jobs, stack)
            assert held == jobs.pool.size() >= 1
            assert jobs.pool.checkedout() == held  # nothing is left in the job pool
            for name in LIVENESS_TICKS:
                await asyncio.wait_for(bodies[name](), 3)
            age = await _heartbeat_age(pg)
            assert age is not None and age < 3, age
            # the budget of INT-002 is unchanged: job pool + liveness = the configured pool
            assert jobs.pool.size() + live.pool.size() == 5
        await assert_invariants(
            pg,
            "liveness-pool:job-pool-exhausted",
            expect_sessions=0,
            only=["no_stale_lease"],
        )
    finally:
        await jobs.dispose()
        await live.dispose()


async def test_control_without_the_liveness_engine_the_heartbeat_starves(
    pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    await pg.run("super", "DELETE FROM assess_heartbeat")
    jobs, live, bodies = _compose(monkeypatch, tmp_path, pg, liveness=False)
    try:
        async with contextlib.AsyncExitStack() as stack:
            await _hold_all(jobs, stack)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(bodies["heartbeat"](), 2)
        assert await _heartbeat_age(pg) is None  # the beat never reached the row
    finally:
        await jobs.dispose()
        await live.dispose()
