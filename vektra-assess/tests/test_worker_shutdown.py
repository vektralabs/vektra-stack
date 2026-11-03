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
        self.order.append(f"runtime.drain({timeout_s:g})")
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
    )
    assert released == 2
    assert order == [
        "beat:draining", "runtime.stop", "ticks.stop", "runtime.drain(20)", "beat:stopped", "control.stop",
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
