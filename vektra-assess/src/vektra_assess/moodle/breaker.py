"""Per-site circuit breaker (plan assess-moodle-ws, group B).

closed -> open after ``threshold`` consecutive transport/5xx failures; open for
``cooldown_s``; then half-open, which lets exactly ONE probe through. Success closes it,
failure re-opens it. Open means ``Unreadable(breaker_open)``, never an error page.
In-memory and per worker process (single replica, COMP-003): replicas do not share state. The clock is injectable for tests.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        threshold: int = 5,
        cooldown_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._clock() - self._opened_at >= self._cooldown_s:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def allow(self) -> bool:
        state = self.state
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.HALF_OPEN and not self._probe_in_flight:
            self._probe_in_flight = True
            return True
        return False

    def release_probe(self) -> None:
        """Give the half-open probe slot back without counting an outcome: the exchange ended
        for a LOCAL reason (no token, policy refusal, cancellation) that says nothing about Moodle."""
        self._probe_in_flight = False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probe_in_flight = False

    def record_failure(self) -> None:
        self._probe_in_flight = False
        self._failures += 1
        if self._opened_at is not None or self._failures >= self._threshold:
            self._opened_at = (
                self._clock()
            )  # (re)open: a failed probe restarts the cooldown


class BreakerRegistry:
    def __init__(
        self,
        threshold: int = 5,
        cooldown_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._args = (threshold, cooldown_s, clock)
        self._by_site: dict[str, CircuitBreaker] = {}

    def for_site(self, site_id: str) -> CircuitBreaker:
        if site_id not in self._by_site:
            self._by_site[site_id] = CircuitBreaker(*self._args)
        return self._by_site[site_id]
