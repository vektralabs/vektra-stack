"""``WorkerRuntime``: the claim loop, per-pool concurrency, hard timeouts, retry and dead-letter.

One asyncio process. Each iteration claims **at most the free slots of each pool** (a pool is never
over-claimed, so a stall in one pool cannot starve another), runs every claimed job under
``asyncio.timeout(budget)``, and settles it through fenced statements (``worker.sql``):

- success -> ``done``;
- exception or timeout -> back to ``ready`` after an exponential, jittered, capped backoff;
- the last allowed attempt failing -> ``dead``, with a content-free audit event written first.

If that audit write fails the job is **not** marked dead: it stays ``leased`` until its lease expires,
the error is logged and the next reconciliation or claim retries the decision. A dead job that left
no audit trail would be a silent loss. Error codes stored on a job are fixed words, never exception
text, so a handler cannot leak content into ``assess_jobs.last_error_code``.

Ticks, the control connection, startup reconciliation and shutdown are separate tasks of the plan and
sit on top of this class.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from vektra_assess.jobs import JobKind
from vektra_assess.worker import sql
from vektra_assess.worker.runner import (
    AcquireDb,
    HandlerNotBound,
    HandlerSpec,
    JobContext,
    TickSpec,
)

log = logging.getLogger("vektra_assess.worker")
_RNG = random.Random()

# content-free audit hook: (event name, small dict of ids and counts). Injected so the runtime
# stays independent of the audit backend; it must raise on failure (never swallow).
Audit = Callable[[str, Mapping[str, Any]], Awaitable[None]]


class JobStore(Protocol):
    """The fenced persistence the runtime needs; ``SqlStore`` is the Postgres implementation."""

    async def claim(
        self,
        kinds: Sequence[JobKind],
        worker_id: str,
        lease_s: float | Mapping[JobKind, float],
        free: int,
        max_attempts: int | Mapping[JobKind, int] | None = None,
    ) -> list[sql.ClaimedJob]: ...

    async def complete(self, job_id: int, worker_id: str, attempt: int) -> bool: ...

    async def fail(
        self, job_id: int, worker_id: str, attempt: int, backoff_s: float, code: str
    ) -> bool: ...

    async def mark_dead(
        self, job_id: int, worker_id: str, attempt: int, code: str
    ) -> bool: ...

    async def release(self, job_id: int, worker_id: str, attempt: int) -> bool: ...


class SqlStore:
    """``JobStore`` over ``worker.sql``; each call takes a short-lived connection."""

    def __init__(
        self, acquire: Callable[[], AbstractAsyncContextManager[sql.Conn]]
    ) -> None:
        self._acquire = acquire

    async def claim(
        self,
        kinds: Sequence[JobKind],
        worker_id: str,
        lease_s: float | Mapping[JobKind, float],
        free: int,
        max_attempts: int | Mapping[JobKind, int] | None = None,
    ) -> list[sql.ClaimedJob]:
        async with self._acquire() as conn:
            return await sql.claim(conn, kinds, worker_id, lease_s, free, max_attempts)

    async def complete(self, job_id: int, worker_id: str, attempt: int) -> bool:
        async with self._acquire() as conn:
            return await sql.complete(conn, job_id, worker_id, attempt)

    async def fail(
        self, job_id: int, worker_id: str, attempt: int, backoff_s: float, code: str
    ) -> bool:
        async with self._acquire() as conn:
            return await sql.fail(conn, job_id, worker_id, attempt, backoff_s, code)

    async def mark_dead(
        self, job_id: int, worker_id: str, attempt: int, code: str
    ) -> bool:
        async with self._acquire() as conn:
            return await sql.mark_dead(conn, job_id, worker_id, attempt, code)

    async def release(self, job_id: int, worker_id: str, attempt: int) -> bool:
        async with self._acquire() as conn:
            return await sql.release(conn, job_id, worker_id, attempt)


@dataclass(frozen=True, slots=True)
class PoolSpec:
    name: str
    concurrency: int

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError(f"pool {self.name}: concurrency must be at least 1")


def backoff_s(
    attempt: int,
    *,
    base_s: float = 5.0,
    cap_s: float = 300.0,
    rng: random.Random | None = None,
) -> float:
    """Exponential backoff with jitter in [0.5, 1.0] of the step, capped. ``attempt`` is 1-based."""
    step = min(cap_s, base_s * float(2 ** max(attempt - 1, 0)))
    return step * (rng if rng is not None else _RNG).uniform(0.5, 1.0)


def _error_code(exc: BaseException) -> str:
    """A fixed word per failure class: exception text can carry content and is never stored."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, HandlerNotBound):
        return "handler_not_bound"
    return "handler_error"


class WorkerRuntime:
    """Implements ``JobRunner`` over a ``JobStore``."""

    def __init__(
        self,
        store: JobStore,
        *,
        worker_id: str,
        pools: Sequence[PoolSpec],
        acquire_db: AcquireDb,
        audit: Audit,
        poll_interval_s: float = 1.0,
        backoff_base_s: float = 5.0,
        backoff_cap_s: float = 300.0,
        rng: random.Random | None = None,
    ) -> None:
        self._store = store
        self.worker_id = worker_id
        self._pools = {p.name: p for p in pools}
        if len(self._pools) != len(pools):
            raise ValueError("duplicate pool name")
        self._in_use: Counter[str] = Counter()
        self._acquire_db = acquire_db
        self._audit = audit
        self._poll_s = poll_interval_s
        self._backoff = (backoff_base_s, backoff_cap_s)
        self._rng = rng
        self._handlers: dict[JobKind, HandlerSpec] = {}
        self._ticks: dict[str, TickSpec] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._jobs: dict[asyncio.Task[None], sql.ClaimedJob] = {}
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        # per-kind counters, exposed through the heartbeat row (no listening socket)
        self.counters: Counter[tuple[str, JobKind]] = Counter()
        self.claim_errors = 0

    # --- JobRunner -------------------------------------------------------------------------

    def register_handler(self, spec: HandlerSpec) -> None:
        if spec.pool not in self._pools:
            raise ValueError(f"{spec.kind.value}: unknown pool {spec.pool!r}")
        if spec.kind in self._handlers:
            raise ValueError(f"{spec.kind.value}: handler already registered")
        self._handlers[spec.kind] = spec

    def register_tick(self, spec: TickSpec) -> None:
        if spec.name in self._ticks:
            raise ValueError(f"tick {spec.name!r} already registered")
        self._ticks[spec.name] = spec

    async def run(self) -> None:
        """Claim and run jobs until ``stop()``. In-flight jobs are left to ``wait_idle``/drain."""
        while not self._stopping.is_set():
            # Clear BEFORE claiming: a NOTIFY or a freed slot that arrives while the claim is in
            # flight sets the event again and the wait below returns at once. Clearing after the
            # claim would erase that wake-up and sleep a full poll interval.
            self._wake.clear()
            try:
                claimed = await self.claim_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A database restart or a dropped connection must not end the loop: the pool
                # reconnects, jobs wait in the table, and the next pass claims them. The error
                # TYPE is logged, never its text.
                self.claim_errors += 1
                log.warning(
                    "assess_claim_failed", extra={"error_type": type(exc).__name__}
                )
                claimed = 0
            if claimed:
                continue  # more may be waiting: do not sleep while there is work
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_s)
            except TimeoutError:
                pass

    async def stop(self) -> None:
        self._stopping.set()
        self._wake.set()

    # --- claim loop ------------------------------------------------------------------------

    def wake(self) -> None:
        """A NOTIFY arrived: skip the rest of the poll sleep. Only a latency hint."""
        self._wake.set()

    def free_slots(self, pool: str) -> int:
        return self._pools[pool].concurrency - self._in_use[pool]

    async def claim_once(self) -> int:
        """One pass over every pool; returns how many jobs were claimed and started."""
        started = 0
        failed = False
        if self._stopping.is_set():
            return 0  # shutting down: nothing new may start
        for pool in self._pools.values():
            specs = [s for s in self._handlers.values() if s.pool == pool.name]
            free = self.free_slots(pool.name)
            if not specs or free <= 0:
                continue
            # each claimed row gets the lease of its own kind
            try:
                jobs = await self._store.claim(
                    [s.kind for s in specs],
                    self.worker_id,
                    {s.kind: s.lease_s for s in specs},
                    free,
                    {s.kind: s.max_attempts for s in specs},
                )
            except Exception as exc:
                # a failing pool must not stop the pools after it from being claimed
                self.claim_errors += 1
                failed = True
                log.warning(
                    "assess_claim_failed",
                    extra={"pool": pool.name, "error_type": type(exc).__name__},
                )
                continue
            if jobs and self._stopping.is_set():
                # Shutdown began while this claim was in flight: the drain has already taken its
                # snapshot and would not know these jobs. Give the rows back instead of starting
                # work that would keep running past the drain and then sit leased until expiry.
                for job in jobs:
                    try:
                        await self._store.release(job.id, self.worker_id, job.attempt)
                        self.counters[("released", job.kind)] += 1
                    except Exception as exc:
                        log.warning(
                            "assess_job_release_failed",
                            extra={"job_id": job.id, "error_type": type(exc).__name__},
                        )
                return started
            for job in jobs:
                self._start(job, self._handlers[job.kind])
                started += 1
        # A pass with a failed pool reports 0 so run() sleeps instead of hammering a failing
        # database while other pools keep starting jobs (started jobs are already running).
        return 0 if failed else started

    def _start(self, job: sql.ClaimedJob, spec: HandlerSpec) -> None:
        self._in_use[spec.pool] += 1
        self.counters[("claimed", job.kind)] += 1
        task = asyncio.create_task(self._run_job(job, spec))
        self._tasks.add(task)
        self._jobs[task] = job
        task.add_done_callback(self._forget)

    def _forget(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        self._jobs.pop(task, None)

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    def stats(self) -> dict[str, Any]:
        """Content-free figures for the heartbeat row: pool occupancy and per-kind counters."""
        return {
            "pools_in_use": {n: self._in_use[n] for n in self._pools},
            "claim_errors": self.claim_errors,
            "jobs": {
                f"{what}:{kind.value}": n for (what, kind), n in self.counters.items()
            },
        }

    async def drain(self, timeout_s: float) -> int:
        """Shutdown: wait up to ``timeout_s`` for in-flight jobs, cancel the rest and release them.

        Call after ``run()`` has returned (no new claim can start). A cancelled job goes back to
        ``ready`` at once (fenced; the aborted run does not count as an attempt) so the next worker
        takes it immediately instead of after the lease expires. A job that finished while we were
        cancelling matches no row on release and is simply not counted. Returns how many jobs were
        released.
        """
        tasks = set(self._tasks)
        if not tasks:
            return 0
        _, pending = await asyncio.wait(tasks, timeout=timeout_s)
        if not pending:
            return 0
        held = {t: self._jobs[t] for t in pending if t in self._jobs}
        for task in pending:
            task.cancel()
        released = 0

        async def settle(task: asyncio.Task[None]) -> None:
            # release each lease as soon as ITS task is gone: one handler slow to cancel must not
            # keep the others' leases from going back (the caller's deadline may cut this short)
            nonlocal released
            await asyncio.gather(task, return_exceptions=True)
            job = held.get(task)
            if job is None:
                return
            try:
                if await self._store.release(job.id, self.worker_id, job.attempt):
                    released += 1
                    self.counters[("released", job.kind)] += 1
            except Exception:
                # the lease expires and the job is retried: slower, never lost
                log.exception(
                    "assess_job_release_failed",
                    extra={"job_id": job.id, "kind": job.kind.value},
                )

        await asyncio.gather(*(settle(t) for t in pending))
        log.info(
            "assess_worker_drained",
            extra={"cancelled": len(pending), "released": released},
        )
        return released

    # --- one job ---------------------------------------------------------------------------

    async def _run_job(self, job: sql.ClaimedJob, spec: HandlerSpec) -> None:
        ctx = JobContext(
            job_id=job.id,
            kind=job.kind,
            args=job.args,
            attempt=job.attempt,
            acquire=self._acquire_db,
            log=logging.LoggerAdapter(log, {"job_id": job.id, "kind": job.kind.value}),
        )
        try:
            try:
                async with asyncio.timeout(spec.budget_s):
                    await spec.run(ctx)
            except asyncio.CancelledError:
                raise  # shutdown cancels in-flight jobs; releasing them is the drain's job
            except Exception as exc:
                await self._settle_failure(job, spec, _error_code(exc), exc)
            else:
                await self._settle_success(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A store or audit failure while settling: the lease expires and the job is retried.
            # The error TYPE is logged, not a traceback: it may carry data, and an outage would
            # print one per in-flight job.
            log.warning(
                "assess_job_settle_failed",
                extra={
                    "job_id": job.id,
                    "kind": job.kind.value,
                    "error_type": type(exc).__name__,
                },
            )
        finally:
            self._in_use[spec.pool] -= 1
            self._wake.set()  # a slot is free: claim again without waiting for the poll

    async def _settle_success(self, job: sql.ClaimedJob) -> None:
        if await self._store.complete(job.id, self.worker_id, job.attempt):
            self.counters[("completed", job.kind)] += 1
        else:
            self._fence_lost(job)

    async def _settle_failure(
        self, job: sql.ClaimedJob, spec: HandlerSpec, code: str, exc: BaseException
    ) -> None:
        # the exception TYPE is logged, never its text (it may carry content)
        log.warning(
            "assess_job_failed",
            extra={
                "job_id": job.id,
                "kind": job.kind.value,
                "attempt": job.attempt,
                "error_code": code,
                "error_type": type(exc).__name__,
            },
        )
        if job.attempt >= job.max_attempts:
            await self._audit(
                "assess_job_dead",
                {
                    "job_id": job.id,
                    "kind": job.kind.value,
                    "attempts": job.attempt,
                    "error_code": code,
                },
            )  # raises on failure: the job stays leased (see module docstring)
            if await self._store.mark_dead(job.id, self.worker_id, job.attempt, code):
                self.counters[("dead", job.kind)] += 1
            else:
                self._fence_lost(job)
            return
        delay = backoff_s(
            job.attempt,
            base_s=self._backoff[0],
            cap_s=self._backoff[1],
            rng=self._rng,
        )
        if await self._store.fail(job.id, self.worker_id, job.attempt, delay, code):
            self.counters[("failed", job.kind)] += 1
        else:
            self._fence_lost(job)

    def _fence_lost(self, job: sql.ClaimedJob) -> None:
        self.counters[("fence_lost", job.kind)] += 1
        log.warning(
            "assess_job_fence_lost", extra={"job_id": job.id, "kind": job.kind.value}
        )
