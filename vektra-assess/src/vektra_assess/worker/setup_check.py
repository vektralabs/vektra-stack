"""The REQ-079 setup check on the job loop: the ``setup_check`` handler and the daily tick.

Plan assess-moodle-ws task F4, carried over to plan assess-job-loop and bound on 2026-11-08.
The check itself is ``vektra_assess.moodle.setup_check.run_setup_check``; this module adds what
the job loop needs around it:

- **handler** (kind ``setup_check``, pool ``setup``, hard timeout 120 s from ``worker/policy.py``):
  ids only in ``args`` (``site_id``, optional ``report_id``). It re-validates the rows before
  acting: an unknown or disabled site (its queued report is closed ``failed``), or a report
  that is unknown, of another site or already finished
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
from vektra_assess.moodle.setup_check import (
    ItemRegistry,
    SetupCheckStale,
    run_setup_check,
)
from vektra_assess.worker.policy import KIND_DEFAULTS
from vektra_assess.worker.runner import AcquireDb, JobContext

log = structlog.get_logger(__name__)

#: reports kept per site (plan assess-moodle-ws, data lifecycle: "keeps last 30 per site")
KEEP_REPORTS = 30

_SITES = text("SELECT id FROM assess_sites WHERE disabled_at IS NULL ORDER BY id")
_SITE = text(
    "SELECT disabled_at IS NOT NULL AS disabled FROM assess_sites WHERE id = :s"
)
_CLOSE_REPORT = text(
    "UPDATE assess_setup_check_reports SET status = 'failed', finished_at = now() "
    "WHERE id = :r AND site_id = :s AND status IN ('queued', 'running')"
)
_REPORT = text(
    "SELECT 1 FROM assess_setup_check_reports "
    "WHERE id = :r AND site_id = :s AND status IN ('queued', 'running')"
)
#: only finished reports count towards the 30 kept: a queued or running one never takes a place
_TRIM = text(
    "DELETE FROM assess_setup_check_reports WHERE site_id = :s "
    "AND status IN ('done', 'failed') AND id NOT IN ("
    "SELECT id FROM assess_setup_check_reports WHERE site_id = :s "
    "AND status IN ('done', 'failed') ORDER BY created_at DESC, id DESC LIMIT :n)"
)
#: the per-item rows follow the same keep-30 rule: one run writes all its rows in one
#: transaction, so they share ``run_at``; the rows of the 30 newest runs stay
_TRIM_ITEMS = text(
    "DELETE FROM assess_setup_checks WHERE site_id = :s AND run_at < ("
    "SELECT min(run_at) FROM (SELECT DISTINCT run_at FROM assess_setup_checks "
    "WHERE site_id = :s ORDER BY run_at DESC LIMIT :n) newest)"
)
#: a ``running`` report older than the job's lease belongs to a run that is dead: a worker killed
#: mid-run (no ``except`` ran) leaves it so, and the retry of a job without ``report_id`` writes a
#: new one. ``created_at`` is not the start of the run (an API report may wait queued first), so
#: age alone is not enough: nothing is failed while another ``setup_check`` job of the site holds
#: a live lease (its report may be that old one, now running).
_FAIL_ORPHANS = text(
    "UPDATE assess_setup_check_reports SET status = 'failed', finished_at = now() "
    "WHERE site_id = :s AND status = 'running' "
    "AND created_at < now() - make_interval(secs => :after) "
    "AND id IS DISTINCT FROM :own "
    # lease ownership, not age alone (CodeRabbit 4506450159): while another setup_check job
    # of this site holds a live lease, its report may be the old running one; leave them all
    "AND NOT EXISTS (SELECT 1 FROM assess_jobs j WHERE j.kind = 'setup_check' "
    "AND j.state = 'leased' AND j.lease_until > now() AND j.id <> :job "
    "AND j.args->>'site_id' = CAST(:s AS text))"
)
#: the default lease of the ``setup_check`` kind; the composition passes the loaded policy's
ORPHAN_AFTER_S = KIND_DEFAULTS[JobKind.SETUP_CHECK].lease_s


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
    """A job argument as a UUID, None when it is not one (a malformed job is stale)."""
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
    orphan_after_s: float = ORPHAN_AFTER_S,
) -> Callable[[JobContext], Awaitable[None]]:
    """``engine`` is the worker's engine: ``run_setup_check`` owns its transactions on it."""

    async def handle(ctx: JobContext) -> None:
        """One setup-check job: re-validate the ids and rows, run, trim."""
        site_id = _uuid(ctx.args.get("site_id"))
        raw_report = ctx.args.get("report_id")
        report_id = None if raw_report is None else _uuid(raw_report)
        if site_id is None or (raw_report is not None and report_id is None):
            log.warning("assess_setup_check_stale", reason_code="malformed_args")
            return
        async with ctx.acquire() as conn:
            site = (await conn.execute(_SITE, {"s": site_id})).first()
            if site is None:
                stale = "unknown_site"
            elif site.disabled:
                # never a Moodle call, an audit row or a matrix write for a disabled site; a
                # report the API queued before the site was disabled is closed, not left queued
                stale = "site_disabled"
                if report_id is not None:
                    await conn.execute(_CLOSE_REPORT, {"r": report_id, "s": site_id})
                    await conn.commit()
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
        async with ctx.acquire() as conn:
            async with conn.begin():
                await conn.execute(
                    _FAIL_ORPHANS,
                    {
                        "s": site_id,
                        "after": orphan_after_s,
                        "own": report_id,
                        "job": ctx.job_id,
                    },
                )
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
        except SetupCheckStale as exc:
            # the site or the report changed between the check above and the claim inside,
            # or the site was disabled while the check ran
            log.info(
                "assess_setup_check_stale",
                site_id=str(site_id),
                reason_code=exc.reason_code,
            )
            return
        async with ctx.acquire() as conn:
            async with conn.begin():
                await conn.execute(_TRIM, {"s": site_id, "n": KEEP_REPORTS})
                await conn.execute(_TRIM_ITEMS, {"s": site_id, "n": KEEP_REPORTS})

    return handle
