"""Opportunistic housekeeping of the API's in-process session state (review fix 3).

The API runs with ``lifespan="off"`` (BUG-025), so no background timer exists. Instead every
assess request runs ``assess_housekeeping`` (a router-level dependency), which sweeps at most once
every ``interval_s``:

- the DEK cache: expired entries (``DekCache.sweep``);
- the event broker: channels ended (``closed``, ``start_rolled_back``) beyond their TTL and idle
  channels (``EventBroker.sweep_idle``);
- the assess-local rate limiters: keys whose window is empty (``cleanup``).

The sweep is synchronous, in-memory and bounded by the number of live entries; it never touches
the database and never fails a request.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import structlog
from fastapi import Request

log = structlog.get_logger(__name__)

INTERVAL_S = 30.0


class Housekeeper:
    def __init__(
        self,
        interval_s: float = INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.interval_s = interval_s
        self._clock = clock
        self._last: float | None = None
        self.runs = 0

    def due(self) -> bool:
        now = self._clock()
        if self._last is not None and now - self._last < self.interval_s:
            return False
        self._last = now
        return True

    def sweep(self, state: object) -> int:
        self.runs += 1
        removed = 0
        cache = getattr(state, "assess_dek_cache", None)
        if cache is not None:
            removed += int(cache.sweep())
        broker = getattr(state, "assess_events", None)
        if broker is not None:
            removed += int(broker.sweep_idle())
        for limiter in (getattr(state, "assess_limiters", None) or {}).values():
            cleanup = getattr(limiter, "cleanup", None)
            if callable(cleanup):
                removed += int(cleanup())
        if removed:
            log.info("assess_housekeeping", removed=removed)
        return removed


def assess_housekeeping(request: Request) -> None:
    """Router-level dependency of every assess route: a sweep when one is due."""
    state = request.app.state
    keeper = getattr(state, "assess_housekeeper", None)
    if keeper is None:
        keeper = Housekeeper()
        state.assess_housekeeper = keeper
    if keeper.due():
        try:
            keeper.sweep(state)
        except Exception as exc:  # housekeeping never fails a request
            log.warning("assess_housekeeping_failed", error_type=type(exc).__name__)
