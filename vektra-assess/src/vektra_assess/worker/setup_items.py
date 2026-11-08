"""Worker-side REQ-079 setup-check items that read the database (coordinator decisions,
2026-11-08, plan assess-moodle-ws Notes).

- ``smtp_probe`` (plan 10 registers it, moodle-ws plan Integration Notes): judged from the row
  the ``smtp_probe`` tick upserts into ``assess_heartbeat`` (component ``smtp``, details
  ``{state, ok, error_class, smtp_code}``, ``beat_at`` = check time). OK when the row is there,
  ``ok`` is true and ``beat_at`` is at most ``SMTP_PROBE_MAX_AGE_S`` old (3 tick intervals: one
  missed or slow probe is tolerated, two are not). The check itself never talks to the relay.
- ``db_log_statement_none`` (ARCH-078, plan assess-foundations): ``SHOW log_statement`` is
  ``none`` and ``SHOW log_parameter_max_length`` is ``0`` on the worker's own connection.

A failed item is ``MISSING`` with a reason code naming the cause (the check has no separate
"failed" result, and the item table stores ok | missing). A database error raises and the runner
reports ``UNVERIFIABLE/check_error``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from vektra_assess.moodle.setup_check import (
    CheckContext,
    ItemCheck,
    ItemRegistry,
    Outcome,
    missing,
    ok,
)
from vektra_assess.worker.runner import AcquireDb
from vektra_assess.worker.slots import SMTP_PROBE_INTERVAL_S

SMTP_PROBE_MAX_AGE_S = 3 * SMTP_PROBE_INTERVAL_S

_SMTP_ROW = text(
    "SELECT details, extract(epoch FROM now() - beat_at) AS age "
    "FROM assess_heartbeat WHERE component = 'smtp'"
)
_LOGGING = text(
    "SELECT current_setting('log_statement'), current_setting('log_parameter_max_length')"
)


def _code(value: Any) -> str:
    v = str(value or "unknown").lower()
    return v if v.replace("_", "").isalnum() and len(v) <= 40 else "unknown"


def smtp_probe_item(acquire: AcquireDb, *, configured: bool) -> ItemCheck:
    async def check(_ctx: CheckContext) -> Outcome:
        if not configured:
            return missing("smtp_not_configured")
        async with acquire() as conn:
            row = (await conn.execute(_SMTP_ROW)).first()
        if row is None:
            return missing("smtp_never_probed")
        details = row.details if isinstance(row.details, dict) else {}
        if float(row.age) > SMTP_PROBE_MAX_AGE_S:
            return missing("smtp_probe_stale")
        if details.get("ok") is not True:
            cause = details.get("error_class") or details.get("state")
            return missing(f"smtp_{_code(cause)}")
        return ok()

    return check


def db_log_statement_item(acquire: AcquireDb) -> ItemCheck:
    async def check(_ctx: CheckContext) -> Outcome:
        async with acquire() as conn:
            statement, max_length = (await conn.execute(_LOGGING)).one()
        problems = []
        if statement != "none":
            problems.append("log_statement_not_none")
        if str(max_length) != "0":
            problems.append("log_parameter_max_length_not_0")
        return missing("_and_".join(problems)) if problems else ok()

    return check


def register_worker_items(
    registry: ItemRegistry, acquire: AcquireDb, *, smtp_configured: bool
) -> None:
    registry.register(
        "smtp_probe", smtp_probe_item(acquire, configured=smtp_configured)
    )
    registry.register("db_log_statement_none", db_log_statement_item(acquire))
