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
4. The START gates (BR-006 (a)-(i), BR-007, BR-010, BR-014, REQ-091): the Moodle half read fresh
   through the ``StartSource`` port (plan-4 readers), the database half here, evaluated by the pure
   ``evaluate_start_gates``; a failed gate rolls back with its code (a team submission is also
   audited ``assess_team_submission_refused``). Then the snapshot (plan 6 capture), whose own
   NotEligible reasons map to the same gate codes.
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
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vektra_assess import privacy
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
from vektra_assess.context.capture.selection import NotEligibleReason
from vektra_assess.context.part_a import SessionFacts, render_part_a
from vektra_assess.crypto import envelope, hashing
from vektra_assess.crypto.kek import wrap_dek
from vektra_assess.crypto.seal import seal
from vektra_assess.gates import Code, GateInputs, evaluate_start_gates
from vektra_assess.keys import AssessKeyring
from vektra_assess.llm.activation import is_activated
from vektra_assess.llm.types import (
    PartBRejectedError,
    ProviderOutageError,
    RagNotAvailableError,
    TurnResult,
)
from vektra_assess.timing import session_duration_s
from vektra_assess.worker import locks
from vektra_assess.worker.policy import (
    COMMIT_MARGIN_S,
)  # one value, shared with the policy
from vektra_assess.worker.runner import AcquireDb, JobContext
from vektra_assess.worker.start_guard import StartLimits, after_commit, after_rollback

log = structlog.get_logger(__name__)

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
    COMMIT_FAILED = (
        "commit_failed"  # COMMIT outcome unknown, the row re-read as STARTING
    )


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


@dataclass(frozen=True)
class DbGateFacts:
    """The START-gate inputs the pre-flight reads from its own database, in the admission tx."""

    now: int
    end_at: int | None
    teacher_opening_at: int | None
    closed_sessions: int
    reset_grants: int
    privacy_configured: bool


@dataclass(frozen=True)
class LmsGateFacts:
    """The START-gate inputs that come from the LMS, read fresh at pre-flight through the
    ``LmsProvider`` and derived by ``LmsStartSource``.

    ``None`` = unreadable (fails closed). The database-side inputs (enabled, sessions, resets,
    teacher date, end date, privacy) are added by the pre-flight itself.
    """

    team_submission: bool | None = None
    grading_method: str | None = None
    attempt_in_progress: bool | None = None
    has_submitted_attempt: bool | None = None
    regrade_in_progress: bool | None = None
    fully_graded: bool | None = None
    released: bool | None = None
    activity_close_at: int | None = None
    grace_s: int | None = None
    participant_closes: tuple[int, ...] | None = None
    own_close_at: int | None = None
    owner_verified: bool | None = None
    enrolled: bool | None = None


class StartSource(Protocol):
    """Where a pre-flight gets its gate inputs and its snapshot: ``LmsStartSource`` over the
    configured LMS provider in production, fakes in tests.

    ``gate_facts`` reads the LMS half of the START gates; ``capture`` returns ``Built`` (with
    the frozen policy), ``CaptureNotEligible`` or ``Failed``. An exception from either is an
    internal error and rolls the Start back as well.
    """

    async def gate_facts(self, target: StartTarget) -> LmsGateFacts: ...

    async def capture(self, target: StartTarget) -> CaptureResult: ...

    async def enrolled(self, target: StartTarget) -> bool | None:
        """Still an ACTIVE student of the activity's course? None when unreadable (M3.7)."""
        ...


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


PrivacyCheck = Callable[[AsyncConnection, UUID], Awaitable[bool]]


async def privacy_not_configured(conn: AsyncConnection, site: UUID) -> bool:
    """BR-006 (h) until plan assess-privacy-retention binds the REQ-089 store: not configured
    (``vektra_assess.privacy.is_configured``, the one definition the teacher's enable uses too).

    Fail closed: every Start is rolled back with ``privacy_not_configured``. Development only,
    ``VEKTRA_ASSESS_PRIVACY_ALLOW_UNSET`` lets the gate pass (the worker refuses it elsewhere).
    """
    return await privacy.is_configured(conn, site)


@dataclass(frozen=True)
class PreflightDeps:
    settings: VektraAssessSettings
    kek: AssessKeyring
    handoff_public: AssessKeyring
    source: StartSource
    runner: PartBRunner
    clock: Callable[[], float] = time.monotonic
    privacy: PrivacyCheck = privacy_not_configured


class _AmbiguousCommit(Exception):
    """The connection failed during or after COMMIT: whether it committed is unknown."""


class _Rollback(Exception):
    def __init__(self, reason: str, extra: AssessAuditEvent | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.extra = extra


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
    "extract(epoch FROM c.end_at)::bigint AS end_at, "
    "extract(epoch FROM c.attested_opening_at)::bigint AS attested_at, "
    "extract(epoch FROM now())::bigint AS db_now, "
    "(SELECT count(*) FROM assess_sessions h WHERE h.site_id = c.site_id AND h.cmid = c.cmid "
    "AND h.student_userid = :u AND h.state = 'CLOSED') AS closed_sessions, "
    "(SELECT count(*) FROM assess_reset_grants g WHERE g.site_id = c.site_id AND g.cmid = c.cmid "
    "AND g.student_userid = :u) AS reset_grants, "
    "coalesce((SELECT e.extra_seconds FROM assess_extra_time e WHERE e.site_id = c.site_id "
    "AND e.cmid = c.cmid AND e.student_userid = :u), 0) AS extra_s "
    "FROM assess_activity_config c WHERE c.site_id = :s AND c.cmid = :c"
)
_WITHIN_T = text(
    # committed_at and the deadline are stamped here, on the wall clock at the end of the
    # transaction: now() is the transaction start, so stamping them earlier would charge the
    # student for the time the commit transaction itself took (CodeRabbit, PR #153)
    # (one clock reading for all three: clock_timestamp() changes on every call)
    "UPDATE assess_sessions SET last_activity_at = c.t, committed_at = c.t, "
    # the one deadline formula (vektra_assess.timing.DEADLINE_SQL) with committed_at = c.t
    "deadline_at = c.t + make_interval(secs => duration_s + credited_outage_s) "
    "FROM (SELECT clock_timestamp() AS t) AS c WHERE id = :sid "
    "AND state = 'ACTIVE' AND c.t - created_at < make_interval(secs => :t)"
)
_REREAD = text("SELECT state, committed_at FROM assess_sessions WHERE id = :sid")
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
    "reply_grace_s = :g, cap_s = :cap, "
    "frozen_policy = CAST(:fp AS jsonb), starting_expires_at = NULL, last_activity_at = now() "
    "WHERE id = :s"
)
#: dated at the pre-flight's admission (database clock, before Moodle was read), never at the
#: commit: the enrolment was observed then, and a slow capture must not make it look fresher
#: than it is
_ENROLLED = text(
    "INSERT INTO assess_enrolment_checks (session_id, result, checked_at) "
    "VALUES (:s, 'enrolled', to_timestamp(:observed))"
)
_HANDOFF = text(
    "INSERT INTO assess_session_handoff (session_id, session_state, epoch, sealed_dek, "
    "handoff_kid) VALUES (:s, 'ACTIVE', :e, :d, :k)"
)


# --- rollback --------------------------------------------------------------------------------


class RollbackLeftovers(RuntimeError):
    """A rollback left a row of the session behind: the transaction is aborted, not committed."""


async def rollback_start(
    conn: AsyncConnection,
    session_id: UUID,
    reason: str,
    *,
    actor: str = "worker",
    extra: AssessAuditEvent | None = None,
    limits: StartLimits | None = None,
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
    # REQ-071 abuse cap and the per-site breaker, in this same transaction (M5.4)
    await after_rollback(
        conn,
        site_id=row.site_id,
        cmid=int(row.cmid),
        userid=int(row.student_userid),
        reason=reason,
        limits=limits or StartLimits(),
    )
    for event in (AssessAuditEvent.START_ROLLBACK, extra):
        if event is None:
            continue
        await write_event(
            conn,
            event,
            actor="worker" if actor == "worker" else "api",
            site_id=row.site_id,
            session_id=session_id,
            reason_code=reason,
            data={"cmid": int(row.cmid), "session_no": int(row.session_no)},
        )
    return True


async def _rollback(
    acquire: AcquireDb,
    session_id: UUID,
    reason: str,
    extra: AssessAuditEvent | None = None,
    limits: StartLimits | None = None,
) -> None:
    async with acquire() as conn:
        async with conn.begin():
            done = await rollback_start(
                conn, session_id, reason, extra=extra, limits=limits
            )
    log.info(
        "assess_preflight_rolled_back",
        session_id=str(session_id),
        reason=reason,
        deleted=done,
    )


# --- gates -----------------------------------------------------------------------------------

#: the capture step's own BR-012 / BR-014 / BR-010 refusals, as START-gate codes
NOT_ELIGIBLE_CODE: dict[NotEligibleReason, Code] = {
    NotEligibleReason.UNREADABLE: Code.UNREADABLE,
    NotEligibleReason.IN_PROGRESS: Code.ATTEMPT_IN_PROGRESS,
    NotEligibleReason.NO_SUBMISSION: Code.NO_SUBMITTED_ATTEMPT,
    NotEligibleReason.NOT_GRADED: Code.NOT_FULLY_GRADED,
    NotEligibleReason.NOT_RELEASED: Code.NOT_RELEASED,
    NotEligibleReason.UNSUPPORTED: Code.UNSUPPORTED_GRADING,
}


def gate_inputs(target: StartTarget, db: DbGateFacts, m: LmsGateFacts) -> GateInputs:
    return GateInputs(
        now=db.now,
        activity_kind="quiz" if target.activity_type == "quiz" else "assign",
        enabled=True,  # checked in the admission transaction (activity_not_configured)
        team_submission=m.team_submission,
        grading_method=m.grading_method,
        attempt_in_progress=m.attempt_in_progress,
        has_submitted_attempt=m.has_submitted_attempt,
        regrade_in_progress=m.regrade_in_progress,
        fully_graded=m.fully_graded,
        released=m.released,
        activity_close_at=m.activity_close_at,
        grace_s=m.grace_s,
        participant_closes=m.participant_closes,
        own_close_at=m.own_close_at,
        teacher_opening_at=db.teacher_opening_at,
        owner_verified=m.owner_verified,
        enrolled=m.enrolled,
        open_session=False,  # this STARTING row is the only open one (partial unique index)
        closed_sessions=db.closed_sessions,
        reset_grants=db.reset_grants,
        privacy_configured=db.privacy_configured,
        end_at=db.end_at,
    )


# --- handler ---------------------------------------------------------------------------------


def _remaining(deps: PreflightDeps, elapsed_at_load: float, t_load: float) -> float:
    elapsed = elapsed_at_load + (deps.clock() - t_load)
    return deps.settings.start_t_s - COMMIT_MARGIN_S - elapsed


async def _admit(
    deps: PreflightDeps, acquire: AcquireDb, session_id: UUID
) -> tuple[StartTarget, int, float, DbGateFacts, int] | None:
    """Steps 1-3. Returns (target, duration_s, elapsed_s, db facts, cap_s) or None for a stale
    job. ``cap_s`` is the activity cap frozen with the session (the transcript header, plan 10)."""
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
                settings.llm_allow_unactivated and settings.is_development
            ):
                raise _Rollback(Reason.MODEL_NOT_ACTIVATED)
            privacy = await deps.privacy(conn, row.site_id) or (
                settings.privacy_allow_unset and settings.is_development
            )
            db = DbGateFacts(
                now=int(act.db_now),
                end_at=act.end_at,
                teacher_opening_at=act.attested_at,
                closed_sessions=int(act.closed_sessions),
                reset_grants=int(act.reset_grants),
                privacy_configured=bool(privacy),
            )
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
    duration = session_duration_s(int(act.cap_minutes), int(act.extra_s))
    return target, duration, elapsed, db, int(act.cap_minutes) * 60


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
    observed_at: int,
    *,
    cap_s: int | None = None,
) -> bool:
    sid = target.session_id
    dek = envelope.new_dek()
    reached_commit = False
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
                        "g": deps.settings.reply_grace_s,
                        "fp": built.frozen_policy.model_dump_json(),
                        "cap": cap_s,
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
                # gate (f) has just verified the enrolment: the first freshness record (M3.7)
                await conn.execute(_ENROLLED, {"s": sid, "observed": observed_at})
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
                # a committed half-open probe closes the breaker
                await after_commit(conn, target.site_id, sid)
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
                # The LAST statement before COMMIT: T on the wall clock (clock_timestamp(), not
                # now(), which is the transaction start), after every write including the audit.
                # The guarded update touches no row past T, and the transaction rolls back. What
                # is left after it is the COMMIT itself (a few milliseconds): that is the only
                # margin by which a commit can land past T.
                guarded = await conn.execute(
                    _WITHIN_T, {"sid": sid, "t": deps.settings.start_t_s}
                )
                if guarded.rowcount != 1:
                    raise _Rollback(Reason.START_TIMEOUT)
                reached_commit = True
    except _Rollback:
        raise
    except Exception as exc:
        if not reached_commit:
            raise  # failed before COMMIT was sent: the transaction is rolled back for sure
        # an error during or after COMMIT: it may or may not have committed
        log.warning("assess_preflight_commit_ambiguous", error_type=type(exc).__name__)
        raise _AmbiguousCommit() from None
    finally:
        del dek
    return True


async def _resolve_ambiguous(
    acquire: AcquireDb, sid: UUID, limits: StartLimits | None = None
) -> None:
    """After an unknown COMMIT outcome, re-read the row on a NEW connection before any rollback:

    - ACTIVE with ``committed_at``: it committed; nothing to undo, no rollback event;
    - still STARTING: it did not; the normal rollback (``commit_failed``);
    - no row: already rolled back (the reaper); nothing to do;
    - the re-read fails too: leave the row to the STARTING reaper (T and no live lease), never
      a blind delete.
    """
    try:
        async with acquire() as conn:
            row = (await conn.execute(_REREAD, {"sid": sid})).first()
    except Exception as exc:
        log.warning(
            "assess_preflight_reread_failed",
            session_id=str(sid),
            error_type=type(exc).__name__,
        )
        return
    if row is None:
        log.info("assess_preflight_ambiguous_gone", session_id=str(sid))
        return
    if row.state == "STARTING":
        await _rollback(acquire, sid, Reason.COMMIT_FAILED, limits=limits)
        return
    log.info("assess_preflight_ambiguous_committed", session_id=str(sid))


def make_preflight_handler(
    deps: PreflightDeps,
) -> Callable[[JobContext], Awaitable[None]]:
    limits = StartLimits.from_settings(deps.settings)

    async def handle(ctx: JobContext) -> None:
        raw = ctx.args.get("session_id")
        try:
            sid = UUID(str(raw))
        except ValueError:
            return  # a malformed job is a stale no-op: nothing to roll back
        try:
            await _run(deps, ctx.acquire, sid)
        except _Rollback as rb:
            await _rollback(ctx.acquire, sid, rb.reason, rb.extra, limits)
        except _AmbiguousCommit:
            await _resolve_ambiguous(ctx.acquire, sid, limits)
        except asyncio.CancelledError:
            # the runtime's budget (T - margin) or a shutdown cancelled the pre-flight mid-way:
            # roll it back now, shielded from the cancellation, instead of leaving it to the reaper
            await asyncio.shield(
                _rollback(ctx.acquire, sid, Reason.START_TIMEOUT, limits=limits)
            )
            raise

    return handle


async def _run(deps: PreflightDeps, acquire: AcquireDb, sid: UUID) -> None:
    t_load = deps.clock()
    admitted = await _admit(deps, acquire, sid)
    if admitted is None:
        return
    target, duration_s, elapsed, db, cap_s = admitted
    # the admission's database time, no later than the enrolment read. db.now is
    # extract(epoch FROM now())::bigint, ROUNDED to the nearest second (up to 0.5 s in the
    # future): one second less keeps the record never newer than the observation, and never
    # newer than a re-check written right after the commit
    observed_at = db.now - 1
    try:
        facts: LmsGateFacts = await _within(
            deps.source.gate_facts(target), _remaining(deps, elapsed, t_load)
        )
        # the gate's "now" is the database's, read at admission, plus the time spent since (the
        # Moodle reads): an end date that passed meanwhile closes the gate
        db = replace(db, now=db.now + int(deps.clock() - t_load))
        decision = evaluate_start_gates(gate_inputs(target, db, facts))
        if not decision.open:
            extra = (
                AssessAuditEvent.TEAM_SUBMISSION_REFUSED
                if decision.code is Code.TEAM_SUBMISSION
                else None
            )
            raise _Rollback(decision.code.value, extra)
        captured: CaptureResult = await _within(
            deps.source.capture(target), _remaining(deps, elapsed, t_load)
        )
        if isinstance(captured, CaptureNotEligible):
            raise _Rollback(NOT_ELIGIBLE_CODE[captured.reason].value)
        if isinstance(captured, Failed):
            raise _Rollback(f"{Reason.CAPTURE_FAILED}_{captured.reason.value}"[:64])
        bundle = assemble_context(captured.snapshot, captured.frozen_policy)
        session_facts = SessionFacts(
            session_number=target.session_no,
            duration_minutes=duration_s // 60,
            previous_context=PREVIOUS_CONTEXT if target.session_no > 1 else None,
        )
        part_a = render_part_a(bundle, session_facts).text
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
        deps,
        acquire,
        target,
        captured,
        part_a,
        part_b,
        duration_s,
        observed_at,
        cap_s=cap_s,
    )
    if committed:
        log.info("assess_preflight_committed", session_id=str(sid))
    else:
        log.info("assess_preflight_stale", session_id=str(sid))
