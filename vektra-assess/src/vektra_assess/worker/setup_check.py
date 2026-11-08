"""The REQ-079 setup check on the job loop: the ``setup_check`` handler and the daily tick.

Plan assess-moodle-ws task F4, carried over to plan assess-job-loop and bound on 2026-11-08.
The check itself is ``vektra_assess.moodle.setup_check.run_setup_check``; this module adds what
the job loop needs around it:

- **handler** (kind ``setup_check``, pool ``setup``, hard timeout 120 s from ``worker/policy.py``):
  ids only in ``args`` (``site_id``, optional ``report_id``). It re-validates the rows before
  acting: an unknown site, or a report that is unknown, of another site or already finished
  (a retry after the run marked it ``failed``) is a stale job, completed without acting. A job
  without ``report_id`` (the daily tick, ``observe_release`` on a release change) writes its own
  report. After a run the site keeps its last ``KEEP_REPORTS`` reports (plan data lifecycle);
  a queued or running report is never trimmed.
- **tick** (daily, also on startup): one ``setup_check`` job per enabled site, with the same
  ``dedupe_key`` as the release-change enqueue, so a pending check is never queued twice. The
  tick only queues: the Moodle calls run in the handler's own pool, under its own timeout.

The capability matrix (``assess_site_capabilities``) is written by the probes of every run
(``run_probes`` + ``persist_matrix``): this check is what keeps the 26 h freshness of the flags.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from vektra_assess.config import VektraAssessSettings
from vektra_assess.jobs import JobKind, enqueue_job
from vektra_assess.moodle.client import MoodleWsClient
from vektra_assess.moodle.setup_check import ItemRegistry, run_setup_check
from vektra_assess.worker.runner import AcquireDb, JobContext

log = structlog.get_logger(__name__)

#: reports kept per site (plan assess-moodle-ws, data lifecycle: "keeps last 30 per site")
KEEP_REPORTS = 30

_SITES = text("SELECT id FROM assess_sites WHERE disabled_at IS NULL ORDER BY id")
_SITE = text("SELECT 1 FROM assess_sites WHERE id = :s")
_REPORT = text(
    "SELECT 1 FROM assess_setup_check_reports "
    "WHERE id = :r AND site_id = :s AND status IN ('queued', 'running')"
)
_TRIM = text(
    "DELETE FROM assess_setup_check_reports WHERE site_id = :s "
    "AND status IN ('done', 'failed') AND id NOT IN ("
    "SELECT id FROM assess_setup_check_reports WHERE site_id = :s "
    "ORDER BY created_at DESC, id DESC LIMIT :n)"
)


def dedupe_key(site_id: object) -> str:
    """The key ``capabilities.observe_release`` uses too: one pending check per site."""
    return f"setup_check:{site_id}"


async def queue_setup_checks(acquire: AcquireDb) -> int:
    """The daily tick body: one deduplicated ``setup_check`` job per enabled site."""
    queued = 0
    async with acquire() as conn:
        async with conn.begin():
            for (site_id,) in (await conn.execute(_SITES)).all():
                queued += await enqueue_job(
                    conn,
                    JobKind.SETUP_CHECK,
                    {"site_id": str(site_id)},
                    dedupe_key=dedupe_key(site_id),
                )
    if queued:
        log.info("assess_setup_checks_queued", count=queued)
    return queued


def _uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except ValueError:
        return None


def make_setup_check_handler(
    engine: AsyncEngine,
    client: MoodleWsClient,
    settings: VektraAssessSettings,
    *,
    registry: ItemRegistry | None = None,
    now: Callable[[], int] | None = None,
) -> Callable[[JobContext], Awaitable[None]]:
    """``engine`` is the worker's engine: ``run_setup_check`` owns its transactions on it."""

    async def handle(ctx: JobContext) -> None:
        site_id = _uuid(ctx.args.get("site_id"))
        raw_report = ctx.args.get("report_id")
        report_id = None if raw_report is None else _uuid(raw_report)
        if site_id is None or (raw_report is not None and report_id is None):
            log.warning("assess_setup_check_stale", reason_code="malformed_args")
            return
        async with ctx.acquire() as conn:
            if (await conn.execute(_SITE, {"s": site_id})).first() is None:
                stale = "unknown_site"
            elif (
                report_id is not None
                and (
                    await conn.execute(_REPORT, {"r": report_id, "s": site_id})
                ).first()
                is None
            ):
                stale = "report_not_runnable"
            else:
                stale = None
        if stale is not None:
            log.info(
                "assess_setup_check_stale", site_id=str(site_id), reason_code=stale
            )
            return
        try:
            await run_setup_check(
                engine,
                site_id=site_id,
                client=client,
                settings=settings,
                registry=registry,
                report_id=report_id,
                now=None if now is None else now(),
            )
        except LookupError:
            # the site or the report changed between the check above and the claim inside
            log.info(
                "assess_setup_check_stale",
                site_id=str(site_id),
                reason_code="report_not_runnable",
            )
            return
        async with ctx.acquire() as conn:
            async with conn.begin():
                await conn.execute(_TRIM, {"s": site_id, "n": KEEP_REPORTS})

    return handle
