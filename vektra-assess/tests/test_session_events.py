"""M1.8: the per-session ``EventBroker`` (ids, ring, Last-Event-ID replay, state observation)."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from vektra_assess.api.events import EVENT_TYPES, EventBroker


def test_ids_are_monotonic_per_session_and_the_ring_is_bounded() -> None:
    b = EventBroker(ring_size=3)
    a, c = uuid.uuid4(), uuid.uuid4()
    ids = [b.publish(a, "reply_delta", {"text": str(i)}, epoch=1).id for i in range(5)]
    assert ids == [1, 2, 3, 4, 5]
    assert b.publish(c, "turn_accepted").id == 1  # per session
    assert [e.id for e in b.after(a, 0)] == [3, 4, 5]  # the ring keeps the last 3
    assert [e.id for e in b.after(a, 4)] == [5]  # Last-Event-ID replay


def test_the_catalogue_is_closed() -> None:
    assert len(EVENT_TYPES) == 13
    with pytest.raises(ValueError):
        EventBroker().publish(uuid.uuid4(), "debug_dump")


def test_observe_publishes_once_per_change() -> None:
    b = EventBroker()
    sid = uuid.uuid4()
    assert b.observe(sid, "STARTING", {}) is None  # first sight: no event
    assert b.observe(sid, "STARTING", {}) is None
    e = b.observe(sid, "ACTIVE", {"state": "ACTIVE"})
    assert e is not None and e.type == "committed"
    assert b.observe(sid, "ACTIVE", {}) is None  # a second poller sees no change
    assert b.observe(sid, "OUTAGE", {}).type == "outage_start"  # type: ignore[union-attr]
    assert b.observe(sid, "ACTIVE", {}).type == "outage_end"  # type: ignore[union-attr]
    assert b.observe(sid, "OUTAGE", {}).type == "outage_start"  # type: ignore[union-attr]
    assert b.observe(sid, "CLOSED", {"close_reason": "finished"}).type == "closed"  # type: ignore[union-attr]
    other = uuid.uuid4()
    b.observe(other, "STARTING", {})
    assert b.observe(other, None, {"reason": "capacity"}).type == "start_rolled_back"  # type: ignore[union-attr]


def test_an_outage_ended_and_reopened_between_two_polls_is_published() -> None:
    """Review of #222, finding 1: the state string alone (OUTAGE, then OUTAGE) hid the change;
    the outage count read with it shows it, as the end of one and the start of the next."""
    b = EventBroker()
    sid = uuid.uuid4()
    b.observe(sid, "ACTIVE", {}, outage_count=0)
    assert b.observe(sid, "OUTAGE", {}, outage_count=1).type == "outage_start"  # type: ignore[union-attr]
    assert b.observe(sid, "OUTAGE", {}, outage_count=1) is None  # nothing new
    moved = {"state": "OUTAGE", "deadline_at": "2026-11-14T19:00:00+00:00"}
    b.observe(sid, "OUTAGE", moved, outage_count=2)
    tail = b.after(sid, 1)
    assert [e.type for e in tail] == ["outage_end", "outage_start"]
    assert tail[0].data["deadline_at"] == moved["deadline_at"]  # the first one's credit
    # a whole outage between two polls: its end, with the moved deadline
    b.observe(sid, "ACTIVE", {}, outage_count=2)
    n = len(b.after(sid, 0))
    assert (
        b.observe(sid, "ACTIVE", {"deadline_at": "x"}, outage_count=3).type
        == "outage_end"
    )  # type: ignore[union-attr]
    assert len(b.after(sid, 0)) == n + 1
    # an unknown count (an observation without one) never invents a change
    assert b.observe(sid, "ACTIVE", {}) is None


def test_an_older_read_than_the_last_observation_is_ignored() -> None:
    """The turn path observes OUTAGE (count 1) as it commits; a poll read just before it (ACTIVE,
    count 0) arriving later is older, so it publishes no spurious outage_end."""
    b = EventBroker()
    sid = uuid.uuid4()
    b.observe(sid, "ACTIVE", {}, outage_count=0)
    assert b.observe(sid, "OUTAGE", {}, outage_count=1) is not None
    assert b.observe(sid, "ACTIVE", {}, outage_count=0) is None
    assert [e.type for e in b.after(sid, 0)] == ["outage_start"]
    # the real end still comes: same count, state ACTIVE
    assert b.observe(sid, "ACTIVE", {}, outage_count=1).type == "outage_end"  # type: ignore[union-attr]


def test_the_cursor_is_the_last_event_id_and_creates_no_channel() -> None:
    b = EventBroker()
    sid = uuid.uuid4()
    assert b.cursor(sid) == 0 and sid not in b
    b.publish(sid, "turn_accepted")
    b.publish(sid, "warning", {"minutes": 5})
    assert b.cursor(sid) == 2
    assert b.after(sid, b.cursor(sid)) == []  # a stream from the cursor replays nothing


async def test_wait_wakes_on_publish_and_times_out_otherwise() -> None:
    b = EventBroker()
    sid = uuid.uuid4()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await b.wait(sid, 0, 0.05)
    assert loop.time() - t0 >= 0.04
    waiter = asyncio.create_task(b.wait(sid, 0, 5))
    await asyncio.sleep(0.01)
    b.publish(sid, "turn_accepted")
    await asyncio.wait_for(waiter, 1)


def test_evict_and_idle_sweep() -> None:
    t = [0.0]
    b = EventBroker(idle_ttl_s=10, clock=lambda: t[0])
    a, c = uuid.uuid4(), uuid.uuid4()
    b.publish(a, "turn_accepted")
    b.publish(c, "turn_accepted")
    b.evict(a)
    assert a not in b and c in b
    t[0] = 11
    assert b.sweep_idle() == 1 and c not in b


def test_warnings_are_published_once_per_threshold_and_deadline() -> None:
    """M3.2: two pollers, one warning; a credited deadline warns again, never at the old one."""
    from datetime import UTC, datetime, timedelta

    b = EventBroker()
    sid = uuid.uuid4()
    t0 = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
    d = t0 + timedelta(minutes=30)
    at5 = d - timedelta(minutes=5)
    assert [e.data["minutes"] for e in b.warn(sid, d, at5, 1800)] == [5]
    assert b.warn(sid, d, at5 + timedelta(seconds=1), 1800) == []  # a second tab polls
    credited = d + timedelta(minutes=3)
    # the old 5-minute moment against the new deadline: nothing
    assert b.warn(sid, credited, at5 + timedelta(seconds=2), 1800) == []
    new5 = credited - timedelta(minutes=5)
    assert [e.data["minutes"] for e in b.warn(sid, credited, new5, 1800)] == [5]
    assert [e.type for e in b.after(sid, 0)] == ["warning", "warning"]


def test_the_outage_reason_is_a_closed_pair() -> None:
    """Live E2E follow-up: the open interval's ``reason_code`` tells a configuration refusal
    apart; any other stored value (older intervals carry the call outcome) is an outage."""
    from vektra_assess.provider_alerts import outage_reason

    assert outage_reason("service_misconfigured") == "service_misconfigured"
    for code in (None, "error", "first_token_timeout", "provider_unavailable", "x"):
        assert outage_reason(code) == "provider_unavailable"
