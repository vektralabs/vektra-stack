"""``scripts/assess_nfr_report.py`` reads the rows it claims to read (plan assess-hardening-release).

Rows with known timestamps are planted in a migrated pg16, with triggers off for the plant only.
The loaded data must match them exactly: commit latencies, student-to-bot reply latencies, Start
attempts with their reasons, close reasons and live intervals, all inside the window and none
outside it.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "assess_nfr_report.py"
T0 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
GENESIS = "0" * 64


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("assess_nfr_report_db", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


r = _load()


async def _plant(pg, *stmts: tuple[str, dict]) -> None:  # type: ignore[no-untyped-def,type-arg]
    engine = create_async_engine(pg.super_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL session_replication_role = replica"))
            for sql, params in stmts:
                await conn.execute(text(sql), params)
    finally:
        await engine.dispose()


def _session(
    site: uuid.UUID,
    student: int,
    created: datetime,
    commit_s: float,
    close_s: float | None,
    reason: str | None,
) -> list[tuple[str, dict]]:  # type: ignore[type-arg]
    sid = uuid.uuid4()
    committed = created + timedelta(seconds=commit_s)
    closed = committed + timedelta(seconds=close_s) if close_s is not None else None
    stmts = [
        (
            "INSERT INTO assess_sessions (id, site_id, cmid, student_userid, session_no, state, "
            "close_reason, epoch, created_at, committed_at, closed_at) VALUES (:id, :site, 7, "
            ":st, 1, :state, :reason, 1, :c, :cm, :cl)",
            {
                "id": sid,
                "site": site,
                "st": student,
                "state": "CLOSED" if closed else "ACTIVE",
                "reason": reason,
                "c": created,
                "cm": committed,
                "cl": closed,
            },
        ),
        # a student turn answered 12 s later by the bot
        (
            "INSERT INTO assess_turns (session_id, turn_no, role, kind, ciphertext, turn_hash, "
            "prev_hash, created_at) VALUES (:id, 1, 'bot', 'opening', '\\x01', 'o', :g, :t0), "
            "(:id, 2, 'student', 'normal', '\\x01', 'a', 'o', :t1), "
            "(:id, 3, 'bot', 'normal', '\\x01', 'b', 'a', :t2)",
            {
                "id": sid,
                "g": GENESIS,
                "t0": committed,
                "t1": committed + timedelta(seconds=1),
                "t2": committed + timedelta(seconds=13),
            },
        ),
        (
            "INSERT INTO assess_start_attempts (site_id, cmid, student_userid, outcome, "
            "reason_code, created_at, session_id) VALUES (:site, 7, :st, 'committed', NULL, :c, :id)",
            {"site": site, "st": student, "c": created, "id": sid},
        ),
    ]
    return stmts


async def test_the_report_reads_exactly_the_planted_rows(pg) -> None:  # type: ignore[no-untyped-def]
    site = uuid.uuid4()
    stmts: list[tuple[str, dict]] = [  # type: ignore[type-arg]
        (
            "INSERT INTO assess_sites (id, moodle_site_uuid, base_url) VALUES (:s, :u, "
            "'https://lms.example')",
            {"s": site, "u": str(site)},
        )
    ]
    stmts += _session(site, 1, T0 + timedelta(minutes=1), 4.0, 600, "finished")
    stmts += _session(site, 2, T0 + timedelta(minutes=2), 8.0, 300, "service_failure")
    stmts += _session(site, 3, T0 + timedelta(minutes=3), 2.0, None, None)  # still live
    # outside the window: must not be read
    stmts += _session(site, 4, T0 - timedelta(hours=2), 50.0, 60, "finished")
    stmts.append(
        (
            "INSERT INTO assess_start_attempts (site_id, cmid, student_userid, outcome, "
            "reason_code, created_at) VALUES (:site, 7, 9, 'rolled_back', 'capacity', :c)",
            {"site": site, "c": T0 + timedelta(minutes=4)},
        )
    )
    await _plant(pg, *stmts)

    cfg = r.Config(
        dsn=pg.super_url.replace("postgresql+asyncpg://", "postgresql://"),
        since=T0,
        until=T0 + timedelta(hours=1),
        label="db-test",
    )
    data = await r.load(cfg)
    assert sorted(data.commit_s) == [2.0, 4.0, 8.0]
    assert data.reply_s == [12.0, 12.0, 12.0]
    assert sorted(data.attempts, key=str) == sorted(
        [("committed", None)] * 3 + [("rolled_back", "capacity")], key=str
    )
    assert sorted(data.close_reasons) == ["finished", "service_failure"]
    assert len(data.live_intervals) == 3
    live = r.concurrency(data.live_intervals)
    assert live["max_live"] == 3  # the three sessions of the window overlap
    res = r.evaluate(data)
    assert res["nfr_020_inputs"]["capacity_refusals"] == 1
    # three sessions are far below the declared minimums: judged nowhere, BLOCKED everywhere
    assert {i["verdict"] for i in res["items"]} == {"BLOCKED"}


async def test_advisory_refusals_are_read_from_the_audit_events(pg) -> None:  # type: ignore[no-untyped-def]
    """The API's advisory refusals write no attempt row, only an ``assess_start_refused`` audit
    event: capacity goes to NFR-020 and out of "admitted", a worker that is not ready is a
    service-caused refusal and counts against the NFR-019 commit rate."""
    await pg.run("super", "DELETE FROM assess_audit_events")
    await pg.run("super", "DELETE FROM assess_start_attempts")
    site = uuid.uuid4()
    t = T0 + timedelta(minutes=5)
    audit = (
        "INSERT INTO assess_audit_events (site_id, event, reason_code, actor_kind, created_at) "
        "VALUES (:s, :e, :r, 'api', :t)"
    )
    stmts: list[tuple[str, dict]] = [  # type: ignore[type-arg]
        (audit, {"s": site, "e": "assess_start_refused", "r": "capacity", "t": t}),
        (audit, {"s": site, "e": "assess_start_refused", "r": "capacity", "t": t}),
        (
            audit,
            {"s": site, "e": "assess_start_refused", "r": "worker_not_ready", "t": t},
        ),
        (
            audit,
            {"s": site, "e": "assess_start_refused", "r": "worker_heartbeat", "t": t},
        ),
        # a student-side refusal and an event outside the window are not read
        (
            audit,
            {"s": site, "e": "assess_start_refused", "r": "start_rate_cap", "t": t},
        ),
        (
            audit,
            {
                "s": site,
                "e": "assess_start_refused",
                "r": "capacity",
                "t": T0 - timedelta(hours=3),
            },
        ),
    ]
    await _plant(pg, *stmts)
    cfg = r.Config(
        dsn=pg.super_url.replace("postgresql+asyncpg://", "postgresql://"),
        since=T0,
        until=T0 + timedelta(hours=1),
        label="db-refusals",
    )
    data = await r.load(cfg)
    assert sorted(data.attempts, key=str) == sorted(
        [
            ("refused", "capacity"),
            ("refused", "capacity"),
            ("refused", "worker_not_ready"),
            ("refused", "worker_heartbeat"),
        ],
        key=str,
    )
    data.attempts += [("committed", None)] * 98
    res = r.evaluate(data)
    assert res["nfr_020_inputs"]["capacity_refusals"] == 2
    item = next(i for i in res["items"] if i["item"] == "NFR-019 commit rate")
    assert (item["admitted"], item["committed"]) == (100, 98)
    assert (
        item["verdict"] == "FAIL"
    )  # 98% < 99%: the worker outage is the service's fault
