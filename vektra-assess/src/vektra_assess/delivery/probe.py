"""SMTP readiness probe (INT-003): EHLO + STARTTLS + AUTH + QUIT, never a MAIL command.

The ``smtp_probe`` tick runs it every few minutes (and at startup) and upserts its result into
``assess_heartbeat`` under the component ``smtp``: ``{"ok", "error_class", "smtp_code"}`` with
``beat_at`` as the time of the check. ``/health/assess`` (plan 12) reads that row and its age;
nothing else is stored (no host, no account, no reply text). A failing probe raises the same
alert as a failing send of that class.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

import structlog
from sqlalchemy import text

from vektra_assess.delivery.smtp import ALERTING, SendResult

log = structlog.get_logger(__name__)

COMPONENT = "smtp"

_UPSERT = text(
    "INSERT INTO assess_heartbeat (component, beat_at, details) "
    "VALUES (:c, now(), CAST(:d AS jsonb)) ON CONFLICT (component) "
    "DO UPDATE SET beat_at = now(), details = EXCLUDED.details"
)


class Prober(Protocol):
    async def probe(self) -> SendResult: ...


async def run_probe(
    prober: Prober, acquire: Callable[[], AbstractAsyncContextManager[Any]]
) -> SendResult:
    result = await prober.probe()
    details = {
        "ok": result.ok,
        "error_class": result.error_class.value if result.error_class else None,
        "smtp_code": result.smtp_code,
    }
    async with acquire() as conn, conn.begin():
        await conn.execute(_UPSERT, {"c": COMPONENT, "d": json.dumps(details)})
    if result.ok:
        return result
    cls = details["error_class"]
    if result.error_class in ALERTING:  # TLS or AUTH: an operator must act
        log.error("assess.delivery.alert", alert=f"smtp_probe_{cls}", error_code=cls)
    else:
        log.warning("assess_smtp_probe_failed", error_code=cls)
    return result
