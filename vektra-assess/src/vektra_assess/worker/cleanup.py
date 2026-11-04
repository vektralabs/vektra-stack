"""Done-job cleanup tick: delete ``done`` rows older than the retention, in batches.

Only ``done`` rows are deleted. ``dead`` rows are evidence of a failure and stay until the purge
plan removes them. Batches keep each DELETE short (the table is hot), and a run stops after
``max_batches`` so one tick can never outlast its timeout; the next hourly run continues.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from vektra_assess.worker import sql

log = logging.getLogger("vektra_assess.worker")


async def cleanup_done_jobs(
    acquire: Callable[[], AbstractAsyncContextManager[sql.Conn]],
    retention_h: int,
    *,
    batch: int = 1000,
    max_batches: int = 50,
) -> int:
    total = 0
    for _ in range(max_batches):
        async with acquire() as conn:
            deleted = await sql.cleanup_done(conn, retention_h, batch)
        total += deleted
        if deleted < batch:
            break
    if total:
        log.info(
            "assess_jobs_cleaned", extra={"deleted": total, "retention_h": retention_h}
        )
    return total
