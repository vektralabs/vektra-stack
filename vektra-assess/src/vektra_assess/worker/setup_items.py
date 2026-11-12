"""Worker-side REQ-079 setup-check items that read the database (coordinator decisions,
2026-11-08, plan assess-moodle-ws Notes).

- ``smtp_probe`` (plan 10 registers it, moodle-ws plan Integration Notes): judged from the row
  the ``smtp_probe`` tick upserts into ``assess_heartbeat`` (component ``smtp``, details
  ``{state, ok, error_class, smtp_code}``, ``beat_at`` = check time). OK when the row is there,
  ``ok`` is true and ``beat_at`` is at most ``SMTP_PROBE_MAX_AGE_S`` old (3 tick intervals: one
  missed or slow probe is tolerated, two are not). With no row or a stale one, the item runs
  the probe itself (bounded, never sends) and judges that fresh result.
- ``privacy_config`` (plan assess-privacy-retention, REQ-089): OK only with the site's privacy
  settings complete and within bounds, MISSING ``privacy_config_unset`` otherwise.
- ``db_log_statement_none`` (ARCH-078, plan assess-foundations): ``SHOW log_statement`` is
  ``none`` and ``SHOW log_parameter_max_length`` is ``0`` on the worker's own connection, AND
  no ``ALTER ROLE/DATABASE ... SET`` override (``pg_db_role_setting``) gives any assess role, or
  this database, another value: the worker's session cannot see the API role's settings. Each
  assess role's effective value is resolved by PostgreSQL's precedence; when a default on the
  worker's own role hides the server value that another assess role inherits, the item is
  ``UNVERIFIABLE/server_log_settings_masked``, never OK.

A failed item is ``MISSING`` with a reason code naming the cause (the check has no separate
"failed" result, and the item table stores ok | missing). A database error raises and the runner
reports ``UNVERIFIABLE/check_error``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text

from vektra_assess.setup import (
    CheckContext,
    ItemCheck,
    ItemRegistry,
    Outcome,
    builtin_registry,
    missing,
    ok,
    reason_code,
    unverifiable,
)
from vektra_assess.worker.runner import AcquireDb
from vektra_assess.worker.slots import SMTP_PROBE_INTERVAL_S

SMTP_PROBE_MAX_AGE_S = 3 * SMTP_PROBE_INTERVAL_S
#: the cap on the item's own probe: a quarter of the 120 s setup-check budget. A healthy relay
#: answers EHLO/STARTTLS/AUTH in seconds; the rest of the budget is kept for the provider's items,
#: the capability probes and the report
SMTP_ITEM_PROBE_MAX_S = 30.0

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
    "SELECT r.rolname, s.setdatabase <> 0, cfg FROM pg_db_role_setting s "
    "LEFT JOIN pg_roles r ON r.oid = s.setrole "
    "CROSS JOIN LATERAL unnest(s.setconfig) AS cfg "
    "WHERE (s.setdatabase = 0 OR s.setdatabase = "
    "(SELECT oid FROM pg_database WHERE datname = current_database())) "
    "AND (s.setrole = 0 OR r.rolname = ANY(:roles)) "
    "AND (cfg LIKE 'log_statement=%' OR cfg LIKE 'log_parameter_max_length=%')"
)
_CURRENT_USER = text("SELECT current_user")
_EXPECTED = {"log_statement": "none", "log_parameter_max_length": "0"}


def _code(value: Any) -> str:
    """The setup check's own reason-code rule (``[a-z0-9_]{1,64}``), ``unknown`` otherwise."""
    return reason_code(str(value or "unknown"), "unknown")


def smtp_probe_item(
    acquire: AcquireDb,
    *,
    configured: bool,
    probe: Callable[[], Awaitable[Any]] | None = None,
    probe_timeout_s: float = SMTP_ITEM_PROBE_MAX_S,
) -> ItemCheck:
    """``probe``: one whole bounded probe (``delivery.probe.run_probe``, which writes the row
    and never sends). Without a fresh row, the item runs it and judges what it wrote (review
    finding 1): at startup a setup check queued earlier can be claimed before the probe tick has
    written its first row, and a long-stopped worker finds only a stale one."""

    async def capped() -> bool:
        """Run the probe within ``probe_timeout_s``; False when the cap cut it short."""
        assert probe is not None
        try:
            async with asyncio.timeout(probe_timeout_s):
                await probe()
        except TimeoutError:
            return False
        return True

    async def read() -> Any:
        """The relay probe row with its age and the database clock."""
        async with acquire() as conn:
            return (await conn.execute(_SMTP_ROW)).first()

    async def check(_ctx: CheckContext) -> Outcome:
        """Judge the probe row, probing once when it is missing, stale or uninformative."""
        if not configured:
            return missing("smtp_not_configured")
        row = await read()
        # at most ONE probe per item run, and that one capped at
        # ``probe_timeout_s``: the rest of the 120 s budget belongs to the LMS
        # items, the capability probes and the report. Hitting the cap is UNVERIFIABLE.
        probed = False
        if probe is not None and (row is None or float(row.age) > SMTP_PROBE_MAX_AGE_S):
            probed = True
            if not await capped():
                return unverifiable("smtp_probe_timeout")
            row = await read()
        if row is None:
            return missing("smtp_never_probed")
        if float(row.age) > SMTP_PROBE_MAX_AGE_S:
            return missing("smtp_probe_stale")
        verdict = _judge(row.details, float(row.now))
        if verdict is None and probe is not None and not probed:
            # busy with no earlier result to go by: probe once more (finding 2)
            if not await capped():
                return unverifiable("smtp_probe_timeout")
            row = await read()
            if row is not None and float(row.age) <= SMTP_PROBE_MAX_AGE_S:
                verdict = _judge(row.details, float(row.now))
        # still busy: not a relay failure (never MISSING), but nothing vouches for it either
        return verdict or unverifiable("smtp_busy")

    return check


def _judge(raw: Any, now: float) -> Outcome | None:
    """OK / MISSING from one probe row; ``busy`` is judged by the last real result it carries
    while that result is itself at most ``SMTP_PROBE_MAX_AGE_S`` old; None when there is no
    such result."""
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
    """The ARCH-078 item: OK only when no assess statement or parameter can reach the logs."""

    async def check(_ctx: CheckContext) -> Outcome:
        """Judge the server and per-role logging settings."""
        async with acquire() as conn:
            statement, max_length = (await conn.execute(_LOGGING)).one()
            me = (await conn.execute(_CURRENT_USER)).scalar_one()
            rows = (
                await conn.execute(_OVERRIDES, {"roles": [*ASSESS_ROLES, me]})
            ).all()
        # (role or None for every role, this database only?) -> {setting: value}
        levels: dict[tuple[str | None, bool], dict[str, str]] = {}
        for role, per_db, cfg in rows:
            name, _, value = str(cfg).partition("=")
            levels.setdefault((role, bool(per_db)), {})[name] = value.strip("'\"")
        session = {"log_statement": statement, "log_parameter_max_length": max_length}
        problems: list[str] = []
        unknown = False
        for name in ("log_statement", "log_parameter_max_length"):
            # what this session reads is the server value only when no role or database
            # default applies to its own role
            server = (
                str(session[name]) if _effective(levels, me, name) is None else None
            )
            seen: set[str] = set()
            for role in ASSESS_ROLES:
                effective = _effective(levels, role, name)
                if effective is None:
                    effective = server
                if effective is None:
                    unknown = True
                else:
                    seen.add(effective)
            seen.add(str(session[name]))
            if seen != {_EXPECTED[name]}:
                problems.append(f"{name}_not_{_EXPECTED[name]}")
        if problems:
            return missing("_and_".join(problems))
        # a role or database default hides the server value from this session, and some
        # assess role inherits it: nothing proves that role is not logged
        return unverifiable("server_log_settings_masked") if unknown else ok()

    return check


def _effective(
    levels: dict[tuple[str | None, bool], dict[str, str]], role: str, name: str
) -> str | None:
    """A role's default for ``name`` in this database, by PostgreSQL's precedence (role in
    this database, role, every role in this database, every role); None = the server value."""
    for key in ((role, True), (role, False), (None, True), (None, False)):
        value = levels.get(key, {}).get(name)
        if value is not None:
            return value
    return None


def fresh_registry() -> ItemRegistry:
    """A new registry with the built-in items, for one composition: never the module-level
    default, which a second composition would register into twice."""
    return builtin_registry()


def register_worker_items(
    registry: ItemRegistry,
    acquire: AcquireDb,
    *,
    smtp_configured: bool,
    smtp_probe: Callable[[], Awaitable[Any]] | None = None,
) -> None:
    """Register the worker's own pluggable items into ``registry`` (one per composition)."""
    registry.register(
        "smtp_probe",
        smtp_probe_item(acquire, configured=smtp_configured, probe=smtp_probe),
    )
    registry.register("db_log_statement_none", db_log_statement_item(acquire))
    registry.register("privacy_config", privacy_config_item(acquire))


def privacy_config_item(acquire: AcquireDb) -> ItemCheck:
    """REQ-089 (plan assess-privacy-retention): OK only when the site's privacy settings are all
    set and within bounds (``privacy.config.is_configured``); MISSING otherwise, so a site can
    never pass its setup check with the retention, legal basis or notice unset."""

    async def check(ctx: CheckContext) -> Outcome:
        """Judge the site's stored privacy settings."""
        from uuid import UUID

        from vektra_assess.privacy.config import is_configured

        async with acquire() as conn:
            configured = await is_configured(conn, UUID(str(ctx.site.site_id)))
        return ok() if configured else missing("privacy_config_unset")

    return check
