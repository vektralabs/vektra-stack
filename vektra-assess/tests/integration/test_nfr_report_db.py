"""``scripts/assess_nfr_report.py`` reads the rows it claims to read (plan assess-hardening-release).

Rows with known timestamps are planted in a migrated pg16, with triggers off for the plant only.
The loaded data must match them exactly: commit latencies, student-to-bot reply latencies, Start
attempts with their reasons, close reasons and live intervals, all inside the window and none
outside it.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
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
            "INSERT INTO assess_sites (id, lms_site_uuid, base_url) VALUES (:s, :u, "
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


async def test_a_role_that_rls_restricts_is_refused_not_reported(pg, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Review of #209 finding 2: under RLS (revision a0002) the API role, or any read-only role
    without BYPASSRLS, reads 0 rows of the sessions and attempts without an error. The script
    refuses it by name (exit 3, nothing written) instead of a BLOCKED report on empty data; the
    worker (unconditional policy) and the superuser still read."""

    def cfg(url: str) -> Any:
        return r.Config(
            dsn=url.replace("postgresql+asyncpg://", "postgresql://"),
            since=T0,
            until=T0 + timedelta(hours=1),
            label="db-rls",
        )

    with pytest.raises(r.RowSecurityRefusal) as exc:
        await r.load(cfg(pg.url("api")))
    assert exc.value.tables == ["assess_sessions", "assess_start_attempts"]
    assert "BYPASSRLS" in str(exc.value)
    # positive control: a role that sees every row is not refused
    await r.load(cfg(pg.super_url))
    # and a role with BYPASSRLS, the recipe of the operator doc, is not either
    role = f"nfr_reader_{uuid.uuid4().hex[:8]}"
    await pg.run(
        "super", f"CREATE ROLE {role} LOGIN PASSWORD 'reader-password-0123' BYPASSRLS"
    )
    await pg.run(
        "super",
        "GRANT SELECT ON assess_sessions, assess_turns, assess_start_attempts, "
        f"assess_audit_events TO {role}",
    )
    url = (
        make_url(pg.super_url)
        .set(username=role, password="reader-password-0123")
        .render_as_string(hide_password=False)
    )
    await r.load(cfg(url))
    # CodeRabbit on #209: a role WITHOUT BYPASSRLS that an unconditional permissive policy opens
    # is accepted, unless a restrictive read policy that is not unconditional also applies to
    # it (PostgreSQL ANDs it with the permissive one: a partial campaign)
    reader = f"nfr_policy_{uuid.uuid4().hex[:8]}"
    await pg.run("super", f"CREATE ROLE {reader} LOGIN PASSWORD 'reader-password-0123'")
    await pg.run(
        "super",
        "GRANT SELECT ON assess_sessions, assess_turns, assess_start_attempts, "
        f"assess_audit_events TO {reader}",
    )
    policies = [
        (f"nfr_open_{t}", t) for t in ("assess_sessions", "assess_start_attempts")
    ]
    restrictive = "nfr_restrict_assess_sessions"
    try:
        for name, table in policies:
            await pg.run(
                "super",
                f"CREATE POLICY {name} ON {table} AS PERMISSIVE FOR SELECT TO {reader} "
                "USING (true)",
            )
        reader_url = (
            make_url(pg.super_url)
            .set(username=reader, password="reader-password-0123")
            .render_as_string(hide_password=False)
        )
        await r.load(cfg(reader_url))  # unconditional: accepted
        await pg.run(
            "super",
            f"CREATE POLICY {restrictive} ON assess_sessions AS RESTRICTIVE FOR SELECT "
            f"TO {reader} USING (cmid > 0)",
        )
        with pytest.raises(r.RowSecurityRefusal) as exc:
            await r.load(cfg(reader_url))
        assert exc.value.tables == ["assess_sessions"]
        # a restrictive policy that is provably unconditional does not refuse
        await pg.run("super", f"DROP POLICY {restrictive} ON assess_sessions")
        await pg.run(
            "super",
            f"CREATE POLICY {restrictive} ON assess_sessions AS RESTRICTIVE FOR SELECT "
            f"TO {reader} USING (true)",
        )
        await r.load(cfg(reader_url))
        # CodeRabbit 4558599990: a restrictive policy with no USING clause (WITH CHECK only:
        # qual NULL, here on FOR ALL, which the read filter considers) filters no read, so it
        # does not refuse either
        await pg.run("super", f"DROP POLICY {restrictive} ON assess_sessions")
        await pg.run(
            "super",
            f"CREATE POLICY {restrictive} ON assess_sessions AS RESTRICTIVE FOR ALL "
            f"TO {reader} WITH CHECK (cmid > 0)",
        )
        qual = await pg.run(
            "super",
            "SELECT qual FROM pg_policies WHERE policyname = :p",
            {"p": restrictive},
        )
        assert qual == [(None,)]
        await r.load(cfg(reader_url))
    finally:
        await pg.run("super", f"DROP POLICY IF EXISTS {restrictive} ON assess_sessions")
        for name, table in policies:
            await pg.run("super", f"DROP POLICY IF EXISTS {name} ON {table}")
    # CodeRabbit on #209 (second round): PostgreSQL applies a policy to every role that
    # inherits the policy's role. A login role whose policies all name a group it inherits:
    group, member = (
        f"nfr_group_{uuid.uuid4().hex[:8]}",
        f"nfr_member_{uuid.uuid4().hex[:8]}",
    )
    await pg.run("super", f"CREATE ROLE {group} NOLOGIN")
    await pg.run(
        "super",
        f"CREATE ROLE {member} LOGIN INHERIT PASSWORD 'reader-password-0123' IN ROLE {group}",
    )
    await pg.run(
        "super",
        "GRANT SELECT ON assess_sessions, assess_turns, assess_start_attempts, "
        f"assess_audit_events TO {group}",
    )
    member_url = (
        make_url(pg.super_url)
        .set(username=member, password="reader-password-0123")
        .render_as_string(hide_password=False)
    )
    inherited = [
        (f"nfr_gopen_{t}", t) for t in ("assess_sessions", "assess_start_attempts")
    ]
    group_restrictive = "nfr_grestrict_assess_sessions"
    try:
        # no policy at all for the member or its group: refused on both tables
        with pytest.raises(r.RowSecurityRefusal) as exc:
            await r.load(cfg(member_url))
        assert exc.value.tables == ["assess_sessions", "assess_start_attempts"]
        # an inherited-only permissive unconditional policy: accepted
        for name, table in inherited:
            await pg.run(
                "super",
                f"CREATE POLICY {name} ON {table} AS PERMISSIVE FOR SELECT TO {group} "
                "USING (true)",
            )
        await r.load(cfg(member_url))
        # an inherited restrictive policy that is not unconditional: refused
        await pg.run(
            "super",
            f"CREATE POLICY {group_restrictive} ON assess_sessions AS RESTRICTIVE "
            f"FOR SELECT TO {group} USING (cmid > 0)",
        )
        with pytest.raises(r.RowSecurityRefusal) as exc:
            await r.load(cfg(member_url))
        assert exc.value.tables == ["assess_sessions"]
    finally:
        await pg.run(
            "super", f"DROP POLICY IF EXISTS {group_restrictive} ON assess_sessions"
        )
        for name, table in inherited:
            await pg.run("super", f"DROP POLICY IF EXISTS {name} ON {table}")
    # through the entry point: exit 3, verdict REFUSED, no report file
    code, res = await asyncio.to_thread(r.run, cfg(pg.url("api")), tmp_path)
    assert (code, res["verdict"]) == (3, "REFUSED")
    assert list(tmp_path.iterdir()) == []
