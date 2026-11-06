"""The Start pre-flight (ARCH-068, REQ-071, REQ-094): commit or roll back, never in between.

Bound to the ``preflight`` job kind. The job carries the session id only; every step re-reads the
authoritative rows. ``created_at`` of the STARTING row is the reference point of the REQ-071
window T (``VEKTRA_ASSESS_START_T_S``), measured on the DATABASE clock.

1. Admission (NFR-020), under a transaction-scoped advisory lock: ACTIVE + OUTAGE sessions plus
   STARTING sessions already in pre-flight must stay below K. Admitted -> ``preflight_started_at``.
2. Activity configuration: enabled, with a cap (BR-013); duration = cap + extra time, frozen.
3. Model activation (NFR-023): ``is_activated`` on every Start, so a new activation row takes
   effect without a restart. ``VEKTRA_ASSESS_LLM_ALLOW_UNACTIVATED`` is honoured only with
   ``VEKTRA_ENV=development``.
4. Gate inputs and snapshot through the ``StartSource`` port (plan 4 readers, plan 6 capture).
5. Part A (``render_part_a``) and Part B (``run_part_b``) inside the remaining budget.
6. ONE commit transaction, guarded by ``now() - created_at < T``: snapshot (+ its exclusion audit),
   the KEK-wrapped DEK, the two opening turns (hash chain anchored on the snapshot hash), state
   ACTIVE with deadline and frozen policy, the hand-off row (DEK sealed to the API), the
   ``committed`` attempt and the ``assess_commit`` audit event.

Any server-attributable failure runs ``rollback_start``: one transaction deletes the STARTING row
(snapshot, keys, turns and hand-off cascade), records the ``rolled_back`` attempt and the
``assess_start_rollback`` audit event with a content-free reason code, then verifies by row count
that nothing of the session is left. Nothing on the client path triggers a rollback: a client that
disconnects after Start still gets its session committed.

Reason codes are fixed strings (``Reason``); no exception text, Moodle value or content is ever
written, logged or returned.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.config import VektraAssessSettings
from vektra_assess.context.assemble import assemble_context
from vektra_assess.context.bundle import ContextBundle
from vektra_assess.context.capture.capture import (
    Built,
    CaptureNotEligible,
    CaptureResult,
    Failed,
    persist_capture,
)
from vektra_assess.context.part_a import SessionFacts, render_part_a
from vektra_assess.crypto import envelope, hashing
from vektra_assess.crypto.kek import wrap_dek
from vektra_assess.crypto.seal import seal
from vektra_assess.keys import AssessKeyring
from vektra_assess.llm.activation import is_activated
from vektra_assess.llm.types import (
    PartBRejectedError,
    ProviderOutageError,
    RagNotAvailableError,
    TurnResult,
)
from vektra_assess.worker import locks
from vektra_assess.worker.runner import AcquireDb, JobContext

log = structlog.get_logger(__name__)

#: seconds kept free at the end of T for the commit transaction itself
COMMIT_MARGIN_S = 5.0
#: the ``previous_context`` line of Part A for session #N > 1 (REQ-094; plan 8 owns the wording)
PREVIOUS_CONTEXT = "Le sessioni precedenti non sono riprese in questa discussione."


class Reason(StrEnum):
    START_TIMEOUT = "start_timeout"
    CAPACITY = "capacity"
    ACTIVITY_NOT_CONFIGURED = "activity_not_configured"
    MODEL_NOT_ACTIVATED = "model_not_activated"
    CAPTURE_FAILED = "capture_failed"
    PART_B_REJECTED = "part_b_rejected"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RAG_NOT_AVAILABLE = "rag_not_available"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class StartTarget:
    session_id: UUID
    site_id: UUID
    cmid: int
    student_userid: int
    courseid_hint: int | None
    activity_type: str  # quiz | assign
    session_no: int
    flags: dict[str, Any]


class StartSource(Protocol):
    """Where a pre-flight gets its snapshot: the Moodle readers in production, fakes in tests.

    ``capture`` returns ``Built`` (with the frozen policy), ``CaptureNotEligible`` or ``Failed``;
    an exception is an internal error and rolls the Start back as well.
    """

    async def capture(self, target: StartTarget) -> CaptureResult: ...


class PartBRunner(Protocol):
    @property
    def model(self) -> str: ...

    @property
    def prompt_version(self) -> str: ...

    async def run_part_b(
        self,
        bundle: ContextBundle,
        *,
        language: str = "it",
        remaining_s: float | None = None,
    ) -> TurnResult: ...


@dataclass(frozen=True)
class PreflightDeps:
    settings: VektraAssessSettings
    kek: AssessKeyring
    handoff_public: AssessKeyring
    source: StartSource
    runner: PartBRunner
    clock: Callable[[], float] = time.monotonic


class _Rollback(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- SQL -------------------------------------------------------------------------------------

_LOAD = text(
    "SELECT s.id, s.site_id, s.cmid, s.student_userid, s.session_no, s.epoch, s.courseid_hint, "
    "s.state, extract(epoch FROM now() - s.created_at) AS elapsed_s "
    "FROM assess_sessions s WHERE s.id = :sid FOR UPDATE"
)
_LIVE = text(
    "SELECT count(*) FROM assess_sessions WHERE id <> :sid AND (state IN ('ACTIVE', 'OUTAGE') "
    "OR (state = 'STARTING' AND preflight_started_at IS NOT NULL))"
)
_ADMIT = text("UPDATE assess_sessions SET preflight_started_at = now() WHERE id = :sid")
_ACTIVITY = text(
    "SELECT c.activity_type, c.enabled, c.cap_minutes, c.flags, "
    "coalesce((SELECT e.extra_seconds FROM assess_extra_time e WHERE e.site_id = c.site_id "
    "AND e.cmid = c.cmid AND e.student_userid = :u), 0) AS extra_s "
    "FROM assess_activity_config c WHERE c.site_id = :s AND c.cmid = :c"
)
_DELETE = text(
    "DELETE FROM assess_sessions WHERE id = :sid AND state = 'STARTING' "
    "RETURNING site_id, cmid, student_userid, session_no"
)
_LEFT = text(
    "SELECT (SELECT count(*) FROM assess_sessions WHERE id = :sid) "
    "+ (SELECT count(*) FROM assess_snapshots WHERE session_id = :sid) "
    "+ (SELECT count(*) FROM assess_session_keys WHERE session_id = :sid) "
    "+ (SELECT count(*) FROM assess_turns WHERE session_id = :sid) "
    "+ (SELECT count(*) FROM assess_session_handoff WHERE session_id = :sid)"
)
_ATTEMPT = text(
    "INSERT INTO assess_start_attempts (site_id, cmid, student_userid, outcome, reason_code, "
    "session_id) VALUES (:s, :c, :u, :o, :r, :sid)"
)
_INSERT_KEY = text(
    "INSERT INTO assess_session_keys (session_id, kek_wrapped, kek_kid, key_version) "
    "VALUES (:s, :b, :k, :v)"
)
_INSERT_TURN = text(
    "INSERT INTO assess_turns (session_id, turn_no, role, kind, ciphertext, turn_hash, "
    "prev_hash) VALUES (:s, :n, 'bot', 'opening', :c, :h, :p)"
)
_ACTIVATE = text(
    "UPDATE assess_sessions SET state = 'ACTIVE', committed_at = now(), "
    "deadline_at = now() + make_interval(secs => :d), duration_s = :d, seq = :seq, "
    "frozen_policy = CAST(:fp AS jsonb), starting_expires_at = NULL, last_activity_at = now() "
    "WHERE id = :s"
)
_HANDOFF = text(
    "INSERT INTO assess_session_handoff (session_id, session_state, epoch, sealed_dek, "
    "handoff_kid) VALUES (:s, 'ACTIVE', :e, :d, :k)"
)


# --- rollback --------------------------------------------------------------------------------


class RollbackLeftovers(RuntimeError):
    """A rollback left a row of the session behind: the transaction is aborted, not committed."""


async def rollback_start(
    conn: AsyncConnection, session_id: UUID, reason: str, *, actor: str = "worker"
) -> bool:
    """Delete a STARTING session and everything under it, in the CALLER's transaction.

    Returns False when there was nothing to roll back (already committed, already rolled back).
    Raises ``RollbackLeftovers`` if any session, snapshot, key, turn or hand-off row survives the
    delete (verified by count, never assumed from the cascade).
    """
    row = (await conn.execute(_DELETE, {"sid": session_id})).first()
    if row is None:
        return False
    left = int((await conn.execute(_LEFT, {"sid": session_id})).scalar_one())
    if left:
        raise RollbackLeftovers(f"{left} row(s) of the session survived the rollback")
    await conn.execute(
        _ATTEMPT,
        {
            "s": row.site_id,
            "c": row.cmid,
            "u": row.student_userid,
            "o": "rolled_back",
            "r": reason,
            "sid": session_id,
        },
    )
    await write_event(
        conn,
        AssessAuditEvent.START_ROLLBACK,
        actor="worker" if actor == "worker" else "api",
        site_id=row.site_id,
        session_id=session_id,
        reason_code=reason,
        data={"cmid": int(row.cmid), "session_no": int(row.session_no)},
    )
    return True


async def _rollback(acquire: AcquireDb, session_id: UUID, reason: str) -> None:
    async with acquire() as conn:
        async with conn.begin():
            done = await rollback_start(conn, session_id, reason)
    log.info(
        "assess_preflight_rolled_back",
        session_id=str(session_id),
        reason=reason,
        deleted=done,
    )


# --- handler ---------------------------------------------------------------------------------


def _remaining(deps: PreflightDeps, elapsed_at_load: float, t_load: float) -> float:
    elapsed = elapsed_at_load + (deps.clock() - t_load)
    return deps.settings.start_t_s - COMMIT_MARGIN_S - elapsed


async def _admit(
    deps: PreflightDeps, acquire: AcquireDb, session_id: UUID
) -> tuple[StartTarget, int, float] | None:
    """Steps 1-3. Returns (target, duration_s, elapsed_s) or None for a stale job."""
    settings = deps.settings
    async with acquire() as conn:
        async with conn.begin():
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(:k)"), {"k": locks.ADMISSION}
            )
            row = (await conn.execute(_LOAD, {"sid": session_id})).first()
            if row is None or row.state != "STARTING":
                return None  # rolled back by the reaper, or already committed: no-op
            elapsed = float(row.elapsed_s)
            if elapsed >= settings.start_t_s:
                raise _Rollback(Reason.START_TIMEOUT)
            live = int((await conn.execute(_LIVE, {"sid": session_id})).scalar_one())
            if live >= settings.admission_k:
                raise _Rollback(Reason.CAPACITY)
            act = (
                await conn.execute(
                    _ACTIVITY,
                    {"s": row.site_id, "c": row.cmid, "u": row.student_userid},
                )
            ).first()
            if act is None or not act.enabled or act.cap_minutes is None:
                raise _Rollback(Reason.ACTIVITY_NOT_CONFIGURED)
            activated = await is_activated(
                conn, deps.runner.model, deps.runner.prompt_version
            )
            if not activated and not (
                settings.llm_allow_unactivated and settings.env == "development"
            ):
                raise _Rollback(Reason.MODEL_NOT_ACTIVATED)
            await conn.execute(_ADMIT, {"sid": session_id})
    target = StartTarget(
        session_id=session_id,
        site_id=row.site_id,
        cmid=int(row.cmid),
        student_userid=int(row.student_userid),
        courseid_hint=row.courseid_hint,
        activity_type=str(act.activity_type),
        session_no=int(row.session_no),
        flags=dict(act.flags or {}),
    )
    duration = int(act.cap_minutes) * 60 + int(act.extra_s)
    return target, duration, elapsed


async def _within(
    coro: Awaitable[Any], remaining: float, reason: str = Reason.START_TIMEOUT
) -> Any:
    if remaining <= 0:
        raise _Rollback(reason)
    try:
        async with asyncio.timeout(remaining):
            return await coro
    except TimeoutError:
        raise _Rollback(reason) from None


async def _commit(
    deps: PreflightDeps,
    acquire: AcquireDb,
    target: StartTarget,
    built: Built,
    part_a: str,
    part_b: TurnResult,
    duration_s: int,
) -> bool:
    sid = target.session_id
    dek = envelope.new_dek()
    try:
        async with acquire() as conn:
            async with conn.begin():
                row = (await conn.execute(_LOAD, {"sid": sid})).first()
                if row is None or row.state != "STARTING":
                    return False  # the reaper rolled it back meanwhile
                if float(row.elapsed_s) >= deps.settings.start_t_s:
                    raise _Rollback(Reason.START_TIMEOUT)
                genesis = await persist_capture(
                    conn, sid, dek, built, site_id=target.site_id
                )
                blob, kid, version = wrap_dek(deps.kek, dek, sid)
                await conn.execute(
                    _INSERT_KEY, {"s": sid, "b": blob, "k": kid, "v": version}
                )
                prev = genesis
                for turn_no, text_ in ((1, part_a), (2, part_b.text)):
                    plaintext = text_.encode("utf-8")
                    digest = hashing.turn_hash(
                        prev, turn_no, "bot", "opening", plaintext
                    )
                    await conn.execute(
                        _INSERT_TURN,
                        {
                            "s": sid,
                            "n": turn_no,
                            "c": envelope.encrypt_turn(dek, sid, turn_no, plaintext),
                            "h": digest,
                            "p": prev,
                        },
                    )
                    prev = digest
                await conn.execute(
                    _ACTIVATE,
                    {
                        "s": sid,
                        "d": duration_s,
                        "seq": 2,
                        "fp": built.frozen_policy.model_dump_json(),
                    },
                )
                hkid = deps.handoff_public.active
                sealed = seal(
                    deps.handoff_public.active_key,
                    hkid,
                    dek,
                    purpose=envelope.Purpose.HANDOFF,
                    binding=[sid, hkid],
                )
                await conn.execute(
                    _HANDOFF, {"s": sid, "e": int(row.epoch), "d": sealed, "k": hkid}
                )
                await conn.execute(
                    _ATTEMPT,
                    {
                        "s": target.site_id,
                        "c": target.cmid,
                        "u": target.student_userid,
                        "o": "committed",
                        "r": None,
                        "sid": sid,
                    },
                )
                await write_event(
                    conn,
                    AssessAuditEvent.COMMIT,
                    actor="worker",
                    site_id=target.site_id,
                    session_id=sid,
                    data={
                        "cmid": target.cmid,
                        "session_no": target.session_no,
                        "snapshot_hash": genesis,
                        "duration_s": duration_s,
                        "elapsed_ms": int(float(row.elapsed_s) * 1000),
                        "part_b_prompt_tokens": part_b.usage.prompt_tokens,
                        "part_b_completion_tokens": part_b.usage.completion_tokens,
                    },
                )
    finally:
        del dek
    return True


def make_preflight_handler(
    deps: PreflightDeps,
) -> Callable[[JobContext], Awaitable[None]]:
    async def handle(ctx: JobContext) -> None:
        raw = ctx.args.get("session_id")
        try:
            sid = UUID(str(raw))
        except ValueError:
            return  # a malformed job is a stale no-op: nothing to roll back
        try:
            await _run(deps, ctx.acquire, sid)
        except _Rollback as rb:
            await _rollback(ctx.acquire, sid, rb.reason)

    return handle


async def _run(deps: PreflightDeps, acquire: AcquireDb, sid: UUID) -> None:
    t_load = deps.clock()
    admitted = await _admit(deps, acquire, sid)
    if admitted is None:
        return
    target, duration_s, elapsed = admitted
    try:
        captured: CaptureResult = await _within(
            deps.source.capture(target), _remaining(deps, elapsed, t_load)
        )
        if isinstance(captured, CaptureNotEligible):
            raise _Rollback(f"not_eligible_{captured.reason.value}")
        if isinstance(captured, Failed):
            raise _Rollback(f"{Reason.CAPTURE_FAILED}_{captured.reason.value}"[:64])
        bundle = assemble_context(captured.snapshot, captured.frozen_policy)
        facts = SessionFacts(
            session_number=target.session_no,
            duration_minutes=duration_s // 60,
            previous_context=PREVIOUS_CONTEXT if target.session_no > 1 else None,
        )
        part_a = render_part_a(bundle, facts).text
        remaining = _remaining(deps, elapsed, t_load)
        part_b: TurnResult = await _within(
            deps.runner.run_part_b(bundle, remaining_s=remaining), remaining
        )
        del bundle
    except _Rollback:
        raise
    except PartBRejectedError:
        raise _Rollback(Reason.PART_B_REJECTED) from None
    except ProviderOutageError:
        raise _Rollback(Reason.PROVIDER_UNAVAILABLE) from None
    except RagNotAvailableError:
        raise _Rollback(Reason.RAG_NOT_AVAILABLE) from None
    except Exception as exc:  # any other server-side failure rolls back, by type only
        log.warning("assess_preflight_error", error_type=type(exc).__name__)
        raise _Rollback(Reason.INTERNAL_ERROR) from None
    committed = await _commit(
        deps, acquire, target, captured, part_a, part_b, duration_s
    )
    log.info(
        "assess_preflight_committed" if committed else "assess_preflight_stale",
        session_id=str(sid),
    )
