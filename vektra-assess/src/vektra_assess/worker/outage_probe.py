"""Outage probe tick (REQ-086, plan assess-session-lifecycle M4.2, M4.5).

Every 5-10 s (the ``outage_probe`` tick), and only while at least one session is in OUTAGE:

1. sessions whose cumulative outage passed C = min(15 min, half the duration) are closed
   ``service_failure`` through the plan-2 close function (which enqueues the transcript and the
   teacher notification in the same transaction);
2. one minimal call probes the provider (``max_tokens=1``). There is one discussion provider per
   deployment (OQ-032), so one probe answers for every OUTAGE session. litellm's ``health_check``
   is NOT used: it makes a full, paid completion;
3. on success every OUTAGE session ends through ``assess_end_outage``: the interval ends now, the
   credit is its length when at least G (``VEKTRA_ASSESS_OUTAGE_MIN_CREDIT_S``), capped so the
   cumulative credit stays within C, the deadline moves by the one formula, ``assess_outage_end``
   is audited, and the session is ACTIVE again. The API's ``PendingTurnDriver`` then answers the
   pending turn without a client.

A failed probe changes nothing: the interval stays open and keeps counting towards C.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import text

from vektra_assess.timing import OUTAGE_CAP_MAX_S
from vektra_assess.worker.runner import AcquireDb

log = structlog.get_logger(__name__)

Probe = Callable[[], Awaitable[bool]]

_OVER_CAP = text(
    "SELECT s.id, s.epoch FROM assess_sessions s WHERE s.state = 'OUTAGE' AND ("
    " SELECT coalesce(sum(extract(epoch FROM coalesce(i.ended_at, now()) - i.started_at)), 0)"
    " FROM assess_outage_incidents i WHERE i.session_id = s.id"
    ") > least(:cap_max, coalesce(s.duration_s, 0) / 2) LIMIT :n"
)
_IN_OUTAGE = text(
    "SELECT id FROM assess_sessions WHERE state = 'OUTAGE' ORDER BY id LIMIT :n"
)
_CLOSE = text("SELECT assess_close_session(:sid, :epoch, 'service_failure')")
_END = text("SELECT assess_end_outage(:sid, :g)")


@dataclass(frozen=True)
class ProbeResult:
    closed: int = 0
    probed: bool = False
    ok: bool = False
    ended: int = 0


async def close_over_cap(acquire: AcquireDb, *, batch: int = 200) -> int:
    async with acquire() as conn:
        due = (
            await conn.execute(_OVER_CAP, {"cap_max": OUTAGE_CAP_MAX_S, "n": batch})
        ).all()
    closed = 0
    for row in due:
        async with acquire() as conn:
            async with conn.begin():
                ok = (
                    await conn.execute(_CLOSE, {"sid": row.id, "epoch": row.epoch})
                ).scalar()
        closed += bool(ok)
    if closed:
        log.warning("assess_sessions_service_failure", count=closed, cause="outage_cap")
    return closed


async def outage_tick(
    acquire: AcquireDb,
    probe: Probe | None,
    *,
    min_credit_s: int,
    batch: int = 200,
) -> ProbeResult:
    closed = await close_over_cap(acquire, batch=batch)
    async with acquire() as conn:
        sids = [r.id for r in (await conn.execute(_IN_OUTAGE, {"n": batch})).all()]
    if not sids or probe is None:
        return ProbeResult(closed=closed)
    try:
        ok = bool(await probe())
    except Exception as exc:  # a failed probe is an answer, never a crash of the tick
        log.info("assess_outage_probe_failed", error_type=type(exc).__name__)
        ok = False
    if not ok:
        return ProbeResult(closed=closed, probed=True)
    ended = 0
    for sid in sids:
        async with acquire() as conn:
            async with conn.begin():
                credited = (
                    await conn.execute(_END, {"sid": sid, "g": min_credit_s})
                ).scalar()
        if credited is not None and credited >= 0:
            ended += 1
        elif credited == -1:
            closed += 1
    log.info("assess_outage_probe_ok", ended=ended, closed=closed)
    return ProbeResult(closed=closed, probed=True, ok=True, ended=ended)


def make_llm_probe(llm: Any, model: str, *, timeout_s: float) -> Probe:
    """One minimal completion (``max_tokens=1``) through the discussion provider; True on any
    answer within ``timeout_s``. No content of any session is sent."""
    from vektra_shared.types import Message

    async def probe() -> bool:
        async with asyncio.timeout(timeout_s):
            await llm.complete(
                [Message(role="user", content="ping")],
                model,
                temperature=0.0,
                max_tokens=1,
            )
        return True

    return probe
