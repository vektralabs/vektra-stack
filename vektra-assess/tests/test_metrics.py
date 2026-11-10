"""Assess /metrics collector (plan assess-privacy-retention 12b, REQ-080): fixed labels only."""

from __future__ import annotations

import re
from typing import Any

from prometheus_client import generate_latest
from prometheus_client.registry import CollectorRegistry

from vektra_assess import metrics as m

#: everything a hostile or corrupted database could put where an enum belongs
HOSTILE: dict[str, Any] = {
    "sessions": {"ACTIVE": 2, "CLOSED": 5, "site-uni.example": 1},
    "closed": {"finished": 4, "mario.rossi@uni.example": 1},
    "deliveries": {"SENT": 3, "RETRYING": 1, "42": 7},
    "rollbacks": {
        "capture_failed_attempt_unreadable": 2,
        "capacity": 1,
        "course-101": 9,
    },
    "retrying_oldest_s": 3600.5,
    "tls_failures": 2,
    "capacity_refusals": 3,
    "jobs_oldest_ready_s": 1.5,
    "worker_heartbeat_age_s": 4.0,
    "smtp_probe_ok": True,
    "smtp_probe_age_s": 30.0,
    "purge_last_success": 1_760_000_000.0,
    "purge_rows_deleted": 120,
    "purge_last_ok": True,
}


def _exposition(snap: dict[str, Any] | None, age: float | None = 1.0) -> str:
    class _Fixed:
        def collect(self):  # type: ignore[no-untyped-def]
            return m.families(snap, age_s=age)

    reg = CollectorRegistry()
    reg.register(_Fixed())  # type: ignore[arg-type]
    return generate_latest(reg).decode()


_LABEL = re.compile(r'(\w+)="([^"]*)"')


def test_every_label_value_is_in_its_fixed_enum() -> None:
    text = _exposition(HOSTILE)
    seen = _LABEL.findall(text)
    assert seen, "no labelled series emitted: the guard would pass vacuously"
    for name, value in seen:
        assert name in m.LABELS, name
        assert value in m.LABELS[name], f"{name}={value}"
    for leak in ("uni.example", "mario", "course-101", '"42"'):
        assert leak not in text


def test_unknown_values_are_counted_under_other() -> None:
    text = _exposition(HOSTILE)
    assert 'vektra_assess_sessions{state="other"} 1.0' in text
    assert 'vektra_assess_sessions_closed_total{reason="other"} 1.0' in text
    assert 'vektra_assess_deliveries{status="other"} 7.0' in text
    assert 'vektra_assess_start_rollbacks_total{reason="capture_failed"} 2.0' in text
    assert 'vektra_assess_start_rollbacks_total{reason="other"} 9.0' in text


def test_every_enum_value_is_present_even_at_zero() -> None:
    text = _exposition({})
    for state in m.SESSION_STATES:
        assert f'vektra_assess_sessions{{state="{state}"}} 0.0' in text
    for status in m.DELIVERY_STATUSES:
        assert f'vektra_assess_deliveries{{status="{status}"}} 0.0' in text


def test_scalar_series() -> None:
    text = _exposition(HOSTILE)
    assert "vektra_assess_metrics_up 1.0" in text
    assert "vektra_assess_deliveries_retrying_oldest_seconds 3600.5" in text
    assert "vektra_assess_tls_failures_total 2.0" in text
    assert "vektra_assess_capacity_refusals_total 3.0" in text
    assert "vektra_assess_purge_rows_deleted_total 120.0" in text
    assert "vektra_assess_smtp_probe_ok 1.0" in text
    assert "vektra_assess_worker_heartbeat_age_seconds 4.0" in text
    assert "vektra_assess_jobs_oldest_ready_seconds 1.5" in text
    assert "vektra_assess_purge_last_success_timestamp 1.76e+09" in text
    assert "vektra_assess_log_keys_dropped_total" in text


def test_no_snapshot_reports_down_and_nothing_else() -> None:
    text = _exposition(None, None)
    assert "vektra_assess_metrics_up 0.0" in text
    assert "vektra_assess_sessions" not in text
    # the process's own figure needs no snapshot
    assert "vektra_assess_log_keys_dropped_total" in text


def test_a_stalled_cache_is_not_served() -> None:
    now = [100.0]
    cache = m.SnapshotCache(clock=lambda: now[0])
    cache.snapshot, cache.read_at = dict(HOSTILE), 100.0
    assert cache.current()[0] is not None
    now[0] += 4 * m.REFRESH_S + 1
    assert cache.current() == (None, None)


async def test_a_failed_refresh_drops_the_snapshot() -> None:
    class _Broken:
        def connect(self):  # type: ignore[no-untyped-def]
            raise OSError("down")

    cache = m.SnapshotCache()
    cache.snapshot, cache.read_at = dict(HOSTILE), 0.0
    await cache.refresh(_Broken())
    assert cache.snapshot is None


def test_install_registers_once_and_swaps_the_cache() -> None:
    first = m.install(m.SnapshotCache())
    second_cache = m.SnapshotCache()
    again = m.install(second_cache)
    assert first is again and again.cache is second_cache
    assert list(again.collect())  # up=0 at least


def test_every_alert_rule_reads_a_series_the_collector_emits() -> None:
    """deploy/prometheus/assess-alerts.yml.example: the plan's alerts, on real series names."""
    from pathlib import Path

    import yaml

    path = (
        Path(__file__).resolve().parents[2]
        / "deploy/prometheus/assess-alerts.yml.example"
    )
    rules = yaml.safe_load(path.read_text())["groups"][0]["rules"]
    text = _exposition(HOSTILE)
    emitted = {line.split("{")[0].split(" ")[0] for line in text.splitlines()
               if line and not line.startswith("#")}  # fmt: skip
    for rule in rules:
        for name in re.findall(r"vektra_assess_\w+", rule["expr"]):
            assert name in emitted, (rule["alert"], name)
    names = {r["alert"] for r in rules}
    assert {
        "VektraAssessWorkerHeartbeatStale",
        "VektraAssessJobBacklog",
        "VektraAssessDeliveryFailed",
        "VektraAssessDeliveryRetryingTooLong",
        "VektraAssessSmtpTlsFailures",
        "VektraAssessPurgeLate",
        "VektraAssessPurgeVerificationFailed",
        "VektraAssessSmtpProbeDown",
    } <= names
