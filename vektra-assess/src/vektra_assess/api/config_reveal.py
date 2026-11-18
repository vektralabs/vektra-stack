"""The API half of a teacher's read-back of one sealed config field, "Mostra" (FEAT-030).

OQ-034, resolved by the product owner on 2026-11-18: a fresh EDITOR of the activity may read
back a stored ``solution``, ``pasted_rubric`` or ``transcript_recipients`` without the LMS storing
it. The API still holds NO private key of the teacher config (ARCH-071): for one request it

1. verifies the actor exactly as the PUT does (``verify_teacher(..., role="editor")``: the same
   refusals, the same stale gate and queued refresh), and checks the field is set at all (the
   API's ``is_set`` column: an unset field is 404 at once, no job);
2. generates an EPHEMERAL keypair in memory (``crypto.reveal.EphemeralKey``), then, in one
   transaction bound to the site, reads the database clock ``t0`` and queues a ``config_reveal``
   job (args: site, cmid, field, a fresh request id, the ephemeral PUBLIC key; one attempt);
3. polls ``assess_config_reveals`` for the row of THAT request, site, cmid and field written at or
   after ``t0`` (backoff 50 -> 500 ms, at most ``VEKTRA_ASSESS_CONFIG_REVEAL_TIMEOUT_S``; a client
   that goes away ends the wait), and in the same transaction opens it in memory, writes the
   audit row (who, which activity, which field, never the value) and DELETES the row.

The value is returned once, with ``Cache-Control: no-store``, and is never logged or stored by the
API. Every request is counted in ``vektra_assess_config_reveal_total{outcome}`` (a closed set).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from vektra_assess.api.common import engine_of, fail, site_of_key
from vektra_assess.api.material_reads import POLL_FIRST_S, POLL_MAX_S
from vektra_assess.api.site_binding import bind_site
from vektra_assess.api.teachers import verify_teacher
from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.crypto.reveal import EphemeralKey
from vektra_assess.crypto.teacher_config import TRANSCRIPT_RECIPIENTS
from vektra_assess.errors import config_field_not_set, config_reveal_unavailable
from vektra_assess.jobs import JobKind, enqueue_job
from vektra_assess.teacher.schemas import ConfigReveal

log = structlog.get_logger(__name__)

REVEALED = "revealed"
NOT_SET = "not_set"
REFUSED = "refused"
TIMEOUT = "timeout"
ENQUEUE_FAILED = "enqueue_failed"
UNREADABLE = "unreadable"
STOPPED = "stopped"
#: the closed label set of ``vektra_assess_config_reveal_total``
OUTCOMES = (REVEALED, NOT_SET, REFUSED, TIMEOUT, ENQUEUE_FAILED, UNREADABLE, STOPPED)

_IS_SET = text(
    "SELECT is_set FROM assess_activity_config_sealed "
    "WHERE site_id = :s AND cmid = :c AND field = :f"
)
_CLOCK = text("SELECT clock_timestamp()")
_ROW = text(
    "SELECT status, sealed_value FROM assess_config_reveals WHERE id = :rid "
    "AND site_id = :s AND cmid = :c AND field = :f AND created_at >= :t0"
)
_DELETE = text("DELETE FROM assess_config_reveals WHERE id = :rid AND site_id = :s")

_counter: Any = None


def count_outcome(outcome: str) -> None:
    """One read-back by closed outcome; a metrics failure never fails the request."""
    global _counter
    try:
        if _counter is None:
            from prometheus_client import Counter

            _counter = Counter(
                "vektra_assess_config_reveal_total",
                "Teacher read-backs of a sealed config field ('Mostra'), by outcome: "
                "revealed, not_set, refused (actor not a fresh editor), timeout, "
                "enqueue_failed, unreadable, stopped (client gone)",
                ["outcome"],
            )
        _counter.labels(outcome=outcome if outcome in OUTCOMES else "other").inc()
    except Exception as exc:
        log.warning("assess_config_reveal_metric_failed", error_type=type(exc).__name__)


def dedupe_key(request_id: UUID) -> str:
    return f"config_reveal:{request_id}"


async def _wait(stop: Callable[[], Awaitable[bool]], seconds: float) -> bool:
    """Sleep ``seconds``; True when the client went away (checked before the sleep)."""
    if await stop():
        return True
    await asyncio.sleep(seconds)
    return False


def _value(field: str, plain: bytes) -> str | list[str] | None:
    """The opened answer as the response carries it; None when it has the wrong shape."""
    try:
        decoded = plain.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if field != TRANSCRIPT_RECIPIENTS:
        return decoded
    try:
        doc = json.loads(decoded)
    except (json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(doc, list) or not all(isinstance(a, str) for a in doc):
        return None
    return list(doc)


async def reveal_config_field(
    request: Request,
    key: Any,
    cmid: int,
    body: ConfigReveal,
    field: str,
    *,
    settings: Any,
    clock: Callable[[], float] = time.monotonic,
) -> JSONResponse:
    started = clock()
    engine = engine_of(request)
    site: UUID | None = None

    def done(result: str) -> None:
        count_outcome(result)
        log.info(
            "assess_config_reveal",
            site_id=str(site) if site is not None else None,
            cmid=cmid,
            field=field,
            outcome=result,
            duration_ms=int((clock() - started) * 1000),
        )

    refused: Any = None
    async with engine.begin() as conn:
        site = await site_of_key(conn, key, "teacher")
        _gate, refused = await verify_teacher(
            conn, site=site, cmid=cmid, actor=body.actor_userid, courseid=body.courseid,
            kind=body.activity_type, max_age_s=settings.teacher_gate_max_age_s,
            route="reveal_config", role="editor",
        )  # fmt: skip
        is_set = (
            None
            if refused is not None
            else (
                await conn.execute(_IS_SET, {"s": site, "c": cmid, "f": field})
            ).scalar()
        )
    if refused is not None:
        done(REFUSED)
        raise refused  # after commit: the refusal stays audited, the refresh queued
    if not is_set:
        done(NOT_SET)
        raise fail(config_field_not_set(field))

    eph = EphemeralKey.generate()
    rid = uuid4()
    try:
        async with engine.begin() as conn:
            await bind_site(conn, site)
            t0 = (await conn.execute(_CLOCK)).scalar()
            queued = await enqueue_job(
                conn,
                JobKind.CONFIG_REVEAL,
                {
                    "site_id": str(site),
                    "cmid": cmid,
                    "field": field,
                    "request_id": str(rid),
                    "public_key": eph.public_hex,
                },
                dedupe_key=dedupe_key(rid),
                max_attempts=1,
            )
    except Exception as exc:
        log.warning(
            "assess_config_reveal_enqueue_failed", error_type=type(exc).__name__
        )
        queued = False
    if not queued:
        done(ENQUEUE_FAILED)
        raise fail(config_reveal_unavailable())

    async def stop() -> bool:
        try:
            return await request.is_disconnected()
        except Exception:  # never let the probe itself fail the request
            return False

    deadline = started + float(settings.config_reveal_timeout_s)
    delay = POLL_FIRST_S
    while True:
        answer: tuple[str, str | list[str] | None] | None = None
        try:
            async with engine.begin() as conn:
                await bind_site(conn, site)
                row = (
                    await conn.execute(
                        _ROW, {"rid": rid, "s": site, "c": cmid, "f": field, "t0": t0}
                    )
                ).first()
                if row is not None:
                    answer = _open(eph, row, site, cmid, field, rid)
                    await conn.execute(_DELETE, {"rid": rid, "s": site})
                    if answer[0] == REVEALED:
                        await write_event(
                            conn,
                            AssessAuditEvent.TEACHER_CONFIG_REVEALED,
                            actor="api",
                            site_id=site,
                            reason_code=field,
                            data={
                                "cmid": cmid,
                                "actor": body.actor_userid,
                                "field": field,
                            },
                        )
        except Exception as exc:  # a database hiccup: try again until the deadline
            log.warning(
                "assess_config_reveal_poll_failed", error_type=type(exc).__name__
            )
            answer = None
        if answer is not None:
            break
        left = deadline - clock()
        if left <= 0:
            done(TIMEOUT)
            raise fail(config_reveal_unavailable())
        if await _wait(stop, min(delay, left)):
            done(STOPPED)
            raise fail(config_reveal_unavailable())
        delay = min(delay * 2, POLL_MAX_S)

    result, value = answer
    done(result)
    if result == NOT_SET:
        raise fail(config_field_not_set(field))
    if result != REVEALED:
        raise fail(config_reveal_unavailable())
    return JSONResponse(
        {"field": field, "value": value}, headers={"Cache-Control": "no-store"}
    )


def _open(
    eph: EphemeralKey, row: Any, site: UUID, cmid: int, field: str, rid: UUID
) -> tuple[str, str | list[str] | None]:
    """(outcome, value) of the worker's answer, opened in memory only."""
    if row.status == "not_set":
        return NOT_SET, None
    if row.status != "ok" or row.sealed_value is None:
        return UNREADABLE, None
    try:
        plain = eph.open(bytes(row.sealed_value), site, cmid, field, rid)
    except Exception:  # InvalidTag, a malformed envelope: never guessed
        return UNREADABLE, None
    value = _value(field, plain)
    return (REVEALED, value) if value is not None else (UNREADABLE, None)
