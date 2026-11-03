"""Tick scheduler: periodic work outside every job pool (COMP-003).

A tick fires every ``interval_s`` plus a random ``0..jitter_s`` (so several workers or ticks do not
beat in step). Each fire is **skipped, never queued**, when:

- the previous run of the same tick is still in flight (the timer keeps running, a slow tick is
  not stacked on itself), or
- its advisory lock is held elsewhere (another process owns this tick), or
- the control connection is down (no lock can be taken, so nothing is fenced).

A run has its own hard timeout and failures are logged by type only. When the control connection
is lost the server drops every session lock, so each running tick is **cancelled** (``cancel_running``):
it is no longer fenced and must not keep writing. Ticks resume on their next schedule after the
reconnect. Counters (``started``, ``skipped``, ``failed``, ``timed_out``) are per tick name and are
exposed through the heartbeat row.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
from collections import Counter
from collections.abc import Sequence
from typing import Any, Protocol

from vektra_assess.worker.listener import ControlConnectionError
from vektra_assess.worker.runner import TickSpec

log = logging.getLogger("vektra_assess.worker")
_RNG = random.Random()


class _Locks(Protocol):
    async def try_lock(self, key: int) -> bool: ...

    async def unlock(self, key: int) -> None: ...


class TickScheduler:
    def __init__(
        self,
        control: _Locks,
        ticks: Sequence[TickSpec],
        *,
        rng: random.Random | None = None,
    ) -> None:
        names = [t.name for t in ticks]
        if len(set(names)) != len(names):
            raise ValueError("duplicate tick name")
        keys = [t.lock_key for t in ticks]
        if len(set(keys)) != len(keys):
            raise ValueError("two ticks share one advisory lock key")
        self._control = control
        self._ticks = list(ticks)
        self._rng = rng if rng is not None else _RNG
        self._loops: list[asyncio.Task[None]] = []
        self._running: dict[str, asyncio.Task[None]] = {}
        self._stopping = False
        self.counters: Counter[tuple[str, str]] = Counter()

    # --- lifecycle -------------------------------------------------------------------------

    def start(self) -> None:
        """Start one timer per tick. Startup runs (``run_on_startup``) are the caller's job."""
        for spec in self._ticks:
            self._loops.append(asyncio.create_task(self._timer(spec)))

    async def stop(self) -> None:
        self._stopping = True
        for task in self._loops:
            task.cancel()
        await asyncio.gather(*self._loops, return_exceptions=True)
        self._loops.clear()
        await self.cancel_running()

    async def cancel_running(self) -> int:
        """Cancel every in-flight run (control connection lost, or shutdown); return how many."""
        tasks = list(self._running.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    async def run_startup_ticks(self) -> None:
        """Run each ``run_on_startup`` tick once, in registration order, and wait for it."""
        for spec in self._ticks:
            if spec.run_on_startup:
                await self.fire(spec.name)
                task = self._running.get(spec.name)
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)

    @property
    def running(self) -> frozenset[str]:
        return frozenset(self._running)

    def stats(self) -> dict[str, Any]:
        """Per-tick counters for the heartbeat row; skips are summed into ``tick_skipped``."""
        out: dict[str, dict[str, int]] = {}
        for (name, event), n in self.counters.items():
            row = out.setdefault(name, {})
            key = "tick_skipped" if event.startswith("skipped_") else event
            row[key] = row.get(key, 0) + n
        return {"ticks": out}

    # --- scheduling ------------------------------------------------------------------------

    async def _timer(self, spec: TickSpec) -> None:
        while True:
            await asyncio.sleep(spec.interval_s + self._rng.uniform(0, spec.jitter_s))
            await self.fire(spec.name)

    async def fire(self, name: str) -> bool:
        """Start one run of ``name`` unless it must be skipped; True when a run started."""
        spec = next(t for t in self._ticks if t.name == name)
        if self._stopping:
            return False
        if name in self._running:
            self.counters[(name, "skipped_in_flight")] += 1
            return False
        try:
            got = await self._control.try_lock(spec.lock_key)
        except ControlConnectionError:
            self.counters[(name, "skipped_control_down")] += 1
            return False
        if not got:
            self.counters[(name, "skipped_locked")] += 1
            return False
        self.counters[(name, "started")] += 1
        task = asyncio.create_task(self._run(spec))
        self._running[name] = task
        task.add_done_callback(functools.partial(self._finished, name))
        return True

    def _finished(self, name: str, task: asyncio.Task[None]) -> None:
        if self._running.get(name) is task:
            del self._running[name]

    async def _run(self, spec: TickSpec) -> None:
        try:
            async with asyncio.timeout(spec.timeout_s):
                await spec.run()
        except asyncio.CancelledError:
            raise  # lock lost or shutting down: the caller decided, nothing to record
        except TimeoutError:
            self.counters[(spec.name, "timed_out")] += 1
            log.warning("assess_tick_timeout", extra={"tick": spec.name})
        except Exception as exc:
            self.counters[(spec.name, "failed")] += 1
            # type only: the message of a failing tick can carry content
            log.warning(
                "assess_tick_failed",
                extra={"tick": spec.name, "error_type": type(exc).__name__},
            )
        finally:
            try:
                await asyncio.shield(self._control.unlock(spec.lock_key))
            except ControlConnectionError:
                pass  # the connection is gone and so is the lock
