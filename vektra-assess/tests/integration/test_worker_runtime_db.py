"""``WorkerRuntime`` on a real Postgres through ``SqlStore`` (plan B3).

Two runtimes (two worker ids) drain one table: every job runs exactly once, retried jobs
back off and end ``dead`` with an audit event, and states are read back from the table.
"""

from __future__ import annotations

import asyncio
import random
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from vektra_assess.jobs import JobKind
from vektra_assess.worker.runner import HandlerSpec, JobContext
from vektra_assess.worker.runtime import PoolSpec, SqlStore, WorkerRuntime

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _no_db():  # type: ignore[no-untyped-def]
    raise AssertionError("not used")
    yield


async def _runtime(pg, worker_id: str, audit, handlers, *, concurrency: int = 4):
    dsn = (
        make_url(pg.url("worker"))
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)

    @asynccontextmanager
    async def acquire():
        async with pool.acquire() as c:
            yield c

    rt = WorkerRuntime(
        SqlStore(acquire),
        worker_id=worker_id,
        pools=[PoolSpec("maintenance", concurrency)],
        acquire_db=_no_db,
        audit=audit,
        poll_interval_s=0.05,
        backoff_base_s=0.05,
        backoff_cap_s=0.1,
        rng=random.Random(3),
    )
    for spec in handlers:
        rt.register_handler(spec)
    return rt, pool


def _spec(handle: Any, **kw: Any) -> HandlerSpec:
    return HandlerSpec(
        JobKind.PURGE, "maintenance", budget_s=kw.pop("budget_s", 2),
        lease_s=kw.pop("lease_s", 4), handle=handle, **kw,
    )  # fmt: skip


async def _drain(
    pg, *runtimes: WorkerRuntime, until: str, timeout_s: float = 15.0
) -> None:
    tasks = [asyncio.create_task(rt.run()) for rt in runtimes]
    try:
        async with asyncio.timeout(timeout_s):
            while (await pg.run("super", until))[0][0] != 0:
                await asyncio.sleep(0.05)
    finally:
        for rt in runtimes:
            await rt.stop()
        await asyncio.gather(*tasks)
        for rt in runtimes:
            await rt.wait_idle()


async def test_two_workers_run_every_job_exactly_once(pg):
    await pg.run("super", "DELETE FROM assess_jobs")
    for i in range(20):
        await pg.run(
            "super",
            "INSERT INTO assess_jobs (kind, args) VALUES ('purge', jsonb_build_object('n', CAST(:i AS integer)))",
            {"i": i},
        )
    ran: list[int] = []

    async def handle(ctx: JobContext) -> None:
        ran.append(int(ctx.args["n"]))
        await asyncio.sleep(0.01)

    async def audit(event: str, data: Any) -> None:  # pragma: no cover
        raise AssertionError("nothing should die here")

    a, pa = await _runtime(pg, "wa", audit, [_spec(handle)])
    b, pb = await _runtime(pg, "wb", audit, [_spec(handle)])
    try:
        await _drain(
            pg, a, b, until="SELECT count(*) FROM assess_jobs WHERE state <> 'done'"
        )
    finally:
        await pa.close()
        await pb.close()
    assert sorted(ran) == list(range(20)), "each job exactly once, none twice"
    assert (
        await pg.run("super", "SELECT count(*) FROM assess_jobs WHERE state='done'")
    )[0][0] == 20
    assert (
        a.counters[("completed", JobKind.PURGE)]
        + b.counters[("completed", JobKind.PURGE)]
        == 20
    )


async def test_failing_job_retries_with_backoff_then_dies_with_an_audit_event(pg):
    await pg.run("super", "DELETE FROM assess_jobs")
    await pg.run(
        "super",
        "INSERT INTO assess_jobs (kind, args, max_attempts) VALUES ('purge', jsonb_build_object('k', 1), 3)",
    )
    attempts: list[int] = []
    events: list[tuple[str, dict[str, Any]]] = []

    async def boom(ctx: JobContext) -> None:
        attempts.append(ctx.attempt)
        raise RuntimeError("private text that must not be stored")

    async def audit(event: str, data: Any) -> None:
        events.append((event, dict(data)))

    rt, pool = await _runtime(pg, "wa", audit, [_spec(boom)])
    try:
        await _drain(
            pg, rt, until="SELECT count(*) FROM assess_jobs WHERE state <> 'dead'"
        )
    finally:
        await pool.close()
    assert attempts == [1, 2, 3]
    row = (
        await pg.run(
            "super", "SELECT state, attempts, last_error_code FROM assess_jobs"
        )
    )[0]
    assert tuple(row) == ("dead", 3, "handler_error")
    assert [e[0] for e in events] == ["assess_job_dead"]
    assert events[0][1]["attempts"] == 3
    assert "private" not in repr(events) and "private" not in repr(tuple(row))


async def test_timeout_is_recorded_as_a_fixed_code(pg):
    await pg.run("super", "DELETE FROM assess_jobs")
    await pg.run(
        "super", "INSERT INTO assess_jobs (kind, max_attempts) VALUES ('purge', 1)"
    )

    async def hang(ctx: JobContext) -> None:
        await asyncio.sleep(30)

    async def audit(event: str, data: Any) -> None:
        pass

    rt, pool = await _runtime(pg, "wa", audit, [_spec(hang, budget_s=0.1, lease_s=0.2)])
    try:
        await _drain(
            pg, rt, until="SELECT count(*) FROM assess_jobs WHERE state <> 'dead'"
        )
    finally:
        await pool.close()
    row = (await pg.run("super", "SELECT state, last_error_code FROM assess_jobs"))[0]
    assert tuple(row) == ("dead", "timeout")
