"""Graceful shutdown: SIGTERM/SIGINT, drain, fenced release, heartbeat ``stopped`` (ARCH-068).

Order, and why:

1. heartbeat ``draining`` (best effort): readiness drops now, not when the row ages out;
2. stop claiming and wait for the claim loop to return, so no new job starts during the drain;
3. stop the tick scheduler (cancels running ticks; their locks die with the control session);
4. wait up to ``drain_s`` (default 20 s, under the Compose ``stop_grace_period`` of 30 s) for
   in-flight handlers, then cancel the rest and release their leases (``ready``, fenced), so the next
   worker takes them at once;
5. heartbeat ``stopped`` (best effort), close the control connection.

Every step after the first signal is guarded: a failure is logged and the sequence continues, because
the process must still exit and a lease that was not released only expires, it is never lost.
A second signal while draining is ignored (the grace period is the hard stop).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from vektra_assess.worker.heartbeat import State

log = logging.getLogger("vektra_assess.worker")


class _Runtime(Protocol):
    async def stop(self) -> None: ...

    async def drain(self, timeout_s: float) -> int: ...


class _Scheduler(Protocol):
    async def stop(self) -> None: ...


class _Heartbeat(Protocol):
    def set_state(self, state: State) -> None: ...

    async def beat(self) -> None: ...


class _Control(Protocol):
    async def stop(self) -> None: ...


def install_signal_handlers(stop: asyncio.Event) -> Callable[[], None]:
    """Set ``stop`` on the first SIGTERM/SIGINT; return a function that removes the handlers."""
    loop = asyncio.get_running_loop()

    def _on_signal(sig: signal.Signals) -> None:
        if stop.is_set():
            log.warning("assess_worker_signal_ignored", extra={"signal": sig.name})
            return
        log.info("assess_worker_signal", extra={"signal": sig.name})
        stop.set()

    sigs = (signal.SIGTERM, signal.SIGINT)
    for sig in sigs:
        loop.add_signal_handler(sig, _on_signal, sig)

    def remove() -> None:
        for sig in sigs:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)

    return remove


async def _guarded(step: str, call: Awaitable[object], timeout_s: float) -> None:
    """Run one best-effort step with its own deadline: a hung database must not eat the grace period."""
    try:
        async with asyncio.timeout(timeout_s):
            await call
    except Exception:
        log.exception("assess_shutdown_step_failed", extra={"step": step})


async def shutdown(
    *,
    runtime: _Runtime,
    run_task: asyncio.Task[None],
    scheduler: _Scheduler,
    heartbeat: _Heartbeat,
    control: _Control,
    drain_s: float,
    grace_s: float = 28.0,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Run the shutdown sequence inside ``grace_s`` (Compose gives 30 s); return leases released.

    Every best-effort step has a short deadline of its own, and the drain gets whatever is left
    (never more than ``drain_s``) after reserving time for the closing steps. Without that, one
    hung call on a half-open database could push the drain past the grace period and Docker would
    SIGKILL the worker before it released a single lease.
    """
    started = clock()
    reserve_s = 6.0  # the final heartbeat and closing the control connection
    heartbeat.set_state("draining")
    await _guarded("heartbeat_draining", heartbeat.beat(), 2.0)
    await runtime.stop()
    await _guarded("claim_loop", asyncio.shield(run_task), 3.0)
    await _guarded("ticks", scheduler.stop(), 3.0)
    drain_budget = max(1.0, min(drain_s, grace_s - (clock() - started) - reserve_s))
    released = 0
    try:
        released = await runtime.drain(drain_budget)
    except Exception:
        log.exception("assess_shutdown_step_failed", extra={"step": "drain"})
    heartbeat.set_state("stopped")
    await _guarded("heartbeat_stopped", heartbeat.beat(), 2.0)
    await _guarded("control", control.stop(), 2.0)
    return released
