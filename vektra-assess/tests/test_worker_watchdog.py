"""Liveness watchdog (plan assess-job-loop, chaos findings): exit when the heartbeat goes stale."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from vektra_assess.worker.watchdog import EXIT_WEDGED, watchdog


@dataclass
class _Beat:
    state: str = "ready"
    last_ok: float = 0.0


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


async def _run(
    beat: _Beat, clock: _Clock, wedged: list[int], stop: asyncio.Event
) -> asyncio.Task[None]:
    return asyncio.create_task(
        watchdog(
            beat,
            stop,
            exit_after_s=60,
            check_every_s=0.01,
            on_wedge=lambda: wedged.append(1),
            clock=clock,
        )
    )


async def test_a_fresh_heartbeat_never_triggers_the_exit() -> None:
    beat, clock, wedged, stop = _Beat(last_ok=100.0), _Clock(), [], asyncio.Event()
    task = await _run(beat, clock, wedged, stop)
    for _ in range(5):
        clock.now += 50  # time passes, but every beat refreshes last_ok in time
        beat.last_ok = clock.now
        await asyncio.sleep(0.03)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert wedged == []


async def test_a_stale_heartbeat_while_ready_exits_once() -> None:
    beat, clock, wedged, stop = _Beat(last_ok=100.0), _Clock(), [], asyncio.Event()
    task = await _run(beat, clock, wedged, stop)
    clock.now = 100.0 + 61
    await asyncio.wait_for(task, 1)
    assert wedged == [1], "called exactly once, then the watchdog is done"


async def test_other_states_are_not_judged_here() -> None:
    for state in ("starting", "draining", "stopped"):
        beat, clock, wedged, stop = (
            _Beat(state=state, last_ok=0.0),
            _Clock(),
            [],
            asyncio.Event(),
        )
        task = await _run(beat, clock, wedged, stop)
        clock.now = 10_000
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 1)
        assert wedged == [], state


def test_the_exit_code_is_ex_software() -> None:
    assert EXIT_WEDGED == 70
