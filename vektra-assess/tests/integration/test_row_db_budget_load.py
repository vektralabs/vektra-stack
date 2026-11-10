"""Load test of the shared row budget on a real pool (plan 13a, CodeRabbit 4513956359 on #190).

Two delivery jobs x three rows each want a connection at once from the worker's job pool (size 4:
the default 5 minus the liveness engine, ``max_overflow=0``), with DB latency injected
(``pg_sleep``). A job-loop statement on the same pool checks out a connection every 100 ms. With
the budget, the rows never hold more than ``4 - RESERVED`` connections and the job loop never
waits for a connection. (The heartbeat and the reapers have their own engine:
``tests/chaos/assess/test_chaos_liveness_pool.py``.) The unbudgeted control shows the
heartbeat queued behind the rows, which proves the measurement can tell the two cases apart.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from vektra_assess.delivery.budget import RowDbBudget, budgeted

pytestmark = pytest.mark.integration

POOL = 4  # the job pool of the default worker pool of 5
LATENCY_S = 0.6


async def _run(pg, budget: RowDbBudget | None) -> tuple[float, int]:  # type: ignore[no-untyped-def]
    engine = create_async_engine(
        pg.super_url, pool_size=POOL, max_overflow=0, pool_timeout=30
    )
    acquire = budgeted(budget, engine.connect)
    held = peak = 0
    stop = asyncio.Event()
    waits: list[float] = []

    async def row() -> None:
        nonlocal held, peak
        for _ in range(2):  # claim, settle
            async with acquire() as conn:
                held += 1
                peak = max(peak, held)
                try:
                    await conn.execute(text("SELECT pg_sleep(:s)"), {"s": LATENCY_S})
                finally:
                    held -= 1

    async def heartbeat() -> None:
        while not stop.is_set():
            t = time.monotonic()
            async with engine.connect() as conn:
                waits.append(time.monotonic() - t)
                await conn.execute(text("SELECT 1"))
            await asyncio.sleep(0.1)

    try:
        async with (
            engine.connect() as conn
        ):  # warm the pool so connects are not measured
            await conn.execute(text("SELECT 1"))
        hb = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.2)
        await asyncio.gather(*(row() for _ in range(6)))  # 2 jobs x 3 rows
        stop.set()
        await hb
    finally:
        await engine.dispose()
    return max(waits), peak


async def test_with_the_budget_the_heartbeat_never_queues_behind_the_rows(pg) -> None:  # type: ignore[no-untyped-def]
    budget = RowDbBudget.for_pool(POOL)
    worst_wait, peak = await _run(pg, budget)
    assert peak <= budget.permits == 3
    # a free connection is always there for the heartbeat: no wait close to a row's latency
    assert worst_wait < LATENCY_S / 2, worst_wait


async def test_control_without_the_budget_the_heartbeat_waits(pg) -> None:  # type: ignore[no-untyped-def]
    worst_wait, peak = await _run(pg, None)
    assert peak == POOL  # the rows took the whole pool
    assert worst_wait >= LATENCY_S / 2, worst_wait
