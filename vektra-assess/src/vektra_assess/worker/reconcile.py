"""Startup reconciliation: put the job table back in a known state before the first claim.

Runs once at startup, under the ``reconcile`` advisory lock, so two workers starting together (an
accidental second replica, a restart overlapping a slow shutdown) do not both rewrite the table;
the second waits for the lock and then finds nothing left to do. The pass is idempotent.

Steps, in this order:

1. expired leases with attempts left go back to ``ready`` (a worker that was killed mid-job);
2. expired leases with **no attempt left** are marked ``dead``, **audit first**: the event is
   written for each job before the row is changed, and a job whose audit write fails stays
   ``leased`` and is retried by the next pass. A dead job without a trail would be a silent loss;
3. every tick with ``run_on_startup`` runs once, in registration order, and is awaited.

No job is claimed until this returns (the composition root starts the claim loop afterwards).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

from vektra_assess.worker import locks, sql
from vektra_assess.worker.runtime import Audit

log = logging.getLogger("vektra_assess.worker")


class ReconcileError(RuntimeError):
    """The pass could not take its lock in time; the worker refuses to start."""


class _Locks(Protocol):
    async def try_lock(self, key: int) -> bool: ...

    async def unlock(self, key: int) -> None: ...


class _StartupTicks(Protocol):
    async def run_startup_ticks(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    requeued: int
    marked_dead: int
    audit_failed: int


async def reconcile(
    control: _Locks,
    acquire: Callable[[], AbstractAsyncContextManager[sql.Conn]],
    audit: Audit,
    ticks: _StartupTicks | None = None,
    *,
    lock_timeout_s: float = 30.0,
    poll_s: float = 0.2,
) -> ReconcileReport:
    """Run the pass under the ``reconcile`` lock; raise ``ReconcileError`` if it stays taken."""
    try:
        async with asyncio.timeout(lock_timeout_s):
            while not await control.try_lock(locks.RECONCILE):
                await asyncio.sleep(poll_s)
    except TimeoutError:
        raise ReconcileError(
            f"another worker has held the reconcile lock for {lock_timeout_s:g} s"
        ) from None
    try:
        async with acquire() as conn:
            requeued = await sql.requeue_expired(conn)
            dead = failed = 0
            for job_id, kind, attempts in await sql.list_exhausted_expired(conn):
                try:
                    await audit(
                        "assess_job_dead",
                        {
                            "job_id": job_id,
                            "kind": kind.value,
                            "attempts": attempts,
                            "error_code": "lease_expired_exhausted",
                        },
                    )
                except Exception:
                    failed += 1
                    log.exception(
                        "assess_reconcile_audit_failed", extra={"job_id": job_id}
                    )
                    continue  # stays leased; the next pass retries
                if await sql.kill_exhausted(conn, job_id):
                    dead += 1
        if ticks is not None:
            await ticks.run_startup_ticks()
        report = ReconcileReport(requeued, dead, failed)
        log.info(
            "assess_reconciled",
            extra={"requeued": requeued, "marked_dead": dead, "audit_failed": failed},
        )
        return report
    finally:
        await control.unlock(locks.RECONCILE)
