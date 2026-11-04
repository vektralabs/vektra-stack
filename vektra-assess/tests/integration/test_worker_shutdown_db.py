"""Graceful shutdown on a real Postgres (plan assess-job-loop, SIGTERM task).

In-flight jobs that do not finish inside the drain window go back to ``ready`` with no lease and no
spent attempt, the heartbeat row ends ``stopped``, and a second runtime completes them at once.
"""

from __future__ import annotations

import asyncio
import random
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from vektra_assess.jobs import JobKind
from vektra_assess.worker.heartbeat import Heartbeat
from vektra_assess.worker.listener import ControlConnection
from vektra_assess.worker.runner import HandlerSpec, JobContext
from vektra_assess.worker.runtime import PoolSpec, SqlStore, WorkerRuntime
from vektra_assess.worker.shutdown import shutdown
from vektra_assess.worker.ticks import TickScheduler

pytestmark = pytest.mark.integration


def _dsn(pg) -> str:
    return (
        make_url(pg.url("worker"))
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )


def _acquire(pg):
    @asynccontextmanager
    async def acquire():
        c = await asyncpg.connect(_dsn(pg))
        try:
            yield c
        finally:
            await c.close()

    return acquire


@asynccontextmanager
async def _no_db():
    raise AssertionError("not used")
    yield


async def _audit(event, data) -> None:
    raise AssertionError("nothing dies in this test")


def _runtime(pg, worker_id: str, handle) -> WorkerRuntime:
    rt = WorkerRuntime(
        SqlStore(_acquire(pg)),
        worker_id=worker_id,
        pools=[PoolSpec("maintenance", 4)],
        acquire_db=_no_db,
        audit=_audit,
        poll_interval_s=0.05,
        rng=random.Random(2),
    )
    rt.register_handler(
        HandlerSpec(
            JobKind.PURGE, "maintenance", budget_s=60, lease_s=120, handle=handle
        )
    )
    return rt


async def test_sigterm_releases_stuck_jobs_and_a_second_worker_finishes_them(
    pg, tmp_path: Path
):
    await pg.run("super", "DELETE FROM assess_jobs")
    await pg.run("super", "DELETE FROM assess_heartbeat")
    for i in range(3):
        await pg.run(
            "super",
            "INSERT INTO assess_jobs (kind, args) VALUES ('purge', jsonb_build_object('n', CAST(:i AS integer)))",
            {"i": i},
        )

    async def stuck(ctx: JobContext) -> None:
        await asyncio.sleep(120)

    a = _runtime(pg, "wa", stuck)
    control = ControlConnection(_dsn(pg), on_notify=lambda: None)
    await control.start()
    hb = Heartbeat(
        _acquire(pg),
        worker_id="wa",
        version="t",
        file_path=tmp_path / "hb",
        stats=a.stats,
    )
    hb.set_state("ready")
    sched = TickScheduler(control, [])
    run_task = asyncio.create_task(a.run())
    try:
        async with asyncio.timeout(10):
            while a.in_flight < 3:
                await asyncio.sleep(0.05)
        assert (
            await pg.run(
                "super", "SELECT count(*) FROM assess_jobs WHERE state='leased'"
            )
        )[0][0] == 3
        released = await shutdown(
            runtime=a,
            run_task=run_task,
            scheduler=sched,
            heartbeat=hb,
            control=control,
            drain_s=0.3,
        )
    finally:
        run_task.cancel()
    assert released == 3
    rows = await pg.run(
        "super",
        "SELECT state, attempts, leased_by, lease_until IS NULL FROM assess_jobs",
    )
    assert [tuple(r) for r in rows] == [("ready", 0, None, True)] * 3
    assert (await pg.run("super", "SELECT details->>'state' FROM assess_heartbeat"))[0][
        0
    ] == "stopped"
    assert not control.connected

    done: list[int] = []

    async def finish(ctx: JobContext) -> None:
        done.append(int(ctx.args["n"]))

    b = _runtime(pg, "wb", finish)
    task_b = asyncio.create_task(b.run())
    try:
        async with asyncio.timeout(10):
            while (
                await pg.run(
                    "super", "SELECT count(*) FROM assess_jobs WHERE state<>'done'"
                )
            )[0][0]:
                await asyncio.sleep(0.05)
    finally:
        await b.stop()
        await task_b
        await b.wait_idle()
    assert sorted(done) == [0, 1, 2]
    states = await pg.run("super", "SELECT attempts FROM assess_jobs")
    assert [r[0] for r in states] == [1, 1, 1], (
        "the released run was not counted as an attempt"
    )
