"""One database-connection budget shared by every fan-out delivery handler (plan 13a).

The transcript handler and the notification handler each run their rows concurrently, up to 3
rows per job, and the delivery pool runs 2 jobs at once. So up to 6 rows can want a connection
at the same instant from a worker pool of ``VEKTRA_ASSESS_WORKER_POOL_SIZE`` (5 by default,
``max_overflow=0``). That pool is shared with the heartbeat and the other pools (CodeRabbit
4513956359 on PR #190). A row holds a connection only for its claim and its settle, never across
the SMTP send, so the pool waits rather than fails. Even so, a slow database lets the rows queue
ahead of the heartbeat, which then goes stale.

``RowDbBudget`` is the one coordinator. Every row-level database step of both handlers takes a
permit before it checks a connection out. The permits are the pool size minus a reserve kept for
the heartbeat, the reapers and the job loop's own statements. The handlers never use the pool
past the budget, so the reserve is always there for the rest of the worker.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

#: connections kept free for the heartbeat, the lease reaper and the job loop's claim/settle
RESERVED = 2


class RowDbBudget:
    def __init__(self, permits: int) -> None:
        if permits < 1:
            raise ValueError("a row budget needs at least one permit")
        self.permits = permits
        self._sem = asyncio.Semaphore(permits)
        self.in_use = 0
        self.peak = 0

    @classmethod
    def for_pool(cls, pool_size: int, reserved: int = RESERVED) -> RowDbBudget:
        """The pool minus the reserve, never below one permit (a pool of 1 or 2 still runs)."""
        return cls(max(1, pool_size - reserved))

    def wrap(self, acquire: Callable[[], Any]) -> Callable[[], Any]:
        """``acquire`` with a permit taken first and released after the connection is back."""

        @asynccontextmanager
        async def guarded() -> AsyncIterator[Any]:
            async with self._sem:
                self.in_use += 1
                self.peak = max(self.peak, self.in_use)
                try:
                    async with acquire() as conn:
                        yield conn
                finally:
                    self.in_use -= 1

        return guarded


def budgeted(
    budget: RowDbBudget | None, acquire: Callable[[], Any]
) -> Callable[[], Any]:
    return acquire if budget is None else budget.wrap(acquire)
