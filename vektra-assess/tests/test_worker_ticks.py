"""Tick scheduler (plan assess-job-loop B6) against a fake lock holder and short timers."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import pytest

from vektra_assess.worker.listener import ControlConnectionError
from vektra_assess.worker.runner import TickBody, TickSpec
from vektra_assess.worker.ticks import TickScheduler


class FakeLocks:
    def __init__(self) -> None:
        self.held: set[int] = set()
        self.elsewhere: set[int] = set()
        self.down = False
        self.history: list[tuple[str, int]] = []

    async def try_lock(self, key: int) -> bool:
        if self.down:
            raise ControlConnectionError("down")
        if key in self.elsewhere or key in self.held:
            return False
        self.held.add(key)
        self.history.append(("lock", key))
        return True

    async def unlock(self, key: int) -> None:
        if self.down:
            raise ControlConnectionError("down")
        self.held.discard(key)
        self.history.append(("unlock", key))


def _tick(name: str, body: TickBody | None, key: int, **kw: Any) -> TickSpec:
    return TickSpec(
        name,
        interval_s=kw.pop("interval_s", 0.05),
        timeout_s=kw.pop("timeout_s", 1),
        lock_key=key,
        body=body,
        **kw,
    )


async def test_a_tick_runs_under_its_lock_and_releases_it() -> None:
    locks, hits = FakeLocks(), []

    async def body() -> None:
        assert 1 in locks.held, "the lock is held while the body runs"
        hits.append(1)

    sch = TickScheduler(locks, [_tick("t", body, 1)])
    sch.start()
    await asyncio.sleep(0.2)
    await sch.stop()
    assert len(hits) >= 2
    assert locks.held == set() and locks.history[:2] == [("lock", 1), ("unlock", 1)]
    assert sch.counters[("t", "started")] == len(hits)


async def test_a_slow_tick_is_skipped_not_stacked() -> None:
    locks, active, peak = FakeLocks(), [0], [0]
    gate = asyncio.Event()

    async def slow() -> None:
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await gate.wait()
        active[0] -= 1

    sch = TickScheduler(locks, [_tick("slow", slow, 1, timeout_s=5)])
    sch.start()
    await asyncio.sleep(0.3)
    assert sch.counters[("slow", "skipped_in_flight")] >= 3
    assert peak[0] == 1, "never two runs of the same tick"
    gate.set()
    await sch.stop()


async def test_a_lock_held_elsewhere_skips_the_run() -> None:
    locks, hits = FakeLocks(), []
    locks.elsewhere.add(1)

    async def body() -> None:
        hits.append(1)

    sch = TickScheduler(locks, [_tick("t", body, 1)])
    sch.start()
    await asyncio.sleep(0.2)
    assert hits == [] and sch.counters[("t", "skipped_locked")] >= 2
    locks.elsewhere.clear()
    await asyncio.sleep(0.15)
    await sch.stop()
    assert hits, "runs as soon as the other owner lets go"


async def test_control_connection_down_skips_and_resumes() -> None:
    locks, hits = FakeLocks(), []

    async def body() -> None:
        hits.append(1)

    locks.down = True
    sch = TickScheduler(locks, [_tick("t", body, 1)])
    sch.start()
    await asyncio.sleep(0.2)
    assert hits == [] and sch.counters[("t", "skipped_control_down")] >= 2
    locks.down = False
    await asyncio.sleep(0.15)
    await sch.stop()
    assert hits


async def test_cancel_running_stops_every_in_flight_tick() -> None:
    locks, cancelled = FakeLocks(), []

    async def long() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise

    sch = TickScheduler(
        locks, [_tick("a", long, 1, timeout_s=60), _tick("b", long, 2, timeout_s=60)]
    )
    sch.start()
    await asyncio.sleep(0.15)
    assert sch.running == {"a", "b"}
    assert await sch.cancel_running() == 2
    assert len(cancelled) == 2 and sch.running == frozenset()
    await sch.stop()


async def test_timeout_and_failure_are_counted_and_the_lock_is_released(
    caplog: pytest.LogCaptureFixture,
) -> None:
    locks = FakeLocks()

    async def hang() -> None:
        await asyncio.sleep(30)

    async def boom() -> None:
        raise ValueError("student 4711 private text")

    sch = TickScheduler(
        locks, [_tick("hang", hang, 1, timeout_s=0.05), _tick("boom", boom, 2)]
    )
    with caplog.at_level(logging.WARNING, logger="vektra_assess.worker"):
        sch.start()
        await asyncio.sleep(0.3)
        await sch.stop()
    assert (
        sch.counters[("hang", "timed_out")] >= 1
        and sch.counters[("boom", "failed")] >= 1
    )
    assert locks.held == set()
    failed = [r for r in caplog.records if r.getMessage() == "assess_tick_failed"]
    assert failed and all(r.__dict__["error_type"] == "ValueError" for r in failed)
    assert all("4711" not in repr(r.__dict__) for r in caplog.records), (
        "no exception text in logs"
    )


async def test_startup_ticks_run_once_in_order_and_are_awaited() -> None:
    locks, order = FakeLocks(), []

    def mk(label: str) -> TickBody:
        async def body() -> None:
            await asyncio.sleep(0.02)
            order.append(label)

        return body

    sch = TickScheduler(
        locks,
        [
            _tick("first", mk("first"), 1, run_on_startup=True, interval_s=60),
            _tick("skipme", mk("skipme"), 2, interval_s=60),
            _tick("second", mk("second"), 3, run_on_startup=True, interval_s=60),
        ],
    )
    await sch.run_startup_ticks()
    assert order == ["first", "second"]
    await sch.stop()


async def test_jitter_spreads_the_first_fire() -> None:
    locks, stamps = FakeLocks(), []
    loop = asyncio.get_running_loop()

    async def body() -> None:
        stamps.append(loop.time())

    sch = TickScheduler(
        locks,
        [_tick("t", body, 1, interval_s=0.1, jitter_s=0.09)],
        rng=random.Random(5),
    )
    t0 = loop.time()
    sch.start()
    await asyncio.sleep(0.5)
    await sch.stop()
    gaps = [b - a for a, b in zip([t0, *stamps], stamps)]
    assert all(0.09 <= g <= 0.3 for g in gaps), gaps
    assert len({round(g, 2) for g in gaps}) > 1, "jitter makes the gaps differ"


def test_duplicate_names_or_keys_are_rejected() -> None:
    async def body() -> None: ...

    with pytest.raises(ValueError, match="duplicate tick name"):
        TickScheduler(FakeLocks(), [_tick("t", body, 1), _tick("t", body, 2)])
    with pytest.raises(ValueError, match="share one advisory lock key"):
        TickScheduler(FakeLocks(), [_tick("a", body, 1), _tick("b", body, 1)])


async def test_stats_sum_skips_into_tick_skipped_for_the_heartbeat_row() -> None:
    locks = FakeLocks()
    locks.elsewhere.add(1)

    async def body() -> None: ...

    sch = TickScheduler(locks, [_tick("t", body, 1)])
    sch.start()
    await asyncio.sleep(0.2)
    await sch.stop()
    ticks = sch.stats()["ticks"]
    assert ticks["t"]["tick_skipped"] >= 2 and "skipped_locked" not in ticks["t"]


async def test_unbound_ticks_are_not_scheduled_and_never_counted_as_failures() -> None:
    locks = FakeLocks()
    bound_hits: list[int] = []

    async def body() -> None:
        bound_hits.append(1)

    sch = TickScheduler(
        locks, [_tick("bound", body, 1), _tick("unbound", None, 2, run_on_startup=True)]
    )
    sch.start()
    await sch.run_startup_ticks()
    await asyncio.sleep(0.2)
    await sch.stop()
    assert bound_hits
    assert (
        sch.counters[("unbound", "failed")] == 0
        and sch.counters[("unbound", "started")] == 0
    )
