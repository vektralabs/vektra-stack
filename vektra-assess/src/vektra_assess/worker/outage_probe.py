"""Outage probe tick (REQ-086, plan assess-session-lifecycle M4.2, M4.5).

Every 5-10 s (the ``outage_probe`` tick), and only while at least one session is in OUTAGE:

1. sessions whose cumulative outage passed C = min(15 min, half the duration) are closed
   ``service_failure`` through the plan-2 close function (which enqueues the transcript and the
   teacher notification in the same transaction);
2. one call probes the provider, shaped like a discussion turn (``make_llm_probe``): streamed,
   through the discussion provider (its model, key and ``VEKTRA_ASSESS_LLM_EXTRA_BODY``), with
   the turn's ``VEKTRA_ASSESS_LLM_MAX_TOKENS``, temperature and seed, and the turn's first-token
   deadline. There is one discussion provider per deployment (OQ-032), so one probe answers for
   every OUTAGE session. litellm's ``health_check`` is NOT used: it is not shaped like a turn;
3. on success every OUTAGE session ends through ``assess_end_outage``: the interval ends now, the
   credit is its length when at least G (``VEKTRA_ASSESS_OUTAGE_G_S``), capped so the
   cumulative credit stays within C, the deadline moves by the one formula, ``assess_outage_end``
   is audited, and the session is ACTIVE again. The API's ``PendingTurnDriver`` then answers the
   pending turn without a client.

"Success" is a stream that produced its first text, or that completed (any ``finish_reason``,
``length`` included, even with no text: a reasoning model may spend its budget on reasoning).
The probe stops reading at the first text, so a recovered provider costs the reasoning and a
token, not a whole answer.

A failed probe changes nothing: the interval stays open and keeps counting towards C. A probe
the provider REFUSES for its configuration (``auth_error``, a non-429 ``http_4xx``, or the
library's ``budget_exceeded``) is reachable but misconfigured: the next turn would be refused as
well, so the outage is NOT ended; the tick logs ``assess_outage_probe_failed`` with
``reason_code=provider_refused`` and the closed cause at warning level. A 429 is transient: it
is logged like an unreachable provider (``provider_unavailable``, info). Left unfixed, the outage
reaches C and the session closes ``service_failure`` as usual.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import text

from vektra_assess.llm.runner import outage_cause
from vektra_assess.llm.types import is_config_refusal
from vektra_assess.worker.runner import AcquireDb

log = structlog.get_logger(__name__)

Probe = Callable[[], Awaitable[bool]]

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
    try:
        ok = bool(await probe())
    except Exception as exc:  # a failed probe is an answer, never a crash of the tick
        _log_failed_probe(exc)
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


def _log_failed_probe(exc: BaseException) -> None:
    """One content-free line on why the probe failed.

    The closed cause comes from the runner's classifier (type names and integer statuses only,
    never the message). A configuration refusal (``is_config_refusal``) or the library's spend
    limit is logged ``provider_refused`` at warning level, since only the operator can fix it.
    Anything else, a 429 included, is the provider or the network being unavailable for now, the
    expected state during an outage: ``provider_unavailable`` at info level.
    """
    cause, status = outage_cause(exc)
    refused = is_config_refusal(cause, status) or cause == "budget_exceeded"
    (log.warning if refused else log.info)(
        "assess_outage_probe_failed",
        reason_code="provider_refused" if refused else "provider_unavailable",
        outage_cause=cause,
        http_status=status,
        error_type=type(exc).__name__,
    )


def make_llm_probe(
    llm: Any,
    model: str,
    *,
    timeout_s: float,
    max_tokens: int,
    temperature: float,
    seed: int | None,
) -> Probe:
    """A probe call shaped like a discussion turn's (``DiscussionTurnRunner._open_stream``): the
    same provider method (``stream``), model, ``max_tokens``, ``temperature`` and ``seed``, and
    ``llm`` is the discussion provider itself, so its extra body (``reasoning_effort``, ...)
    reaches the probe as it reaches a turn. A provider that refuses those parameters refuses
    the probe too; only the messages differ (one fixed word, no session content).

    True as soon as the stream yields its first text, or when it completes without any (any
    finish reason); the stream is closed there, so the probe never reads a whole answer.
    ``timeout_s`` covers the open and the first text, like the turn's first-token deadline.
    """
    from vektra_shared.types import Message

    extra: dict[str, object] = {} if seed is None else {"seed": seed}

    async def probe() -> bool:
        async with asyncio.timeout(timeout_s):
            stream = await llm.stream(
                [Message(role="user", content="ping")],
                model,
                temperature=temperature,
                max_tokens=max_tokens,
                **extra,
            )
            it = stream.__aiter__()
            try:
                async for chunk in it:
                    if chunk.content.strip():
                        break
            finally:
                close = getattr(it, "aclose", None)
                if close is not None:
                    await close()
        return True

    return probe
