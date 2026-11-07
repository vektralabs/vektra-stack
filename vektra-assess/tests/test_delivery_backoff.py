"""Plan 10b ``delivery/backoff.py``: the schedule and the 72 h window, on an explicit clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vektra_assess.delivery import backoff as b

T0 = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
SCHED = b.parse_schedule(b.DEFAULT_SCHEDULE)
WINDOW = timedelta(hours=72)


def test_the_default_schedule() -> None:
    assert SCHED == (
        timedelta(minutes=1),
        timedelta(minutes=5),
        timedelta(minutes=15),
        timedelta(hours=1),
    )
    assert b.parse_schedule("30s, 2m ,90") == (
        timedelta(seconds=30),
        timedelta(minutes=2),
        timedelta(seconds=90),
    )


@pytest.mark.parametrize("bad", ["", "1m,,5m", "1d", "-1m", "0m", "five", "1m;5m"])
def test_a_malformed_schedule_is_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        b.parse_schedule(bad)


def test_steps_then_hourly() -> None:
    got = [b.delay(n, SCHED) for n in range(1, 8)]
    assert got == [
        timedelta(minutes=1),
        timedelta(minutes=5),
        timedelta(minutes=15),
        timedelta(hours=1),
        timedelta(hours=1),
        timedelta(hours=1),
        timedelta(hours=1),
    ]


def test_retries_stop_at_the_window_and_the_last_one_lands_on_its_end() -> None:
    now, attempts, times = T0, 0, []
    while True:
        attempts += 1
        nxt = b.next_not_before(attempts, T0, now, SCHED, WINDOW)
        if nxt is None:
            break
        times.append(nxt)
        now = nxt
    assert now == T0 + WINDOW  # the final attempt ran exactly at the end of the window
    assert times[:4] == [
        T0 + timedelta(minutes=1),
        T0 + timedelta(minutes=6),
        T0 + timedelta(minutes=21),
        T0 + timedelta(minutes=81),
    ]
    # 1+5+15 min, then hourly up to 72 h: 4 scheduled + 70 hourly + the capped last one
    assert len(times) == 75 and all(t <= T0 + WINDOW for t in times)
    assert (
        b.next_not_before(1, T0, T0 + WINDOW + timedelta(seconds=1), SCHED, WINDOW)
        is None
    )
