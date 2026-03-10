"""arq cron task for QueryTrace retention cleanup (ARCH-041).

Reads VEKTRA_ANALYTICS_RETENTION_DAYS from config. When set, deletes
QueryTrace records older than the retention period. Designed to run
alongside the soft-delete cleanup in vektra_ingest.jobs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

import vektra_shared.db as _shared_db

log = structlog.get_logger(__name__)


async def cleanup_analytics_traces_task(ctx: dict[str, Any]) -> None:
    """arq cron task: delete QueryTrace records past retention period.

    Reads VEKTRA_ANALYTICS_RETENTION_DAYS from config. When None, no
    cleanup is performed. Uses AnalyticsService.delete_before().
    """
    from vektra_shared.config import ObservabilityConfig

    config = ObservabilityConfig()
    if config.analytics_retention_days is None:
        log.debug(
            "analytics_cleanup_skipped",
            reason="VEKTRA_ANALYTICS_RETENTION_DAYS not set",
        )
        return

    cutoff = datetime.now(UTC) - timedelta(days=config.analytics_retention_days)

    if _shared_db._session_factory is None:
        log.error("analytics_cleanup_no_session_factory")
        return

    try:
        from vektra_analytics.service import AnalyticsService

        service = AnalyticsService()
        async with _shared_db._session_factory() as session:
            count = await service.delete_before(session, cutoff)
            await session.commit()

        log.info("analytics_cleanup_purged", count=count, cutoff=cutoff.isoformat())

    except Exception as exc:
        log.error("analytics_cleanup_failed", error=str(exc))
