"""SMTP readiness probe (INT-003): EHLO + STARTTLS + AUTH + QUIT, never a MAIL command.

The ``smtp_probe`` tick runs it every few minutes (and at startup) and upserts its result into
``assess_heartbeat`` under the component ``smtp``: ``{"state", "ok", "error_class", "smtp_code"}`` with
``beat_at`` as the time of the check. ``/health/assess`` (plan 12) reads that row and its age;
nothing else is stored (no host, no account, no reply text). A failing probe raises the same
alert as a failing send of that class.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

import structlog
from sqlalchemy import text

from vektra_assess.delivery.smtp import ALERTING, ErrorClass, SendResult, SlotBusy

log = structlog.get_logger(__name__)

COMPONENT = "smtp"

_UPSERT = text(
    "INSERT INTO assess_heartbeat (component, beat_at, details) "
    "VALUES (:c, now(), CAST(:d AS jsonb)) ON CONFLICT (component) "
    "DO UPDATE SET beat_at = now(), details = EXCLUDED.details"
)


#: a busy outcome keeps the previous real result as ``last`` = ``{state, ok, error_class, at}``
#: (``at``: epoch seconds of that probe), carried unchanged over a busy
#: row's own ``last``; null when there is none. Taken from the row the UPSERT conflicts with,
#: in the same statement, so a result another probe writes concurrently is never dropped.
_UPSERT_BUSY = text(
    "INSERT INTO assess_heartbeat (component, beat_at, details) "
    "VALUES (:c, now(), CAST(:d AS jsonb) || jsonb_build_object('last', NULL)) "
    "ON CONFLICT (component) DO UPDATE SET beat_at = now(), "
    "details = EXCLUDED.details || jsonb_build_object('last', CASE "
    "WHEN assess_heartbeat.details->>'state' = 'busy' THEN assess_heartbeat.details->'last' "
    "ELSE jsonb_build_object('state', assess_heartbeat.details->'state', "
    "'ok', assess_heartbeat.details->'ok', "
    "'error_class', assess_heartbeat.details->'error_class', "
    "'at', extract(epoch FROM assess_heartbeat.beat_at)) END)"
)


class Prober(Protocol):
    async def probe(self) -> SendResult: ...


async def run_probe(
    prober: Prober,
    acquire: Callable[[], AbstractAsyncContextManager[Any]],
    *,
    timeout_s: float | None = None,
) -> SendResult:
    """Probe once and ALWAYS write the row: ``state`` is ``ok``, ``failed`` (with the class),
    ``timeout`` (the probe did not finish within ``timeout_s``) or ``busy`` (no relay session
    freed up within the bounded wait)."""
    state = "ok"
    try:
        async with asyncio.timeout(timeout_s):
            result = await prober.probe()
        if not result.ok:
            state = "failed"
    except TimeoutError:
        result, state = SendResult(False, ErrorClass.CONNECT), "timeout"
    except SlotBusy:
        result, state = SendResult(False), "busy"
    details = {
        "state": state,
        "ok": result.ok,
        "error_class": result.error_class.value
        if state == "failed" and result.error_class
        else None,
        "smtp_code": result.smtp_code,
    }
    async with acquire() as conn, conn.begin():
        # busy says nothing about the relay (every session was in use by deliveries): keep
        # the last real result next to it, so a reader can still judge the relay
        upsert = _UPSERT_BUSY if state == "busy" else _UPSERT
        await conn.execute(upsert, {"c": COMPONENT, "d": json.dumps(details)})
    if result.ok:
        return result
    cls = details["error_class"] or state
    if (
        result.error_class in ALERTING and state == "failed"
    ):  # TLS or AUTH: an operator acts
        log.error("assess.delivery.alert", alert=f"smtp_probe_{cls}", error_code=cls)
    else:
        log.warning("assess_smtp_probe_failed", error_code=cls)
    return result
