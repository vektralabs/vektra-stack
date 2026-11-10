"""The shared row connection budget of the delivery handlers (plan assess-hardening-release, 13a)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from vektra_assess.delivery.budget import RESERVED, RowDbBudget, budgeted


class FakePool:
    def __init__(self) -> None:
        self.out = 0
        self.peak = 0

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[object]:
        self.out += 1
        self.peak = max(self.peak, self.out)
        try:
            await asyncio.sleep(0.02)
            yield object()
        finally:
            self.out -= 1


def test_the_budget_is_the_pool_minus_the_reserve_never_below_one() -> None:
    # the job pool of the default worker pool of 5 is 4 (1 goes to the liveness engine)
    assert RowDbBudget.for_pool(4).permits == 4 - RESERVED == 3
    assert RowDbBudget.for_pool(2).permits == 1
    assert RowDbBudget.for_pool(1).permits == 1
    with pytest.raises(ValueError):
        RowDbBudget(0)


async def test_six_rows_never_hold_more_connections_than_the_permits() -> None:
    """2 delivery jobs x 3 rows each, all at once: at most ``permits`` connections out."""
    pool, budget = FakePool(), RowDbBudget(3)
    acquire = budget.wrap(pool.acquire)

    async def row() -> None:
        for _ in range(2):  # claim, then settle
            async with acquire():
                await asyncio.sleep(0.01)

    await asyncio.gather(*(row() for _ in range(6)))
    assert pool.peak == 3 and budget.peak == 3 and budget.in_use == 0


async def test_without_the_budget_the_six_rows_take_six_connections() -> None:
    """Control: the same load unbounded reaches 6, so the test above is not vacuous."""
    pool = FakePool()
    acquire = budgeted(None, pool.acquire)

    async def row() -> None:
        async with acquire():
            await asyncio.sleep(0.01)

    await asyncio.gather(*(row() for _ in range(6)))
    assert pool.peak == 6


async def test_a_failing_row_gives_its_permit_back() -> None:
    pool, budget = FakePool(), RowDbBudget(1)
    acquire = budget.wrap(pool.acquire)
    with pytest.raises(RuntimeError):
        async with acquire():
            raise RuntimeError("settle failed")
    assert budget.in_use == 0
    async with asyncio.timeout(1):
        async with acquire():
            pass  # the single permit is free again
