"""Shutdown sequence and signal handling (plan assess-job-loop, SIGTERM task)."""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

import pytest

from vektra_assess.worker.shutdown import install_signal_handlers, shutdown


class _Rec:
    def __init__(self) -> None:
        self.calls: list[str] = []


class Runtime(_Rec):
    def __init__(self, order: list[str], drained: int = 2, fail: bool = False) -> None:
        self.order, self.drained, self.fail = order, drained, fail

    async def stop(self) -> None:
        self.order.append("runtime.stop")

    async def drain(self, timeout_s: float) -> int:
        self.order.append(f"runtime.drain({round(timeout_s):g})")
        if self.fail:
            raise RuntimeError("db gone")
        return self.drained


class Scheduler:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def stop(self) -> None:
        self.order.append("ticks.stop")


class Heartbeat:
    def __init__(self, order: list[str], fail: bool = False) -> None:
        self.order, self.fail, self.state = order, fail, "ready"

    def set_state(self, state: Any) -> None:
        self.state = state

    async def beat(self) -> None:
        self.order.append(f"beat:{self.state}")
        if self.fail:
            raise ConnectionError("db down")


class Control:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def stop(self) -> None:
        self.order.append("control.stop")


async def _done() -> None:
    return None


async def test_sequence_is_draining_stop_claims_ticks_drain_stopped_close() -> None:
    order: list[str] = []
    run_task = asyncio.create_task(_done())
    released = await shutdown(
        runtime=Runtime(order),
        run_task=run_task,
        scheduler=Scheduler(order),
        heartbeat=Heartbeat(order),
        control=Control(order),
        drain_s=20,
        clock=lambda: 0.0,
    )
    assert released == 2
    assert order == [
        "beat:draining", "runtime.stop", "ticks.stop", "runtime.drain(19)", "beat:stopped", "control.stop",
    ]  # fmt: skip


async def test_every_step_is_guarded_so_the_process_can_still_exit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    order: list[str] = []
    run_task = asyncio.create_task(_done())
    released = await shutdown(
        runtime=Runtime(order, fail=True),
        run_task=run_task,
        scheduler=Scheduler(order),
        heartbeat=Heartbeat(order, fail=True),
        control=Control(order),
        drain_s=1,
    )
    assert released == 0
    assert order[-1] == "control.stop", (
        "the control connection is closed whatever failed"
    )
    assert "assess_shutdown_step_failed" in caplog.text


async def test_sigterm_and_sigint_set_the_event_once_and_a_second_signal_is_ignored() -> (
    None
):
    stop = asyncio.Event()
    remove = install_signal_handlers(stop)
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        async with asyncio.timeout(2):
            await stop.wait()
        os.kill(os.getpid(), signal.SIGINT)  # during the drain: logged, not fatal
        await asyncio.sleep(0.05)
        assert stop.is_set()
    finally:
        remove()


async def test_a_hung_heartbeat_does_not_eat_the_grace_period() -> None:
    """Review finding: one hung call on a half-open database must not delay the drain."""
    order: list[str] = []

    class Hung(Heartbeat):
        async def beat(self) -> None:
            order.append(f"beat:{self.state}")
            await asyncio.sleep(3600)

    drained: list[float] = []

    class Rt(Runtime):
        async def drain(self, timeout_s: float) -> int:
            drained.append(timeout_s)
            return 0

    run_task = asyncio.create_task(_done())
    t0 = asyncio.get_running_loop().time()
    await shutdown(
        runtime=Rt(order),
        run_task=run_task,
        scheduler=Scheduler(order),
        heartbeat=Hung(order),
        control=Control(order),
        drain_s=20,
    )
    elapsed = asyncio.get_running_loop().time() - t0
    assert elapsed < 8, f"two hung beats cost {elapsed:.1f}s: each step is bounded"
    assert order[-1] == "control.stop"
    assert drained and drained[0] <= 20


async def test_the_drain_gets_only_what_is_left_of_the_grace_period() -> None:
    now = [0.0]

    class Rt(Runtime):
        async def drain(self, timeout_s: float) -> int:
            self.budget = timeout_s
            return 0

    rt = Rt([])
    run_task = asyncio.create_task(_done())

    class SlowTicks(Scheduler):
        async def stop(self) -> None:
            now[0] += 12.0  # earlier steps consumed 12 s of the 28 s grace

    await shutdown(
        runtime=rt,
        run_task=run_task,
        scheduler=SlowTicks([]),
        heartbeat=Heartbeat([]),
        control=Control([]),
        drain_s=20,
        grace_s=28.0,
        clock=lambda: now[0],
    )
    # 28 - 12 elapsed - 6 reserved = 10 s for the whole drain, 3 s of it kept for cancel and release
    assert rt.budget == 7.0


async def test_a_drain_below_one_second_is_honoured_and_a_stuck_drain_hits_the_deadline() -> (
    None
):
    class Rt(Runtime):
        async def drain(self, timeout_s: float) -> int:
            self.budget = timeout_s
            return 0

    rt = Rt([])
    await shutdown(
        runtime=rt,
        run_task=asyncio.create_task(_done()),
        scheduler=Scheduler([]),
        heartbeat=Heartbeat([]),
        control=Control([]),
        drain_s=0.3,
    )
    assert rt.budget == 0.3, "no one-second floor"

    class Stuck(Runtime):
        async def drain(self, timeout_s: float) -> int:
            await asyncio.sleep(3600)  # a release waiting on a dead connection
            return 0

    order: list[str] = []
    t0 = asyncio.get_running_loop().time()
    await shutdown(
        runtime=Stuck(order),
        run_task=asyncio.create_task(_done()),
        scheduler=Scheduler(order),
        heartbeat=Heartbeat(order),
        control=Control(order),
        drain_s=20,
        grace_s=8.0,  # 8 - 6 reserved = 2 s for the whole drain
    )
    assert asyncio.get_running_loop().time() - t0 < 6, "the drain is under one deadline"
    assert order[-1] == "control.stop"
