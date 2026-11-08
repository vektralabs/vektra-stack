"""The worker-side setup-check items ``smtp_probe`` and ``db_log_statement_none`` on a real
Postgres (coordinator decisions 2026-11-08, plan assess-moodle-ws Notes).

The setup check has three results (OK, MISSING, UNVERIFIABLE) and stores ok | missing: a failed
item is MISSING with a reason code that names what failed.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fixtures.mailsink import MailSink, sink_config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from vektra_assess.delivery.probe import run_probe
from vektra_assess.delivery.smtp import SmtpSender
from vektra_assess.moodle.setup_check import (
    MISSING,
    OK,
    UNVERIFIABLE,
    Outcome,
    builtin_registry,
)
from vektra_assess.worker import setup_items
from vektra_assess.worker.slots import SMTP_PROBE_INTERVAL_S

pytestmark = pytest.mark.integration

CTX: Any = None  # neither item reads the Moodle context


async def _smtp_row(pg, details: dict[str, Any] | None, age_s: float = 0) -> None:  # type: ignore[no-untyped-def]
    await pg.run("super", "DELETE FROM assess_heartbeat WHERE component = 'smtp'")
    if details is not None:
        await pg.run(
            "super",
            "INSERT INTO assess_heartbeat (component, beat_at, details) VALUES "
            "('smtp', now() - make_interval(secs => :a), CAST(:d AS jsonb))",
            {"a": age_s, "d": json.dumps(details)},
        )


async def smtp(  # type: ignore[no-untyped-def]
    pg, *, configured: bool = True, prober: Any = None, **item_kw: Any
) -> Outcome:
    """``prober`` given: the item may run the real probe (``run_probe``) against it."""
    engine = create_async_engine(pg.url("worker"))
    try:

        async def probe() -> None:
            await run_probe(prober, engine.connect, timeout_s=10)

        check = setup_items.smtp_probe_item(
            engine.connect,
            configured=configured,
            probe=probe if prober is not None else None,
            **item_kw,
        )
        return await check(CTX)
    finally:
        await engine.dispose()


async def test_the_item_probe_is_capped_so_the_check_can_finish(pg) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4506662233: the item's own probe has a cap well inside the 120 s setup budget,
    so a hanging relay cannot use up the time the capability probes and the report need; a
    probe that hits the cap is UNVERIFIABLE, not a failed check."""
    import asyncio

    class Hangs:
        async def probe(self) -> Any:
            await asyncio.sleep(30)

    await _smtp_row(pg, None)
    loop = asyncio.get_running_loop()
    started = loop.time()
    out = await smtp(pg, prober=Hangs(), probe_timeout_s=0.3)
    assert out == Outcome(UNVERIFIABLE, "smtp_probe_timeout")
    assert loop.time() - started < 5
    assert setup_items.SMTP_ITEM_PROBE_MAX_S <= 30


async def _smtp_details(pg) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    rows = await pg.run(
        "super", "SELECT details FROM assess_heartbeat WHERE component = 'smtp'"
    )
    return rows[0][0] if rows else None


@pytest.mark.parametrize("before", ["none", "stale"])
async def test_a_missing_or_stale_probe_is_run_by_the_item_itself(  # type: ignore[no-untyped-def]
    pg, tmp_path, before: str
) -> None:
    """Review finding 1: at startup a queued setup check can be claimed before the probe tick
    has written its row (or after a long outage, a stale one); the item then probes itself
    (never sends) instead of reporting MISSING until the next daily run."""
    await _smtp_row(
        pg,
        None if before == "none" else OK_ROW,
        age_s=setup_items.SMTP_PROBE_MAX_AGE_S + 60,
    )
    sink = MailSink(tmp_path / "ok").start()
    try:
        out = await smtp(pg, prober=SmtpSender(sink_config(sink)))
    finally:
        sink.stop()
    assert out == Outcome(OK)
    assert (await _smtp_details(pg))["state"] == "ok"
    assert sink.messages == []


async def test_a_fresh_probe_row_is_not_probed_again(pg) -> None:  # type: ignore[no-untyped-def]
    class Never:
        async def probe(self) -> Any:
            raise AssertionError("a fresh row must be judged, not re-probed")

    await _smtp_row(pg, OK_ROW, age_s=10)
    assert await smtp(pg, prober=Never()) == Outcome(OK)


OK_ROW = {"state": "ok", "ok": True, "error_class": None, "smtp_code": 221}


def test_the_freshness_bound_is_three_probe_intervals() -> None:
    assert setup_items.SMTP_PROBE_MAX_AGE_S == 3 * SMTP_PROBE_INTERVAL_S == 900


async def test_a_fresh_ok_probe_is_ok(pg) -> None:  # type: ignore[no-untyped-def]
    await _smtp_row(pg, OK_ROW, age_s=10)
    assert await smtp(pg) == Outcome(OK)


async def test_a_stale_probe_fails(pg) -> None:  # type: ignore[no-untyped-def]
    await _smtp_row(pg, OK_ROW, age_s=setup_items.SMTP_PROBE_MAX_AGE_S + 5)
    assert await smtp(pg) == Outcome(MISSING, "smtp_probe_stale")


@pytest.mark.parametrize(
    ("details", "reason"),
    [
        (
            {"state": "failed", "ok": False, "error_class": "tls", "smtp_code": None},
            "smtp_tls",
        ),
        (
            {"state": "failed", "ok": False, "error_class": "auth", "smtp_code": 535},
            "smtp_auth",
        ),
        (
            {"state": "timeout", "ok": False, "error_class": None, "smtp_code": None},
            "smtp_timeout",
        ),
    ],
)
async def test_a_failed_probe_fails_naming_its_error_class(pg, details, reason) -> None:  # type: ignore[no-untyped-def]
    await _smtp_row(pg, details, age_s=10)
    assert await smtp(pg) == Outcome(MISSING, reason)


BUSY = {"state": "busy", "ok": False, "error_class": None, "smtp_code": None}


def _last(
    state: str, ok_: bool, error_class: str | None, age_s: float = 10
) -> dict[str, Any]:
    """A ``last`` stamped NOW minus ``age_s``: built when the test runs, never at collection
    (CI collects the whole suite long before this test runs)."""
    return {
        "state": state,
        "ok": ok_,
        "error_class": error_class,
        "at": time.time() - age_s,
    }


@pytest.mark.parametrize(
    ("last", "expected"),
    [
        (("ok", True, None, 10), Outcome(OK)),
        (("failed", False, "tls", 10), Outcome(MISSING, "smtp_tls")),
        # no earlier result and nothing to probe with: not a relay failure, and not OK
        (None, Outcome(UNVERIFIABLE, "smtp_busy")),
        # CodeRabbit 4506450148: a success older than the freshness bound vouches for nothing
        (
            ("ok", True, None, setup_items.SMTP_PROBE_MAX_AGE_S + 60),
            Outcome(UNVERIFIABLE, "smtp_busy"),
        ),
        # a ``last`` without its time is no history
        (
            {"state": "ok", "ok": True, "error_class": None},
            Outcome(UNVERIFIABLE, "smtp_busy"),
        ),
    ],
)
async def test_a_busy_relay_is_judged_by_its_last_real_result(
    pg, last, expected
) -> None:  # type: ignore[no-untyped-def]
    """Review finding 2: ``busy`` means every relay session was in use by deliveries, not that
    the relay failed: the item judges the last non-busy result, never MISSING for busy itself,
    and only while that result is itself fresh."""
    if isinstance(last, tuple):
        last = _last(*last)
    await _smtp_row(pg, BUSY | ({"last": last} if last else {}), age_s=10)
    assert await smtp(pg) == expected


async def test_a_busy_row_without_history_is_probed_again(pg, tmp_path) -> None:  # type: ignore[no-untyped-def]
    await _smtp_row(pg, BUSY, age_s=10)
    sink = MailSink(tmp_path / "ok").start()
    try:
        assert await smtp(pg, prober=SmtpSender(sink_config(sink))) == Outcome(OK)
    finally:
        sink.stop()


async def test_the_item_probes_the_relay_at_most_once(pg) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4506450166: a stale row probed into a busy one must not be probed again, or
    two bounded probes (about 65 s each) could use up the 120 s setup-check budget before the
    capability probes and the report are written."""
    from vektra_assess.delivery.smtp import SlotBusy

    calls = []

    class Busy:
        async def probe(self) -> Any:
            calls.append(1)
            raise SlotBusy

    await _smtp_row(pg, OK_ROW, age_s=setup_items.SMTP_PROBE_MAX_AGE_S + 60)
    assert await smtp(pg, prober=Busy()) == Outcome(UNVERIFIABLE, "smtp_busy")
    assert len(calls) == 1


async def test_a_busy_probe_keeps_the_last_real_result_with_its_time(pg) -> None:  # type: ignore[no-untyped-def]
    """``run_probe`` writes ``last`` on a busy outcome with the time of that real result,
    carried unchanged across several busy ones (CodeRabbit 4506450148)."""
    from vektra_assess.delivery.smtp import SlotBusy

    class Busy:
        async def probe(self) -> Any:
            raise SlotBusy

    await _smtp_row(pg, OK_ROW, age_s=100)
    engine = create_async_engine(pg.url("worker"))
    try:
        seen = []
        for _ in range(2):
            await run_probe(Busy(), engine.connect, timeout_s=1)
            details = await _smtp_details(pg)
            assert details["state"] == "busy"
            last = details["last"]
            assert {k: last[k] for k in ("state", "ok", "error_class")} == {
                "state": "ok",
                "ok": True,
                "error_class": None,
            }
            seen.append(last["at"])
        # the time of the real result (100 s ago), not of the busy rows
        assert seen[0] == seen[1] and 80 < time.time() - seen[0] < 160
    finally:
        await engine.dispose()


async def test_a_non_ascii_error_class_is_not_a_reason_code(pg) -> None:  # type: ignore[no-untyped-def]
    """Review finding 10: the reason code follows the setup check's own rule ([a-z0-9_])."""
    bad = {"state": "failed", "ok": False, "error_class": "tlsé", "smtp_code": None}
    await _smtp_row(pg, bad, age_s=10)
    assert await smtp(pg) == Outcome(MISSING, "smtp_unknown")


async def test_never_probed_fails(pg) -> None:  # type: ignore[no-untyped-def]
    await _smtp_row(pg, None)
    assert await smtp(pg) == Outcome(MISSING, "smtp_never_probed")


async def test_no_relay_configured_is_missing(pg) -> None:  # type: ignore[no-untyped-def]
    await _smtp_row(pg, OK_ROW, age_s=10)  # even a leftover good row does not count
    assert await smtp(pg, configured=False) == Outcome(MISSING, "smtp_not_configured")


# --- db_log_statement_none (ARCH-078) ------------------------------------------------------


def _session(pg, *settings: str):  # type: ignore[no-untyped-def]
    """An acquire whose connection has ``SET`` the given settings (superuser session)."""

    @asynccontextmanager
    async def acquire() -> AsyncIterator[AsyncConnection]:
        async with pg.conn("super") as c:
            for s in settings:
                await c.execute(text(f"SET {s}"))
            yield c

    return acquire


async def test_both_settings_hold_is_ok(pg) -> None:  # type: ignore[no-untyped-def]
    check = setup_items.db_log_statement_item(
        _session(pg, "log_statement = 'none'", "log_parameter_max_length = 0")
    )
    assert await check(CTX) == Outcome(OK)


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        (
            ("log_statement = 'all'", "log_parameter_max_length = 0"),
            "log_statement_not_none",
        ),
        (
            ("log_statement = 'none'", "log_parameter_max_length = -1"),
            "log_parameter_max_length_not_0",
        ),
        (
            ("log_statement = 'mod'", "log_parameter_max_length = 64"),
            "log_statement_not_none_and_log_parameter_max_length_not_0",
        ),
    ],
)
async def test_a_logging_setting_fails_by_name(pg, settings, reason) -> None:  # type: ignore[no-untyped-def]
    check = setup_items.db_log_statement_item(_session(pg, *settings))
    assert await check(CTX) == Outcome(MISSING, reason)


@pytest.mark.parametrize(
    ("target", "setting", "reason"),
    [
        ("ROLE vektra_assess_api", "log_statement = 'all'", "log_statement_not_none"),
        (
            "ROLE vektra_assess_worker",
            "log_parameter_max_length = 64",
            "log_parameter_max_length_not_0",
        ),
        ("DATABASE {db}", "log_statement = 'mod'", "log_statement_not_none"),
    ],
)
async def test_a_role_or_database_override_fails_the_item(  # type: ignore[no-untyped-def]
    pg, target: str, setting: str, reason: str
) -> None:
    """Review finding 8: the worker session's own values miss an override that turns logging
    on for another assess role (the API's statements) or for the whole database."""
    db = (await pg.run("super", "SELECT current_database()"))[0][0]
    target = target.format(db=f'"{db}"')
    name = setting.split("=")[0].strip()
    await pg.run("super", f"ALTER {target} SET {setting}")
    try:
        check = setup_items.db_log_statement_item(
            _session(pg, "log_statement = 'none'", "log_parameter_max_length = 0")
        )
        assert await check(CTX) == Outcome(MISSING, reason)
    finally:
        await pg.run("super", f"ALTER {target} RESET {name}")
    assert await check(CTX) == Outcome(OK)


async def test_a_worker_override_that_masks_the_server_value_is_not_ok(pg) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4507313007: the server logs every statement and only the worker role is set
    to ``none``. The worker session then reads ``none``, but the API role inherits ``all``. The
    worker cannot read the server value under its own override, so the item must not say OK."""
    engine = create_async_engine(pg.url("worker"))
    admin = create_async_engine(pg.url("super"), isolation_level="AUTOCOMMIT")

    async def run(*statements: str) -> None:
        async with admin.connect() as c:
            for s in statements:
                await c.execute(text(s))

    try:
        await run(
            "ALTER SYSTEM SET log_statement = 'all'",
            "ALTER SYSTEM SET log_parameter_max_length = 0",
            "SELECT pg_reload_conf()",
            "ALTER ROLE vektra_assess_worker SET log_statement = 'none'",
        )
        out = await setup_items.db_log_statement_item(engine.connect)(CTX)
    finally:
        await run(
            "ALTER ROLE vektra_assess_worker RESET log_statement",
            "ALTER SYSTEM RESET log_statement",
            "ALTER SYSTEM RESET log_parameter_max_length",
            "SELECT pg_reload_conf()",
        )
        await admin.dispose()
        await engine.dispose()
    assert out == Outcome(UNVERIFIABLE, "server_log_settings_masked")


async def test_the_worker_role_can_read_both_settings(pg) -> None:  # type: ignore[no-untyped-def]
    """As the item runs in production: the worker role reads the server's own values (the test
    container keeps the stock ``log_parameter_max_length = -1``, so it is named, not unreadable)."""
    engine = create_async_engine(pg.url("worker"))
    try:
        out = await setup_items.db_log_statement_item(engine.connect)(CTX)
    finally:
        await engine.dispose()
    assert out == Outcome(MISSING, "log_parameter_max_length_not_0")


def test_both_items_join_the_registry() -> None:
    reg = builtin_registry()
    setup_items.register_worker_items(reg, lambda: None, smtp_configured=True)  # type: ignore[arg-type, return-value]
    assert {"smtp_probe", "db_log_statement_none"} <= set(reg.names())
