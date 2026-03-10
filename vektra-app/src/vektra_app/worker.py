"""arq worker settings factory for the Vektra application.

Composes ingest-level worker settings with cross-module cron tasks
(analytics cleanup) that would violate import boundaries if added
directly to vektra_ingest.jobs.

Usage (CMD_TARGET=worker entrypoint):
    from vektra_app.worker import get_app_worker_settings
    settings = get_app_worker_settings(registry)
"""

from __future__ import annotations

from typing import Any


def get_app_worker_settings(registry: Any) -> Any:
    """Return arq WorkerSettings with all platform cron tasks.

    Combines:
    - vektra_ingest cleanup_soft_deleted_task (VEKTRA_RETENTION_DAYS)
    - vektra_analytics cleanup_analytics_traces_task (VEKTRA_ANALYTICS_RETENTION_DAYS)
    """
    from arq import cron

    from vektra_analytics.jobs import cleanup_analytics_traces_task
    from vektra_ingest.jobs import get_worker_settings

    extra_cron_jobs = [
        cron(cleanup_analytics_traces_task, hour=3, minute=30),
    ]

    return get_worker_settings(registry, extra_cron_jobs=extra_cron_jobs)
