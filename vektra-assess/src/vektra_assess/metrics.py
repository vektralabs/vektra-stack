"""Content-free assess metrics on the API's ``/metrics`` (plan assess-privacy-retention 12b, REQ-080).

The numbers come from ``assess_metrics_snapshot()`` (revision 0041, SECURITY DEFINER, counts and
ages only). The API refreshes them every ``REFRESH_S`` seconds in a background task
(:class:`SnapshotCache`), because a Prometheus collector is synchronous and must not block a
scrape on the database. A scrape reads the last snapshot.

Labels are FIXED enums only: a value the snapshot returns outside its enum (a new close reason, a
corrupted row) is counted under ``other``, never emitted as itself. So no site, user, course or
id can become a label, whatever the database holds. ``LABELS`` is the whole allowed vocabulary;
the guard test fails if the exposition contains any other label value.

``*_total`` series count the rows the database still holds (the retention purge removes old
ones), so they can go down after a purge: alert on levels, not on ``rate()``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    Metric,
)
from prometheus_client.registry import REGISTRY, Collector

log = logging.getLogger("vektra_assess.metrics")

REFRESH_S = 15.0
OTHER = "other"

SESSION_STATES = ("STARTING", "ACTIVE", "OUTAGE", "CLOSED")
CLOSE_REASONS = (
    "finished",
    "timed_out",
    "service_failure",
    "budget_exhausted",
    "terminated_by_teacher",
    "access_revoked",
)
DELIVERY_STATUSES = ("PENDING", "RETRYING", "SENT", "FAILED", "ABANDONED", "REPLACED")
#: the Start rollback reasons of ``worker.preflight.Reason``; ``capture_failed_*`` variants fold
#: into ``capture_failed``
ROLLBACK_REASONS = (
    "start_timeout",
    "capacity",
    "activity_not_configured",
    "model_not_activated",
    "capture_failed",
    "part_b_rejected",
    "provider_unavailable",
    "rag_not_available",
    "internal_error",
    "commit_failed",
    # a teacher's terminate rolls a STARTING session back (revision 0038, assess_rollback_starting)
    "terminated_by_teacher",
    # a rollback recorded without a reason (the snapshot's COALESCE)
    "none",
)

#: label name -> every value it may take
LABELS: Mapping[str, frozenset[str]] = {
    "state": frozenset((*SESSION_STATES, OTHER)),
    "reason": frozenset((*CLOSE_REASONS, *ROLLBACK_REASONS, OTHER)),
    "status": frozenset((*DELIVERY_STATUSES, OTHER)),
}

SNAPSHOT_SQL = "SELECT public.assess_metrics_snapshot()"


def _fold(value: object, allowed: Iterable[str], *, prefix: bool = False) -> str:
    v = str(value)
    for a in allowed:
        if v == a or (prefix and v.startswith(a + "_")):
            return a
    return OTHER


def _num(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _grouped(
    raw: object, allowed: tuple[str, ...], *, prefix: bool = False
) -> dict[str, float]:
    """Counts per fixed enum value: every allowed value present (0 included), unknown -> other."""
    out = dict.fromkeys(allowed, 0.0)
    out[OTHER] = 0.0
    if isinstance(raw, Mapping):
        for key, n in raw.items():
            count = _num(n)
            if count is not None:
                out[_fold(key, allowed, prefix=prefix)] += count
    return out


def families(snap: Mapping[str, Any] | None, *, age_s: float | None) -> list[Metric]:
    """The metric families of one snapshot (``None``: the snapshot is not available)."""
    up = GaugeMetricFamily(
        "vektra_assess_metrics_up",
        "1 when the last assess metrics snapshot was read within the refresh budget",
    )
    up.add_metric([], 1.0 if snap is not None else 0.0)
    out: list[Metric] = [up]
    from vektra_shared.log_safety import dropped_keys_total

    dropped = CounterMetricFamily(
        "vektra_assess_log_keys_dropped",
        "Log keys of vektra_assess.* loggers replaced by the allowlist in this process",
    )
    dropped.add_metric([], float(dropped_keys_total()))
    out.append(dropped)
    if snap is None:
        return out
    if age_s is not None:
        g = GaugeMetricFamily(
            "vektra_assess_metrics_snapshot_age_seconds",
            "Age of the assess metrics snapshot",
        )
        g.add_metric([], age_s)
        out.append(g)

    sessions = GaugeMetricFamily(
        "vektra_assess_sessions", "Assess sessions by state", labels=["state"]
    )
    for state, n in _grouped(snap.get("sessions"), SESSION_STATES).items():
        sessions.add_metric([state], n)
    out.append(sessions)

    closed = CounterMetricFamily(
        "vektra_assess_sessions_closed",
        "Closed assess sessions still held, by close reason",
        labels=["reason"],
    )
    for reason, n in _grouped(snap.get("closed"), CLOSE_REASONS).items():
        closed.add_metric([reason], n)
    out.append(closed)

    deliveries = GaugeMetricFamily(
        "vektra_assess_deliveries", "Delivery rows by status", labels=["status"]
    )
    for status, n in _grouped(snap.get("deliveries"), DELIVERY_STATUSES).items():
        deliveries.add_metric([status], n)
    out.append(deliveries)

    rollbacks = CounterMetricFamily(
        "vektra_assess_start_rollbacks",
        "Start rollbacks still held, by reason",
        labels=["reason"],
    )
    for reason, n in _grouped(
        snap.get("rollbacks"), ROLLBACK_REASONS, prefix=True
    ).items():
        rollbacks.add_metric([reason], n)
    out.append(rollbacks)

    def gauge(name: str, doc: str, key: str) -> None:
        value = _num(snap.get(key))
        g = GaugeMetricFamily(name, doc)
        if value is not None:
            g.add_metric([], value)
            out.append(g)

    def counter(name: str, doc: str, key: str) -> None:
        value = _num(snap.get(key))
        c = CounterMetricFamily(name, doc)
        c.add_metric([], value if value is not None else 0.0)
        out.append(c)

    gauge(
        "vektra_assess_deliveries_retrying_oldest_seconds",
        "Age of the oldest RETRYING delivery",
        "retrying_oldest_s",
    )
    counter(
        "vektra_assess_capacity_refusals",
        "Start rollbacks for capacity (pre-flight) still held",
        "capacity_refusals",
    )
    counter(
        "vektra_assess_tls_failures",
        "Delivery rows whose last attempt failed on TLS",
        "tls_failures",
    )
    gauge(
        "vektra_assess_purge_last_success_timestamp",
        "Unix time of the last purge run that completed with every count at 0",
        "purge_last_success",
    )
    counter(
        "vektra_assess_purge_rows_deleted",
        "Content rows deleted by the retention purge (cumulative, worker-recorded)",
        "purge_rows_deleted",
    )
    gauge(
        "vektra_assess_worker_heartbeat_age_seconds",
        "Age of the assess worker heartbeat (database clock)",
        "worker_heartbeat_age_s",
    )
    gauge(
        "vektra_assess_jobs_oldest_ready_seconds",
        "Age of the oldest due job waiting for a worker",
        "jobs_oldest_ready_s",
    )
    smtp_ok = snap.get("smtp_probe_ok")
    if isinstance(smtp_ok, bool):
        g = GaugeMetricFamily(
            "vektra_assess_smtp_probe_ok", "1 when the last SMTP relay probe passed"
        )
        g.add_metric([], 1.0 if smtp_ok else 0.0)
        out.append(g)
    purge_ok = snap.get("purge_last_ok")
    if isinstance(purge_ok, bool):
        g = GaugeMetricFamily(
            "vektra_assess_purge_last_run_ok",
            "1 when the last purge run left no content row behind",
        )
        g.add_metric([], 1.0 if purge_ok else 0.0)
        out.append(g)
    return out


class SnapshotCache:
    """The last snapshot, refreshed by :meth:`run` every ``REFRESH_S`` seconds."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.snapshot: dict[str, Any] | None = None
        self.read_at: float | None = None

    async def refresh(self, engine: Any) -> None:
        from sqlalchemy import text

        try:
            async with engine.connect() as conn:
                value = (await conn.execute(text(SNAPSHOT_SQL))).scalar_one()
        except Exception as exc:
            log.warning(
                "assess_metrics_snapshot_failed",
                extra={"error_type": type(exc).__name__},
            )
            self.snapshot = None
            return
        self.snapshot = dict(value) if isinstance(value, Mapping) else None
        self.read_at = self._clock()

    async def run(self, engine: Any, interval_s: float = REFRESH_S) -> None:
        while True:
            await self.refresh(engine)
            await asyncio.sleep(interval_s)

    def current(self) -> tuple[dict[str, Any] | None, float | None]:
        if self.snapshot is None or self.read_at is None:
            return None, None
        age = self._clock() - self.read_at
        if (
            age > 4 * REFRESH_S
        ):  # the refresher stalled: say so rather than serve old counts
            return None, None
        return self.snapshot, age


class AssessCollector(Collector):
    """Registered once on the default registry; reads whatever cache is attached."""

    def __init__(self) -> None:
        self.cache: SnapshotCache | None = None

    def collect(self) -> Iterable[Metric]:
        if self.cache is None:
            return []
        snap, age = self.cache.current()
        return families(snap, age_s=age)


_COLLECTOR: AssessCollector | None = None


def install(cache: SnapshotCache) -> AssessCollector:
    """Attach ``cache`` to the process-wide collector (registered on first use)."""
    global _COLLECTOR
    if _COLLECTOR is None:
        _COLLECTOR = AssessCollector()
        REGISTRY.register(_COLLECTOR)
    _COLLECTOR.cache = cache
    return _COLLECTOR
