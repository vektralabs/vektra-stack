"""Worker-side REQ-079 setup-check items that read the database (coordinator decisions,
2026-11-08, plan assess-moodle-ws Notes).

- ``smtp_probe`` (plan 10 registers it, moodle-ws plan Integration Notes): judged from the row
  the ``smtp_probe`` tick upserts into ``assess_heartbeat`` (component ``smtp``, details
  ``{state, ok, error_class, smtp_code}``, ``beat_at`` = check time). OK when the row is there,
  ``ok`` is true and ``beat_at`` is at most ``SMTP_PROBE_MAX_AGE_S`` old (3 tick intervals: one
  missed or slow probe is tolerated, two are not). With no row or a stale one, the item runs
  the probe itself (bounded, never sends) and judges that fresh result.
- ``db_log_statement_none`` (ARCH-078, plan assess-foundations): ``SHOW log_statement`` is
  ``none`` and ``SHOW log_parameter_max_length`` is ``0`` on the worker's own connection, AND
  no ``ALTER ROLE/DATABASE ... SET`` override (``pg_db_role_setting``) gives any assess role, or
  this database, another value: the worker's session cannot see the API role's settings.

A failed item is ``MISSING`` with a reason code naming the cause (the check has no separate
"failed" result, and the item table stores ok | missing). A database error raises and the runner
reports ``UNVERIFIABLE/check_error``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text

from vektra_assess.moodle.setup_check import (
    CheckContext,
    ItemCheck,
    ItemRegistry,
    Outcome,
    missing,
    ok,
    unverifiable,
)
from vektra_assess.moodle.setup_check import _code as reason_code
from vektra_assess.worker.runner import AcquireDb
from vektra_assess.worker.slots import SMTP_PROBE_INTERVAL_S

SMTP_PROBE_MAX_AGE_S = 3 * SMTP_PROBE_INTERVAL_S

_SMTP_ROW = text(
    "SELECT details, extract(epoch FROM now() - beat_at) AS age, "
    "extract(epoch FROM now()) AS now FROM assess_heartbeat WHERE component = 'smtp'"
)
_LOGGING = text(
    "SELECT current_setting('log_statement'), current_setting('log_parameter_max_length')"
)
#: the assess roles whose statements must not be logged (the other two sessions' settings are
#: not visible from the worker's own session)
ASSESS_ROLES = ("vektra_assess_api", "vektra_assess_worker", "vektra_assess_definer")
#: per-role and per-database overrides (``ALTER ROLE/DATABASE ... SET``) that apply to an assess
#: role in this database: the role alone, the database alone, or the pair
_OVERRIDES = text(
    "SELECT cfg FROM pg_db_role_setting s "
    "LEFT JOIN pg_roles r ON r.oid = s.setrole "
    "CROSS JOIN LATERAL unnest(s.setconfig) AS cfg "
    "WHERE (s.setdatabase = 0 OR s.setdatabase = "
    "(SELECT oid FROM pg_database WHERE datname = current_database())) "
    "AND (s.setrole = 0 OR r.rolname = ANY(:roles)) "
    "AND (cfg LIKE 'log_statement=%' OR cfg LIKE 'log_parameter_max_length=%')"
)
_EXPECTED = {"log_statement": "none", "log_parameter_max_length": "0"}


def _code(value: Any) -> str:
    """The setup check's own reason-code rule (``[a-z0-9_]{1,64}``), ``unknown`` otherwise."""
    return reason_code(str(value or "unknown"), "unknown")


def smtp_probe_item(
    acquire: AcquireDb,
    *,
    configured: bool,
    probe: Callable[[], Awaitable[Any]] | None = None,
) -> ItemCheck:
    """``probe``: one whole bounded probe (``delivery.probe.run_probe``, which writes the row
    and never sends). Without a fresh row, the item runs it and judges what it wrote (review
    finding 1): at startup a setup check queued earlier can be claimed before the probe tick has
    written its first row, and a long-stopped worker finds only a stale one."""

    async def read() -> Any:
        async with acquire() as conn:
            return (await conn.execute(_SMTP_ROW)).first()

    async def check(_ctx: CheckContext) -> Outcome:
        if not configured:
            return missing("smtp_not_configured")
        row = await read()
        if probe is not None and (row is None or float(row.age) > SMTP_PROBE_MAX_AGE_S):
            await probe()
            row = await read()
        if row is None:
            return missing("smtp_never_probed")
        if float(row.age) > SMTP_PROBE_MAX_AGE_S:
            return missing("smtp_probe_stale")
        verdict = _judge(row.details, float(row.now))
        if verdict is None and probe is not None:
            # busy with no earlier result to go by: probe once more (finding 2)
            await probe()
            row = await read()
            if row is not None and float(row.age) <= SMTP_PROBE_MAX_AGE_S:
                verdict = _judge(row.details, float(row.now))
        # still busy: not a relay failure (never MISSING), but nothing vouches for it either
        return verdict or unverifiable("smtp_busy")

    return check


def _judge(raw: Any, now: float) -> Outcome | None:
    """OK / MISSING from one probe row; ``busy`` is judged by the last real result it carries
    (review finding 2) while that result is itself at most ``SMTP_PROBE_MAX_AGE_S`` old
    (CodeRabbit 4506450148); None when there is no such result."""
    details = raw if isinstance(raw, dict) else {}
    if details.get("state") == "busy":
        last = details.get("last")
        if not isinstance(last, dict):
            return None
        at = last.get("at")
        if not isinstance(at, int | float) or now - float(at) > SMTP_PROBE_MAX_AGE_S:
            return None
        details = last
    if details.get("ok") is True:
        return ok()
    cause = details.get("error_class") or details.get("state")
    return missing(f"smtp_{_code(cause)}")


def db_log_statement_item(acquire: AcquireDb) -> ItemCheck:
    async def check(_ctx: CheckContext) -> Outcome:
        async with acquire() as conn:
            statement, max_length = (await conn.execute(_LOGGING)).one()
            overrides = [
                str(c).split("=", 1)
                for (c,) in (
                    await conn.execute(_OVERRIDES, {"roles": list(ASSESS_ROLES)})
                ).all()
            ]
        values = {
            "log_statement": {statement},
            "log_parameter_max_length": {max_length},
        }
        for name, value in overrides:
            values[name].add(value.strip("'\""))
        problems = [
            f"{name}_not_{_EXPECTED[name]}"
            for name in ("log_statement", "log_parameter_max_length")
            if {str(v) for v in values[name]} != {_EXPECTED[name]}
        ]
        return missing("_and_".join(problems)) if problems else ok()

    return check


def register_worker_items(
    registry: ItemRegistry,
    acquire: AcquireDb,
    *,
    smtp_configured: bool,
    smtp_probe: Callable[[], Awaitable[Any]] | None = None,
) -> None:
    registry.register(
        "smtp_probe",
        smtp_probe_item(acquire, configured=smtp_configured, probe=smtp_probe),
    )
    registry.register("db_log_statement_none", db_log_statement_item(acquire))
