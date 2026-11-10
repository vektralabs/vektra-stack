"""``assess_metrics_snapshot()`` on real Postgres (plan assess-privacy-retention 12b, revision 0041).

Counts only, for the API only: the worker cannot call it, and the API still cannot read the rows
it counts. No identifier of any row reaches the result.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy.exc import DBAPIError

from vektra_assess import metrics as m

from .conftest import make_session

pytestmark = pytest.mark.integration


async def _seed(pg) -> tuple[uuid.UUID, uuid.UUID]:  # type: ignore[no-untyped-def]
    active = await make_session(pg, "ACTIVE")
    closed = await make_session(pg, "CLOSED")
    site = (
        await pg.run(
            "super", "SELECT site_id FROM assess_sessions WHERE id = :s", {"s": active}
        )
    )[0][0]
    for outcome, reason in (
        ("rolled_back", "capture_failed_attempt_unreadable"),
        ("rolled_back", "capacity"),
        ("committed", None),
    ):
        await pg.run(
            "super",
            "INSERT INTO assess_start_attempts (site_id, cmid, student_userid, outcome, "
            "reason_code) VALUES (:s, 1, 7, :o, :r)",
            {"s": site, "o": outcome, "r": reason},
        )
    await pg.run(
        "super",
        "INSERT INTO assess_audit_events (site_id, event, reason_code, actor_kind) "
        "VALUES (:s, 'assess_start_refused', 'capacity', 'api')",
        {"s": site},
    )
    await pg.run(
        "super",
        "INSERT INTO assess_jobs (kind, not_before) VALUES ('purge', now() - interval '90 s')",
    )
    for component, details in (
        ("worker", {"state": "ready"}),
        ("smtp", {"state": "busy", "last": {"ok": False, "state": "fail"}}),
        ("purge", {"ok": True, "last_success": 1_760_000_000, "rows_deleted": 12}),
    ):
        await pg.run(
            "super",
            "INSERT INTO assess_heartbeat (component, details) VALUES (:c, CAST(:d AS jsonb)) "
            "ON CONFLICT (component) DO UPDATE SET details = EXCLUDED.details, beat_at = now()",
            {"c": component, "d": json.dumps(details)},
        )
    return active, closed


async def test_the_api_reads_counts_and_nothing_else(pg) -> None:  # type: ignore[no-untyped-def]
    active, closed = await _seed(pg)
    snap = (await pg.run("api", m.SNAPSHOT_SQL))[0][0]
    assert snap["sessions"]["ACTIVE"] >= 1 and snap["sessions"]["CLOSED"] >= 1
    assert snap["closed"]["finished"] >= 1
    assert snap["rollbacks"]["capture_failed_attempt_unreadable"] == 1
    assert (
        snap["capacity_refusals"] == 1
    )  # the pre-flight one; advisory ones are audit-only
    assert snap["jobs_oldest_ready_s"] >= 89
    assert snap["smtp_probe_ok"] is False  # busy: judged on the previous probe
    assert snap["purge_rows_deleted"] == 12 and snap["purge_last_ok"] is True
    assert snap["worker_heartbeat_age_s"] is not None
    blob = json.dumps(snap)
    for ident in (active, closed):
        assert str(ident) not in blob
    site = (
        await pg.run(
            "super",
            "SELECT site_id::text FROM assess_sessions WHERE id = :s",
            {"s": active},
        )
    )[0][0]
    assert site not in blob
    # the collector folds the raw reason into its enum
    text = "\n".join(str(f) for f in m.families(snap, age_s=0.0))
    assert "capture_failed_attempt_unreadable" not in text


async def test_only_the_api_may_call_it_and_it_opens_no_table(pg) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(DBAPIError, match="permission denied"):
        await pg.run("worker", m.SNAPSHOT_SQL)
    for table in ("assess_jobs", "assess_audit_events"):
        with pytest.raises(DBAPIError, match="permission denied"):
            await pg.run("api", f"SELECT count(*) FROM {table}")


async def test_the_snapshot_and_the_exposition_agree(pg) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit nitpick (metrics.py): seeded RETRYING and TLS-failed deliveries and the purge
    fields come out of ``families`` with the values the database holds."""
    from prometheus_client import generate_latest
    from prometheus_client.registry import CollectorRegistry

    await pg.run("super", "DELETE FROM assess_deliveries")
    for status, cls in (("RETRYING", "tls"), ("FAILED", "tls"), ("RETRYING", None)):
        await pg.run(
            "super",
            "INSERT INTO assess_deliveries (kind, recipient_kind, recipient_ref, status, "
            "last_error_class, job_id, created_at) VALUES ('operator_notify', 'address', "
            ":ref, :st, :c, 1, now() - interval '2 hours')",
            {"ref": uuid.uuid4().hex * 2, "st": status, "c": cls},
        )
    # a test send must stay out of every delivery figure
    await pg.run(
        "super",
        "INSERT INTO assess_deliveries (kind, recipient_kind, recipient_ref, status, "
        "last_error_class) VALUES ('smtp_test', 'address', :ref, 'FAILED', 'tls')",
        {"ref": uuid.uuid4().hex * 2},
    )
    await pg.run(
        "super",
        "INSERT INTO assess_heartbeat (component, details) VALUES ('purge', "
        "CAST(:d AS jsonb)) "
        "ON CONFLICT (component) DO UPDATE SET details = EXCLUDED.details",
        {
            "d": json.dumps(
                {"ok": False, "last_success": 1700000000, "rows_deleted": 42}
            )
        },
    )
    snap = (await pg.run("api", m.SNAPSHOT_SQL))[0][0]

    class _Fixed:
        def collect(self):  # type: ignore[no-untyped-def]
            return m.families(snap, age_s=0.0)

    reg = CollectorRegistry()
    reg.register(_Fixed())  # type: ignore[arg-type]
    text = generate_latest(reg).decode()
    assert 'vektra_assess_deliveries{status="RETRYING"} 2.0' in text
    assert 'vektra_assess_deliveries{status="FAILED"} 1.0' in text
    assert "vektra_assess_tls_failures 2.0" in text
    oldest = [ln for ln in text.splitlines()
              if ln.startswith("vektra_assess_deliveries_retrying_oldest_seconds ")]  # fmt: skip
    assert oldest and float(oldest[0].split()[1]) >= 7190
    assert "vektra_assess_purge_last_success_timestamp 1.7e+09" in text
    assert "vektra_assess_purge_rows_deleted_total 42.0" in text
    assert "vektra_assess_purge_last_run_ok 0.0" in text
