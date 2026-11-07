"""Delivery backoff (REQ-075, COMP-007): ``1m,5m,15m,1h`` then the last step again (hourly) until
the retry window (72 h by default) closes, then FAILED.

Pure functions over an explicit ``now``: the delivery handler and its tests share the clock they
pass in, so a simulated 2 h outage or a 72 h expiry needs no sleeping.

- ``parse_schedule("1m,5m,15m,1h")``: a non-empty list of positive durations (``s``, ``m``, ``h``
  suffixes; a bare number is seconds). Anything else raises ``ValueError`` (the setting is refused
  at startup, never silently replaced).
- ``next_not_before(attempts, first_attempt_at, now, ...)``: when the next attempt may run, or
  ``None`` when the window is over (the row becomes FAILED). The last attempt is placed exactly
  at the end of the window, so a relay that comes back in the last hour still gets its chance.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timedelta

DEFAULT_SCHEDULE = "1m,5m,15m,1h"
DEFAULT_WINDOW_HOURS = 72

_STEP = re.compile(r"^\s*(\d+)\s*([smh]?)\s*$")
_UNIT = {"": 1, "s": 1, "m": 60, "h": 3600}


def parse_schedule(text: str) -> tuple[timedelta, ...]:
    steps: list[timedelta] = []
    for part in str(text).split(","):
        match = _STEP.match(part)
        if match is None:
            raise ValueError(f"not a duration: {part.strip()!r} (use e.g. 30s, 5m, 1h)")
        seconds = int(match.group(1)) * _UNIT[match.group(2)]
        if seconds <= 0:
            raise ValueError("every step of the retry schedule must be positive")
        steps.append(timedelta(seconds=seconds))
    if not steps:
        raise ValueError("the retry schedule is empty")
    return tuple(steps)


def delay(attempts: int, schedule: Sequence[timedelta]) -> timedelta:
    """The wait after the ``attempts``-th failed attempt (1-based); past the list, its last step."""
    if attempts < 1:
        return timedelta(0)
    return schedule[min(attempts, len(schedule)) - 1]


def next_not_before(
    attempts: int,
    first_attempt_at: datetime,
    now: datetime,
    schedule: Sequence[timedelta],
    window: timedelta,
) -> datetime | None:
    """``None``: the window is over, the row is FAILED. Otherwise the next attempt's time,
    never later than the end of the window."""
    deadline = first_attempt_at + window
    if now >= deadline:
        return None
    return min(now + delay(attempts, schedule), deadline)
