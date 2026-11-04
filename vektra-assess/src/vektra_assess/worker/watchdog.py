"""Liveness watchdog: exit when the worker can no longer prove it reaches the database.

A database that restarts, or a network that drops, can leave connections half-open: no FIN, no
error, only silence. The process stays alive, its event loop runs, and nothing gets done. Every
call has a timeout, but the drivers' own cancellation of a timed-out command can itself wait on
the dead peer (asyncpg opens a cancel connection and waits for it, unbounded), so no single
timeout is a complete answer.

The backstop is blunt and standard: if the heartbeat row has not been written for
``VEKTRA_ASSESS_WORKER_WEDGE_EXIT_S`` while the worker should be ``ready``, log it and exit with
``EXIT_WEDGED``. The supervisor (``restart: unless-stopped``) starts a fresh process; its startup
reconciliation requeues whatever the dead one held, and fenced, idempotent handlers make the
re-run safe. The exit is ``os._exit`` on purpose: a wedged process cannot drain, and draining would
wait on the very connections that are dead.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from typing import Protocol

log = logging.getLogger("vektra_assess.worker")

EXIT_WEDGED = 70  # EX_SOFTWARE


class _Beating(Protocol):
    state: str
    last_ok: float


def _hard_exit() -> None:
    log.error("assess_worker_wedged_exiting", extra={"exit_code": EXIT_WEDGED})
    for handler in logging.getLogger().handlers:
        handler.flush()
    os._exit(EXIT_WEDGED)


async def watchdog(
    heartbeat: _Beating,
    stop: asyncio.Event,
    *,
    exit_after_s: float,
    check_every_s: float | None = None,
    on_wedge: Callable[[], None] = _hard_exit,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Run until ``stop``; call ``on_wedge`` once when the heartbeat is stale while ready."""
    every = (
        check_every_s
        if check_every_s is not None
        else max(0.5, min(5.0, exit_after_s / 4))
    )
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
            return
        except TimeoutError:
            pass
        # starting, draining and stopped are judged elsewhere (startup refusal, shutdown timeout)
        if heartbeat.state == "ready" and clock() - heartbeat.last_ok > exit_after_s:
            on_wedge()
            return
