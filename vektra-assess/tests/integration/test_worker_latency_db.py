"""Claim latency (plan assess-job-loop, group D): the poll fallback and NOTIFY (INT-002).

Without any NOTIFY a committed job is claimed within 1.5 s by the 1 s poll; with NOTIFY the p95
of commit-to-handler-start over 200 jobs is at most 200 ms. Measured in-process with a monotonic
clock from the moment ``COMMIT`` returned to the moment the handler starts.
"""

from __future__ import annotations

import asyncio
import random
import time
from contextlib import asynccontextmanager

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from vektra_assess.jobs import JobKind, enqueue_job
from vektra_assess.worker.listener import ControlConnection
from vektra_assess.worker.runner import HandlerSpec, JobContext
from vektra_assess.worker.runtime import PoolSpec, SqlStore, WorkerRuntime

pytestmark = pytest.mark.integration


def _dsn(pg) -> str:
    return (
        make_url(pg.url("worker"))
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )


@asynccontextmanager
async def _no_db():
    raise AssertionError("not used")
    yield


async def _audit(event, data) -> None:  # pragma: no cover
    raise AssertionError("nothing may die")


async def _runtime(pg, poll_s: float, started: asyncio.Queue):
    pool = await asyncpg.create_pool(_dsn(pg), min_size=1, max_size=3)

    @asynccontextmanager
    async def acquire():
        async with pool.acquire() as c:
            yield c

    async def handle(ctx: JobContext) -> None:
        started.put_nowait(time.monotonic())

    rt = WorkerRuntime(
        SqlStore(acquire),
        worker_id="w",
        pools=[PoolSpec("maintenance", 4)],
        acquire_db=_no_db,
        audit=_audit,
        poll_interval_s=poll_s,
        rng=random.Random(1),
    )
    rt.register_handler(HandlerSpec(JobKind.PURGE, "maintenance", 5, 10, handle=handle))
    return rt, pool


async def _commit_job(pg, n: int) -> float:
    async with pg.conn("api") as c:
        await enqueue_job(c, JobKind.PURGE, {"n": n})
        await c.commit()
        return time.monotonic()


async def test_poll_fallback_alone_claims_within_one_and_a_half_seconds(pg):
    """No LISTEN at all: the enqueue sends its NOTIFY but nobody is listening."""
    await pg.run("super", "DELETE FROM assess_jobs")
    started: asyncio.Queue = asyncio.Queue()
    rt, pool = await _runtime(pg, 1.0, started)  # the production poll interval
    task = asyncio.create_task(rt.run())
    try:
        worst = 0.0
        for n in range(4):
            await asyncio.sleep(
                random.Random(n).uniform(0.05, 0.9)
            )  # land anywhere in the poll window
            t_commit = await _commit_job(pg, n)
            t_start = await asyncio.wait_for(started.get(), timeout=3)
            worst = max(worst, t_start - t_commit)
        assert worst <= 1.5, f"poll fallback took {worst:.2f}s"
    finally:
        await rt.stop()
        await task
        await rt.wait_idle()
        await pool.close()


async def test_notify_p95_enqueue_to_claim_is_under_200_ms_over_200_jobs(pg):
    await pg.run("super", "DELETE FROM assess_jobs")
    started: asyncio.Queue = asyncio.Queue()
    # a 30 s poll: only the notification can explain a fast claim
    rt, pool = await _runtime(pg, 30.0, started)
    control = ControlConnection(_dsn(pg), on_notify=rt.wake)
    await control.start()
    task = asyncio.create_task(rt.run())
    lat: list[float] = []
    try:
        await asyncio.sleep(0.3)  # the loop is parked on its wake event
        for n in range(200):
            t_commit = await _commit_job(pg, n)
            t_start = await asyncio.wait_for(started.get(), timeout=5)
            lat.append(t_start - t_commit)
    finally:
        await rt.stop()
        await task
        await rt.wait_idle()
        await control.stop()
        await pool.close()
    lat.sort()
    p95, p50, worst = lat[int(0.95 * len(lat)) - 1], lat[len(lat) // 2], lat[-1]
    assert p95 <= 0.2, (
        f"p95 {p95 * 1000:.0f} ms (p50 {p50 * 1000:.0f}, max {worst * 1000:.0f})"
    )
