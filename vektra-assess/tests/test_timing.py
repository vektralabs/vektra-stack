"""M3.1: one deadline formula, the outage cap and the warning schedule (fake clock)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vektra_assess.api.models import TurnRequest
from vektra_assess.timing import (
    DEADLINE_SQL,
    deadline,
    due_warnings,
    outage_cap_s,
    remaining_s,
)

T0 = datetime(2026, 11, 6, 9, 0, 0, tzinfo=UTC)


def test_deadline_is_commit_plus_cap_plus_extra_plus_credit() -> None:
    assert deadline(T0, 30 * 60) == T0 + timedelta(minutes=30)
    assert deadline(T0, 30 * 60 + 600, 180) == T0 + timedelta(minutes=43)
    assert "duration_s + credited_outage_s" in DEADLINE_SQL
    with pytest.raises(ValueError):
        deadline(T0.replace(tzinfo=None), 60)


def test_remaining_at_the_utc_boundaries() -> None:
    d = deadline(T0, 1800)
    assert remaining_s(d, d - timedelta(seconds=1)) == 1
    assert remaining_s(d, d) == 0
    assert remaining_s(d, d + timedelta(seconds=1)) == 0
    # a local-time representation of the same instant gives the same answer
    cet = d.astimezone(__import__("zoneinfo").ZoneInfo("Europe/Rome"))
    assert remaining_s(cet, d - timedelta(seconds=1)) == 1


def test_outage_cap_is_min_15_min_and_half_the_duration() -> None:
    assert outage_cap_s(120 * 60) == 900
    assert outage_cap_s(20 * 60) == 600
    assert outage_cap_s(5 * 60) == 150


def test_no_request_field_can_carry_a_client_time() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TurnRequest.model_validate({"text": "x", "client_time": "2026-11-06T09:00:00Z"})


def test_warnings_fire_once_per_deadline_and_skip_10_for_short_sessions() -> None:
    d = deadline(T0, 30 * 60)
    emitted: set[tuple[int, str]] = set()
    for m in (10, 5, 1):
        at = d - timedelta(minutes=m)
        assert due_warnings(d, at - timedelta(seconds=1), 1800, emitted) == []
        assert due_warnings(d, at, 1800, emitted) == [m]
        emitted.add((m, d.isoformat()))
        assert due_warnings(d, at + timedelta(seconds=2), 1800, emitted) == []
    short = deadline(T0, 20 * 60)
    assert due_warnings(short, short - timedelta(minutes=10), 1200, set()) == []


def test_an_outage_credit_moves_the_warnings_and_none_fire_at_the_old_deadline() -> (
    None
):
    old = deadline(T0, 30 * 60)
    new = deadline(T0, 30 * 60, credited_s=180)
    for m in (10, 5, 1):
        # at the old deadline's moment, against the CURRENT (new) deadline: nothing
        assert due_warnings(new, old - timedelta(minutes=m), 1800, set()) == []
        hit = new - timedelta(minutes=m)
        assert due_warnings(new, hit + timedelta(seconds=4), 1800, set()) == [m]
        assert due_warnings(new, hit + timedelta(seconds=5), 1800, set()) == []
