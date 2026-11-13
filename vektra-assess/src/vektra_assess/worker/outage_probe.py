"""Outage probe tick (REQ-086, plan assess-session-lifecycle M4.2, M4.5).

Every 5-10 s (the ``outage_probe`` tick), and only while at least one session is in OUTAGE:

1. sessions whose cumulative outage passed C = min(15 min, half the duration) are closed
   ``service_failure`` through the plan-2 close function (which enqueues the transcript and the
   teacher notification in the same transaction);
2. one small call probes the provider (``PROBE_MAX_TOKENS``, through the discussion provider
   with its extra body and temperature). There is one discussion provider per deployment
   (OQ-032), so one probe answers for every OUTAGE session. litellm's ``health_check`` is NOT
   used: it ignores the assess extra body and its budget is too small for a reasoning model;
3. on success every OUTAGE session ends through ``assess_end_outage``: the interval ends now, the
   credit is its length when at least G (``VEKTRA_ASSESS_OUTAGE_G_S``), capped so the
   cumulative credit stays within C, the deadline moves by the one formula, ``assess_outage_end``
   is audited, and the session is ACTIVE again. The API's ``PendingTurnDriver`` then answers the
   pending turn without a client.

"Success" is any completed response, whatever its ``finish_reason`` (``length`` included) and
even with empty content: a reasoning model may spend the whole probe budget on reasoning.

A failed probe changes nothing: the interval stays open and keeps counting towards C. A probe
the provider REFUSES (a refusal cause: ``auth_error``, ``http_4xx``, ``budget_exceeded``) is
reachable but misconfigured: the next turn would be refused as well, so the outage is NOT
ended; the tick logs ``assess_outage_probe_failed`` with ``reason_code=provider_refused`` and
the closed cause at warning level, so the operator looks at the configuration, not the network.
Left unfixed, the outage reaches C and the session closes ``service_failure`` as usual.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import text

from vektra_assess.llm.runner import outage_cause
from vektra_assess.llm.types import REFUSAL_CAUSES
from vektra_assess.worker.runner import AcquireDb

log = structlog.get_logger(__name__)

Probe = Callable[[], Awaitable[bool]]

#: the probe's completion budget. Not 1: an OpenAI reasoning model refuses ``max_tokens=1`` with
#: a 400 (measured: 1 refused, 16 answered with ``finish_reason=length``), and the probe could
#: then never end an outage. 32 leaves headroom over 16 and stays below the 64 floor of
#: VEKTRA_ASSESS_LLM_MAX_TOKENS, so the configured budget never caps it in practice
PROBE_MAX_TOKENS = 32

#: C and the spent outage from their one SQL definition (revision 0032)
_OVER_CAP = text(
    "SELECT s.id, s.epoch FROM assess_sessions s WHERE s.state = 'OUTAGE' "
    "AND assess_outage_spent(s.id) > assess_outage_cap(s.duration_s) LIMIT :n"
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
    #: the closed refusal cause when the provider answered the probe with a refusal
    refused_cause: str | None = None


async def close_over_cap(acquire: AcquireDb, *, batch: int = 200) -> int:
    async with acquire() as conn:
        due = (await conn.execute(_OVER_CAP, {"n": batch})).all()
    closed = 0
    for row in due:
        async with acquire() as conn:
            async with conn.begin():
                ok = (
                    await conn.execute(_CLOSE, {"sid": row.id, "epoch": row.epoch})
                ).scalar()
        closed += bool(ok)
    if closed:
        # reason_code, the allowlisted key: a bare "cause" was logged as "[dropped]"
        log.warning(
            "assess_sessions_service_failure", count=closed, reason_code="outage_cap"
        )
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
    refused: str | None = None
    try:
        ok = bool(await probe())
    except Exception as exc:  # a failed probe is an answer, never a crash of the tick
        refused = _log_failed_probe(exc)
        ok = False
    if not ok:
        return ProbeResult(closed=closed, probed=True, refused_cause=refused)
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


def _log_failed_probe(exc: BaseException) -> str | None:
    """One content-free line on why the probe failed; the refusal cause, or None.

    The closed cause comes from the runner's classifier (type names and integer statuses only,
    never the message). A refusal is the provider answering: logged ``provider_refused`` at
    warning level, since only the operator can fix it. Anything else is the provider or the
    network being unavailable, the expected state during an outage: ``provider_unavailable``.
    """
    cause, status = outage_cause(exc)
    refused = cause in REFUSAL_CAUSES
    (log.warning if refused else log.info)(
        "assess_outage_probe_failed",
        reason_code="provider_refused" if refused else "provider_unavailable",
        outage_cause=cause,
        http_status=status,
        error_type=type(exc).__name__,
    )
    return cause if refused else None


def make_llm_probe(
    llm: Any,
    model: str,
    *,
    timeout_s: float,
    llm_max_tokens: int | None = None,
    temperature: float = 0.0,
) -> Probe:
    """One small completion through the discussion provider; True on any completed answer
    within ``timeout_s`` (any ``finish_reason``, empty content included). No content of any
    session is sent.

    ``llm`` is the discussion provider itself, so the deployment's extra body
    (``VEKTRA_ASSESS_LLM_EXTRA_BODY``, e.g. ``reasoning_effort``) reaches the probe exactly as it
    reaches a turn. The budget is ``PROBE_MAX_TOKENS``, never above ``llm_max_tokens``;
    ``temperature`` is the discussion's, since some reasoning models accept only their own.
    """
    from vektra_shared.types import Message

    budget = PROBE_MAX_TOKENS
    if llm_max_tokens is not None:
        budget = max(1, min(budget, llm_max_tokens))

    async def probe() -> bool:
        async with asyncio.timeout(timeout_s):
            await llm.complete(
                [Message(role="user", content="ping")],
                model,
                temperature=temperature,
                max_tokens=budget,
            )
        return True

    return probe
