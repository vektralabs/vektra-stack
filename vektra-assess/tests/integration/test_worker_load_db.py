"""Two concurrent runtimes over 500 jobs (plan assess-job-loop, group D, first integration task).

Every job runs exactly once: ``done`` rows = 500 AND side-effect rows = 500 with a UNIQUE job
reference, so a double execution would raise instead of hiding in a counter. Pool caps are observed
from inside the handlers (peak in-flight per runtime), not read back from the configuration.

The other bullets of the same plan task are proven where their mechanism lives and are listed in
the plan notes: expired-lease reclaim and fenced completion (``test_worker_sql_db``), timeout with
backoff and ``max_attempts`` to ``dead`` (``test_worker_runtime_db``).
"""

from __future__ import annotations

import asyncio
import random
from contextlib import asynccontextmanager

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from vektra_assess.jobs import JobKind
from vektra_assess.worker.runner import HandlerSpec, JobContext
from vektra_assess.worker.runtime import PoolSpec, SqlStore, WorkerRuntime

pytestmark = pytest.mark.integration

N = 500
CAP = 6  # concurrency of each runtime's pool


def _dsn(pg) -> str:
    return (
        make_url(pg.url("worker"))
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )


async def test_500_jobs_two_runtimes_exactly_once_and_pool_caps_hold(pg):
    await pg.run("super", "DELETE FROM assess_jobs")
    await pg.run("super", "DROP TABLE IF EXISTS assess_job_effects")
    await pg.run(
        "super",
        "CREATE TABLE assess_job_effects (job_n INTEGER PRIMARY KEY, worker TEXT NOT NULL)",
    )
    await pg.run(
        "super", "GRANT INSERT, SELECT ON assess_job_effects TO vektra_assess_worker"
    )
    await pg.run(
        "super",
        "INSERT INTO assess_jobs (kind, args) "
        "SELECT 'purge', jsonb_build_object('n', g) FROM generate_series(1, :n) g",
        {"n": N},
    )

    engine = create_async_engine(pg.url("worker"), pool_size=8, max_overflow=0)
    in_flight = {"wa": 0, "wb": 0}
    peak = {"wa": 0, "wb": 0}

    def make_handler(worker: str):
        async def handle(ctx: JobContext) -> None:
            in_flight[worker] += 1
            peak[worker] = max(peak[worker], in_flight[worker])
            try:
                await asyncio.sleep(0.002)
                async with ctx.acquire() as conn:
                    # UNIQUE (job_n): a second execution of the same job raises here
                    await conn.execute(
                        text("INSERT INTO assess_job_effects VALUES (:n, :w)"),
                        {"n": int(ctx.args["n"]), "w": worker},
                    )
                    await conn.commit()
            finally:
                in_flight[worker] -= 1

        return handle

    async def audit(event, data) -> None:  # pragma: no cover
        raise AssertionError("nothing may die")

    pools = []
    runtimes = []
    for wid in ("wa", "wb"):
        pool = await asyncpg.create_pool(_dsn(pg), min_size=1, max_size=4)
        pools.append(pool)

        @asynccontextmanager
        async def acquire(p=pool):
            async with p.acquire() as c:
                yield c

        rt = WorkerRuntime(
            SqlStore(acquire),
            worker_id=wid,
            pools=[PoolSpec("maintenance", CAP)],
            acquire_db=engine.connect,
            audit=audit,
            poll_interval_s=0.05,
            rng=random.Random(4),
        )
        rt.register_handler(
            HandlerSpec(JobKind.PURGE, "maintenance", 10, 20, handle=make_handler(wid))
        )
        runtimes.append(rt)

    tasks = [asyncio.create_task(rt.run()) for rt in runtimes]
    try:
        async with asyncio.timeout(120):
            while (
                await pg.run(
                    "super", "SELECT count(*) FROM assess_jobs WHERE state <> 'done'"
                )
            )[0][0]:
                await asyncio.sleep(0.1)
    finally:
        for rt in runtimes:
            await rt.stop()
        await asyncio.gather(*tasks)
        for rt in runtimes:
            await rt.wait_idle()
        for p in pools:
            await p.close()
        await engine.dispose()

    assert (
        await pg.run("super", "SELECT count(*) FROM assess_jobs WHERE state='done'")
    )[0][0] == N
    effects = (
        await pg.run(
            "super", "SELECT count(*), count(DISTINCT job_n) FROM assess_job_effects"
        )
    )[0]
    assert tuple(effects) == (N, N), "every job ran exactly once: 0 duplicates, 0 lost"
    attempts = (
        await pg.run("super", "SELECT min(attempts), max(attempts) FROM assess_jobs")
    )[0]
    assert tuple(attempts) == (1, 1), "no job needed a retry"
    for wid in ("wa", "wb"):
        assert 1 <= peak[wid] <= CAP, (wid, peak)
    by = await pg.run(
        "super", "SELECT worker, count(*) FROM assess_job_effects GROUP BY 1 ORDER BY 1"
    )
    assert len(by) == 2 and all(r[1] > 0 for r in by), f"both runtimes did work: {by}"
    await pg.run("super", "DROP TABLE assess_job_effects")
