"""Session time (REQ-069, REQ-070, REQ-086): ONE deadline formula, the outage cap and warnings.

    deadline = committed_at + duration + credited outage
    duration = cap + extra time, both frozen at Start (``assess_sessions.duration_s``)

``deadline`` is the Python form, ``DEADLINE_SQL`` the SQL form of the same formula over the columns
of ``assess_sessions``; every writer of ``deadline_at`` (the commit, the outage credit) uses the SQL
form, and a test checks the stored value against the Python one. Only the server's clock is ever
read: no request carries a client time (the request models forbid unknown fields).

``outage_cap_s`` is C = min(15 min, 50% of the duration): cumulative outage above C closes the
session as ``service_failure`` and caps the credit. ``due_warnings`` decides the 10 / 5 / 1 minute
warnings against the CURRENT deadline (10 only when the duration is 30 minutes or more), each once
per (session, threshold, deadline): a deadline moved by an outage credit warns again at its new
times and never at the old ones.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta

#: the SQL form of ``deadline`` over the ``assess_sessions`` columns (no parameters)
DEADLINE_SQL = "committed_at + make_interval(secs => duration_s + credited_outage_s)"

OUTAGE_CAP_MAX_S = 900  # 15 minutes
WARN_THRESHOLDS_MIN = (10, 5, 1)
WARN_10_MIN_DURATION_S = 30 * 60


def deadline(committed_at: datetime, duration_s: int, credited_s: int = 0) -> datetime:
    if committed_at.tzinfo is None:
        raise ValueError("deadline needs an aware (UTC) timestamp")
    return committed_at + timedelta(seconds=duration_s + credited_s)


def outage_cap_s(duration_s: int | None, cap_max_s: int = OUTAGE_CAP_MAX_S) -> int:
    """C = min(cap_max_s, 50% of the session duration); a missing duration gives 0.

    The Python mirror of the SQL ``assess_outage_cap`` (revision 0032), which is the definition
    the database, the probe and the lazy close use; a test checks the two agree."""
    return min(cap_max_s, (duration_s or 0) // 2)


def minutes_left(seconds: float) -> int:
    """Whole minutes left, rounded UP (``/state``'s ``active.minutes_left``): "1 minute" until
    the very end, 0 only once the time is over. The discussion UI's timer rounds the same way
    (``ui/src/views/controls.ts`` ``minutesLeft``; shared test vectors)."""
    return max(0, -(-int(seconds) // 60))


def remaining_s(deadline_at: datetime, now: datetime) -> int:
    return max(0, int((deadline_at - now).total_seconds()))


WarningKey = tuple[int, str]  # (threshold minutes, deadline ISO): unique per session


def due_warnings(
    deadline_at: datetime,
    now: datetime,
    duration_s: int,
    emitted: Collection[WarningKey],
    *,
    tolerance_s: int = 5,
) -> list[int]:
    """Thresholds (minutes) due now and not yet emitted for THIS deadline.

    A threshold is due inside ``[deadline - minutes, deadline - minutes + tolerance)`` only, so a
    stream that connects late does not replay a warning whose moment has passed, and a threshold
    passed while the session was in OUTAGE (the deadline then moves) is judged against the new
    deadline."""
    out = []
    for minutes in WARN_THRESHOLDS_MIN:
        if minutes == 10 and duration_s < WARN_10_MIN_DURATION_S:
            continue
        at = deadline_at - timedelta(minutes=minutes)
        if at <= now < at + timedelta(seconds=tolerance_s):
            if (minutes, deadline_at.isoformat()) not in emitted:
                out.append(minutes)
    return out
