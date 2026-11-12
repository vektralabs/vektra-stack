"""Global async token bucket for Moodle calls (plan assess-moodle-ws, group B).

One bucket per worker process (single replica, COMP-003: scaling the worker out multiplies
the cap by the replica count), shared by every caller in it (render checks, gate
refresh, bursts of Starts): ``VEKTRA_LMS_MOODLE_WS_RPS`` is a cap on the load put on Moodle,
not a per-caller allowance. ``acquire`` waits instead of failing. Clock and sleep are
injectable so tests never sleep for real.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class AsyncTokenBucket:
    def __init__(
        self,
        rate: float,
        burst: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self._rate = rate
        self._capacity = burst if burst is not None else max(1.0, rate)
        self._tokens = self._capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(
            self._capacity, self._tokens + (now - self._last) * self._rate
        )
        self._last = now

    async def acquire(self) -> None:
        async with self._lock:  # FIFO: waiters queue behind the one sleeping
            self._refill()
            if self._tokens < 1.0:
                await self._sleep((1.0 - self._tokens) / self._rate)
                self._refill()
                # the sleep was computed to be exactly enough: absorb float rounding so a
                # clock that cannot represent the last epsilon never spins
                self._tokens = max(self._tokens, 1.0)
            self._tokens -= 1.0
