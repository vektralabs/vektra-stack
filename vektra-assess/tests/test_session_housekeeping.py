"""Review fix 3: in-memory DEKs and event rings are swept, opportunistically, on a fake clock.

The API runs with ``lifespan="off"`` (BUG-025), so there is no timer: an assess request runs the
sweep at most once every ``interval_s``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from vektra_assess.api.events import EventBroker
from vektra_assess.api.housekeeping import Housekeeper, assess_housekeeping
from vektra_assess.api.material import DekCache


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_dek_cache_sweep_drops_only_expired_keys() -> None:
    clock = Clock()
    cache = DekCache(ttl_s=10, clock=clock)
    old, new = uuid.uuid4(), uuid.uuid4()
    cache.put(old, 1, b"o" * 32)
    clock.t += 6
    cache.put(new, 1, b"n" * 32)
    clock.t += 5  # old is 11 s old, new 5 s
    assert cache.sweep() == 1
    assert len(cache) == 1 and cache.get(new, 1) is not None


def test_broker_sweeps_closed_and_rolled_back_channels_after_their_ttl_and_idle_ones() -> (
    None
):
    clock = Clock()
    b = EventBroker(idle_ttl_s=3600, closed_ttl_s=60, clock=clock)
    closed, rolled, live, idle = (uuid.uuid4() for _ in range(4))
    b.publish(closed, "closed", {})
    b.publish(rolled, "start_rolled_back", {})
    b.publish(live, "reply_delta", {"text": "x"})
    b.publish(idle, "turn_accepted", {})
    clock.t += 30
    b.publish(live, "reply_done", {})  # recent activity
    assert b.sweep_idle() == 0  # closed channels stay readable within their TTL
    clock.t += 31  # closed / rolled back 61 s ago
    assert b.sweep_idle() == 2
    assert closed not in b and rolled not in b and live in b and idle in b
    clock.t += 3600
    assert b.sweep_idle() == 2 and len(list(b.channels())) == 0


def test_the_sweep_runs_at_most_once_per_interval() -> None:
    clock = Clock()
    cache = DekCache(ttl_s=1, clock=clock)
    broker = EventBroker(idle_ttl_s=1, closed_ttl_s=1, clock=clock)
    hk = Housekeeper(interval_s=30, clock=clock)
    state = SimpleNamespace(
        assess_dek_cache=cache, assess_events=broker, assess_housekeeper=hk
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    cache.put(uuid.uuid4(), 1, b"k" * 32)
    broker.publish(uuid.uuid4(), "closed", {})
    clock.t += 5
    assess_housekeeping(request)  # type: ignore[arg-type]
    assert len(cache) == 0 and len(list(broker.channels())) == 0 and hk.runs == 1
    cache.put(uuid.uuid4(), 1, b"k" * 32)
    clock.t += 5
    assess_housekeeping(request)  # type: ignore[arg-type]
    assert hk.runs == 1 and len(cache) == 1  # within the interval: no second sweep
    clock.t += 30
    assess_housekeeping(request)  # type: ignore[arg-type]
    assert hk.runs == 2 and len(cache) == 0


def test_the_sweep_drops_rate_limiter_keys_with_an_empty_window(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Review fix 7: the assess-local limiters lose their stale keys in the same sweep."""
    import vektra_admin.rate_limit as rl

    t = [5000.0]
    monkeypatch.setattr(rl.time, "monotonic", lambda: t[0])
    limiter = rl.RateLimiter()
    limiter.check(("site", 1, 2), 30)
    limiter.check(("site", 3, 4), 30)
    t[0] += 61
    clock = Clock()
    hk = Housekeeper(interval_s=30, clock=clock)
    state = SimpleNamespace(assess_limiters={"state": limiter}, assess_housekeeper=hk)
    assess_housekeeping(SimpleNamespace(app=SimpleNamespace(state=state)))  # type: ignore[arg-type]
    assert limiter._windows == {}
