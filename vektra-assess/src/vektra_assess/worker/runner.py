"""Worker runtime contracts: the ``JobRunner`` seam, handler and tick specs (ARCH-068).

``JobRunner`` is the Phase-2 seam: today ``WorkerRuntime`` implements it over Postgres; an arq
backend could implement it later without touching a handler. A handler is
``async def handle(ctx: JobContext) -> None`` and receives **ids only** (``ctx.args``): it must
re-read and re-validate the authoritative rows inside its own transaction before acting, and
acting on a stale job is a no-op that completes the job.

A slot (a handler kind or a tick) is registered with its schedule, budget and lease even when
no body exists yet. An unbound slot raises ``HandlerNotBound`` when it runs, and the worker
refuses to start while a slot is unbound and assess is enabled (fail closed, never a silent
no-op).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncConnection

from vektra_assess.jobs import JobKind

# Opens a short-lived connection with its own transaction; the handler owns commit/rollback
# boundaries through it. Handlers do external I/O outside any open transaction.
AcquireDb = Callable[[], AbstractAsyncContextManager[AsyncConnection]]


class HandlerNotBound(RuntimeError):
    """A registered slot has no body yet. Raised on use; startup refuses to proceed with one."""

    def __init__(self, slot: str) -> None:
        super().__init__(f"assess worker slot {slot!r} has no handler bound")
        self.slot = slot


@dataclass(frozen=True, slots=True)
class JobContext:
    """What a handler sees: the job's identity and ids, a DB acquire function, a logger.

    Nothing else: no row content, no session objects, no secrets. ``args`` holds ids only
    (the table rejects anything over 1 KiB); ``attempt`` is 1 on the first run.
    """

    job_id: int
    kind: JobKind
    args: Mapping[str, str | int]
    attempt: int
    acquire: AcquireDb
    log: logging.Logger | logging.LoggerAdapter[logging.Logger]


Handler = Callable[[JobContext], Awaitable[None]]
TickBody = Callable[[], Awaitable[None]]


def _check_budget(name: str, budget_s: float, lease_s: float) -> None:
    if budget_s <= 0:
        raise ValueError(f"{name}: budget_s must be positive")
    if lease_s < 2 * budget_s:
        # no lease renewal exists: a lease shorter than twice the budget lets a second worker
        # reclaim a job whose first run is still inside its own hard timeout
        raise ValueError(f"{name}: lease_s ({lease_s}) must be at least 2 x budget_s")


@dataclass(frozen=True, slots=True)
class HandlerSpec:
    """One job kind: the pool it runs in, its hard timeout and lease. ``handle=None`` = unbound."""

    kind: JobKind
    pool: str
    budget_s: float
    lease_s: float
    max_attempts: int = 5
    handle: Handler | None = None

    def __post_init__(self) -> None:
        _check_budget(self.kind.value, self.budget_s, self.lease_s)
        if self.max_attempts < 1:
            raise ValueError(f"{self.kind.value}: max_attempts must be at least 1")

    @property
    def bound(self) -> bool:
        return self.handle is not None

    async def run(self, ctx: JobContext) -> None:
        if self.handle is None:
            raise HandlerNotBound(self.kind.value)
        await self.handle(ctx)


@dataclass(frozen=True, slots=True)
class TickSpec:
    """A periodic task outside every job pool.

    Skipped (never queued) if its previous run is still in flight or ``lock_key`` is held by
    another process. ``lock_key`` is a constant from ``vektra_assess.worker.locks``.
    """

    name: str
    interval_s: float
    timeout_s: float
    lock_key: int
    jitter_s: float = 0.0
    run_on_startup: bool = False
    body: TickBody | None = None

    def __post_init__(self) -> None:
        if self.interval_s <= 0 or self.timeout_s <= 0:
            raise ValueError(f"{self.name}: interval_s and timeout_s must be positive")
        if self.jitter_s < 0 or self.jitter_s >= self.interval_s:
            raise ValueError(f"{self.name}: jitter_s must be in [0, interval_s)")

    @property
    def bound(self) -> bool:
        return self.body is not None

    async def run(self) -> None:
        if self.body is None:
            raise HandlerNotBound(self.name)
        await self.body()


@runtime_checkable
class JobRunner(Protocol):
    """The seam: register slots, run until stopped, stop cleanly."""

    def register_handler(self, spec: HandlerSpec) -> None: ...

    def register_tick(self, spec: TickSpec) -> None: ...

    async def run(self) -> None: ...

    async def stop(self) -> None: ...
