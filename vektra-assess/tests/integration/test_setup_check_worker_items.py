"""The worker-side setup-check items ``smtp_probe`` and ``db_log_statement_none`` on a real
Postgres (coordinator decisions 2026-11-08, plan assess-moodle-ws Notes).

The setup check has three results (OK, MISSING, UNVERIFIABLE) and stores ok | missing: a failed
item is MISSING with a reason code that names what failed.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from vektra_assess.moodle.setup_check import MISSING, OK, Outcome, builtin_registry
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


async def smtp(pg, *, configured: bool = True) -> Outcome:  # type: ignore[no-untyped-def]
    engine = create_async_engine(pg.url("worker"))
    try:
        check = setup_items.smtp_probe_item(engine.connect, configured=configured)
        return await check(CTX)
    finally:
        await engine.dispose()


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
        (
            {"state": "busy", "ok": False, "error_class": None, "smtp_code": None},
            "smtp_busy",
        ),
    ],
)
async def test_a_failed_probe_fails_naming_its_error_class(pg, details, reason) -> None:  # type: ignore[no-untyped-def]
    await _smtp_row(pg, details, age_s=10)
    assert await smtp(pg) == Outcome(MISSING, reason)


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
