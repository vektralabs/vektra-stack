"""Teacher operations on an activity's sessions (plan assess-teacher-ops 11b, INT-001).

Every route here takes an ``assess:teacher`` key (exact scope: an ``admin`` key is 403) bound to
its site, names the acting LMS user in the body, and verifies them as a fresh GRADER of the
activity (``api.teachers.verify_teacher``): absent or stale facts refuse with a refresh queued, a
user the LMS does not list refuses, both audited. ``{cmid}``, ``{sid}`` and ``{userid}`` are
resolved server-side inside the key's site and the path's activity: a session of another
activity or site is a 404, whoever asks.

- ``POST /activities/{cmid}/sessions/{sid}/reset`` (REQ-087): the student's LATEST session must
  be CLOSED and not reset yet; the START gates are re-run (the database half live, the LMS half
  from the student's fresh render evaluation); one ``assess_reset_grants`` row per (student,
  activity, session number), the reason encrypted under the transcript KEK. No student notice:
  REQ-092 is out of v1 (its flag is refused at startup).
- ``POST /activities/{cmid}/terminate`` (REQ-081): ONE transaction; the live sessions are locked,
  their count must equal ``expected_active_count`` (409 with the current number, nothing changed);
  each ACTIVE / OUTAGE session goes through the single close path ``assess_close_session(...,
  'terminated_by_teacher')`` (epoch bumped, hand-off deleted, transcript queued); each STARTING
  one is rolled back to ELIGIBLE (``assess_rollback_starting``: nothing consumed); with
  ``also_disable`` the activity is disabled in the same transaction (mint and Start check it).
- ``PUT /activities/{cmid}/students/{userid}/extra-time`` (REQ-078): BR-013 ranges, refused while
  the student's current session is STARTING / ACTIVE / OUTAGE, or CLOSED without a pending reset.
- ``PUT /activities/{cmid}/opening-fallback`` (REQ-091): only while a BR-006(d) input is
  unreadable; on or after the activity close; only later once a session started; attested.

Refusals that must survive (the unverified-teacher audit row, a queued refresh) are raised after
the transaction commits; every other refusal leaves nothing behind. Teacher reasons are never
logged, audited or returned outside the grader list.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from vektra_assess import privacy
from vektra_assess.api.common import (
    engine_of,
    fail,
    is_fresh,
    iso_time,
    parse_body,
    site_of_key,
)
from vektra_assess.api.deps import settings_of
from vektra_assess.api.teachers import (
    RETRY_AFTER_S,
    TeacherGate,
    gate_hint,
    path_id,
    queue_after_refusal,
    queue_teacher_gate,
    verify_teacher,
)
from vektra_assess.api.transcripts import transcript_key_of
from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.capabilities import capability
from vektra_assess.crypto.teacher_reason import encrypt_reason
from vektra_assess.errors import (
    bad_request,
    config_invalid,
    reset_refused,
    retry_details,
    retry_headers,
    session_not_found,
    terminate_count_mismatch,
)
from vektra_assess.gates import Opening
from vektra_assess.teacher import validation as v
from vektra_assess.teacher.schemas import (
    ExtraTimePut,
    FieldError,
    OpeningFallbackPut,
    ResetPost,
    TeacherAction,
    TerminatePost,
    field_errors,
)
from vektra_assess.timing import session_duration_s
from vektra_shared.auth import ApiKeyInfo, require_exact_scope
from vektra_shared.errors import ErrorResponse
from vektra_shared.lms import Readable

log = structlog.get_logger(__name__)
router = APIRouter()

_guard = require_exact_scope("assess:teacher")

#: sessions that hold the activity open for the student (not ELIGIBLE, not CLOSED)
OPEN_STATES = ("STARTING", "ACTIVE", "OUTAGE")
LIVE_STATES = ("ACTIVE", "OUTAGE")  # "N sessioni attive"

_PURGED = text("SELECT 1 FROM assess_purge_records WHERE session_id = :sid")
_SESSION = text(
    "SELECT id, student_userid, session_no, state, close_reason FROM assess_sessions "
    "WHERE id = :sid AND site_id = :s AND cmid = :c"
)
#: every session of one student on one activity, locked: the reset and the extra time decide
#: on the student's current session, and a Start cannot commit meanwhile
_STUDENT_SESSIONS = text(
    "SELECT id, session_no, state, close_reason FROM assess_sessions "
    "WHERE site_id = :s AND cmid = :c AND student_userid = :u ORDER BY session_no FOR UPDATE"
)
_GRANTS = text(
    "SELECT count(*) FROM assess_reset_grants WHERE site_id = :s AND cmid = :c "
    "AND student_userid = :u"
)
_CONFIG = text(
    "SELECT enabled, cap_minutes, version, extract(epoch FROM end_at)::bigint AS end_at, "
    "extract(epoch FROM attested_opening_at)::bigint AS attested_at "
    "FROM assess_activity_config WHERE site_id = :s AND cmid = :c FOR UPDATE"
)
#: the wall clock read AFTER the locks: ``now()`` is the transaction start, and a reset may have
#: waited for the activity lock past the end date
_CLOCK = text("SELECT extract(epoch FROM clock_timestamp())::bigint AS db_now")
_RENDER = text(
    "SELECT inputs, extract(epoch FROM clock_timestamp() - refreshed_at) AS age_s "
    "FROM assess_gate_inputs "
    "WHERE site_id = :s AND student_userid = :u AND cmid = :c"
)
_INSERT_GRANT = text(
    "INSERT INTO assess_reset_grants (id, site_id, cmid, student_userid, granted_by, session_no, "
    "reason_ct, reason_kid, include_previous_turns) VALUES (:id, :s, :c, :u, :by, :n, :ct, :kid, "
    ":prev) ON CONFLICT ON CONSTRAINT uq_assess_reset_grants_session DO NOTHING RETURNING id"
)
_LIVE = text(
    "SELECT id, state, epoch FROM assess_sessions WHERE site_id = :s AND cmid = :c "
    "AND state IN ('STARTING', 'ACTIVE', 'OUTAGE') ORDER BY id FOR UPDATE"
)
_CLOSE = text("SELECT assess_close_session(:sid, :epoch, 'terminated_by_teacher')")
_ROLLBACK = text("SELECT assess_rollback_starting(:sid)")
_DISABLE = text(
    "UPDATE assess_activity_config SET enabled = false, version = version + 1, "
    "updated_by = :u, updated_at = now() WHERE site_id = :s AND cmid = :c AND enabled "
    "RETURNING version"
)
_INSERT_TERMINATION = text(
    "INSERT INTO assess_terminations (id, site_id, cmid, reason_ct, reason_kid, closed_count, "
    "rolled_back_count, also_disable, created_by) VALUES (:id, :s, :c, :ct, :kid, :closed, "
    ":rolled, :dis, :by)"
)
_EXTRA = text(
    "SELECT extra_seconds FROM assess_extra_time WHERE site_id = :s AND cmid = :c "
    "AND student_userid = :u"
)
_UPSERT_EXTRA = text(
    "INSERT INTO assess_extra_time (site_id, cmid, student_userid, extra_seconds, granted_by, "
    "created_at) VALUES (:s, :c, :u, :x, :by, now()) ON CONFLICT (site_id, cmid, student_userid) "
    "DO UPDATE SET extra_seconds = :x, granted_by = :by, created_at = now()"
)
_STARTED = text(
    "SELECT EXISTS (SELECT 1 FROM assess_sessions WHERE site_id = :s AND cmid = :c "
    "AND committed_at IS NOT NULL)"
)
_ATTEST = text(
    "UPDATE assess_activity_config SET attested_opening_at = :at, attested_by = :by, "
    "version = version + 1, updated_by = :u, updated_at = now() "
    "WHERE site_id = :s AND cmid = :c RETURNING version"
)


class _Refused(Exception):
    """A refusal decided inside the transaction, raised once it has committed.

    ``refresh``: the refusal was decided on cached LMS facts; ``_run`` queues ``refresh(conn)``
    AFTER the commit, in its own transaction (``queue_after_refusal``), and raises ``exc`` (with
    the retry hint) when that worked, ``unhinted`` (the same refusal, no hint) when it did not."""

    def __init__(
        self,
        exc: HTTPException,
        *,
        refresh: Callable[[Any], Awaitable[Any]] | None = None,
        unhinted: HTTPException | None = None,
    ) -> None:
        super().__init__("refused")
        self.exc = exc
        self.refresh = refresh
        self.unhinted = unhinted


def _render_refusal(
    hinted: ErrorResponse,
    unhinted: ErrorResponse,
    site: UUID,
    student: int,
    cmid: int,
    courseid: int | None,
) -> _Refused:
    """A reset refused on the student's cached LMS half: the render check (and its single
    follow-up beside a pending one) is queued after the refusal; the hint iff it was."""

    async def refresh(conn: Any) -> None:
        from vektra_assess.api.state import queue_render_check

        await queue_render_check(conn, site, student, cmid, courseid, follow_up=True)

    return _Refused(
        fail(hinted, retry_headers(hinted)), refresh=refresh, unhinted=fail(unhinted)
    )


def _uuid(raw: str, name: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError:
        raise fail(bad_request(name)) from None


def _invalid(
    errors: list[FieldError], retry_after_s: int | None = None
) -> HTTPException:
    """The 400 of field errors; ``retry_after_s`` (and ``Retry-After``) when a refresh of the
    cached LMS facts they were decided on was queued."""
    unique: list[FieldError] = []
    for e in errors:
        if e not in unique:
            unique.append(e)
    err = config_invalid(field_errors(unique), retry_after_s=retry_after_s)
    return fail(err, retry_headers(err))


async def _run(
    request: Request,
    key: ApiKeyInfo,
    cmid: int,
    body: TeacherAction,
    route: str,
    action: Any,
) -> JSONResponse:
    """The common frame: site of the key, fresh grader check (audited refusal committed), the
    action in the same transaction, and any refusal raised after the commit."""
    settings = settings_of(request)
    refused: HTTPException | None = None
    deferred: _Refused | None = None
    async with engine_of(request).begin() as conn:
        site = await site_of_key(conn, key, "teacher")
        gate, refused = await verify_teacher(
            conn, site=site, cmid=cmid, actor=body.actor_userid, courseid=body.courseid,
            kind=body.activity_type, max_age_s=settings.teacher_gate_max_age_s,
            route=route, role="grader",
        )  # fmt: skip
        if refused is None:
            try:
                status, view = await action(conn, site, gate)
            except _Refused as r:
                refused, deferred = r.exc, r
    if deferred is not None and deferred.refresh is not None:
        queued = await queue_after_refusal(engine_of(request), deferred.refresh)
        if not queued and deferred.unhinted is not None:
            raise deferred.unhinted
    if refused is not None:
        raise refused
    return JSONResponse(view, status_code=status, headers={"Cache-Control": "no-store"})


# --- reset (REQ-087) ----------------------------------------------------------------------------


@router.post("/activities/{cmid}/sessions/{sid}/reset")
async def reset_session(
    cmid: str, sid: str, request: Request, key: ApiKeyInfo = Depends(_guard)
) -> JSONResponse:
    c = path_id(cmid, "cmid")
    session_id = _uuid(sid, "sid")
    body = await parse_body(request, ResetPost)
    reason, errors = v.check_reason(body.reason)
    if errors or reason is None:
        raise _invalid(errors)
    kek = transcript_key_of(request)
    settings = settings_of(request)

    async def action(conn: Any, site: UUID, gate: TeacherGate) -> tuple[int, Any]:
        row = (
            await conn.execute(_SESSION, {"sid": session_id, "s": site, "c": c})
        ).first()
        if row is None:
            raise _Refused(fail(session_not_found()))
        student = int(row.student_userid)
        params = {"s": site, "c": c, "u": student}
        # lock order of every teacher operation and of Start: the activity row, then sessions
        cfg = (await conn.execute(_CONFIG, {"s": site, "c": c})).first()
        sessions = (await conn.execute(_STUDENT_SESSIONS, params)).all()
        if not any(s.id == row.id for s in sessions):
            # rolled back (a terminate's STARTING rollback) while this reset waited for the
            # activity lock: the session is gone
            raise _Refused(fail(session_not_found()))
        if any(s.state in OPEN_STATES for s in sessions):
            raise _Refused(fail(reset_refused("not_closed")))
        latest = sessions[-1]
        if latest.id != row.id:
            raise _Refused(fail(reset_refused("not_latest")))
        # a session the retention purge emptied (plan 12): its content is gone, and a reset
        # reason written now would outlive it unscrubbed
        if (await conn.execute(_PURGED, {"sid": row.id})).scalar() is not None:
            raise _Refused(fail(reset_refused("purged")))
        grants = int((await conn.execute(_GRANTS, params)).scalar_one())
        if grants >= len(sessions):
            raise _Refused(fail(reset_refused("already_reset")))
        if cfg is None:
            raise _Refused(fail(reset_refused("gate", gate="not_enabled")))
        allowed_unset = settings.privacy_allow_unset and settings.is_development
        privacy_ok = bool(await privacy.is_configured(conn, site)) or allowed_unset
        db_now = int((await conn.execute(_CLOCK)).scalar_one())

        def gate_code(lms_reasons: list[str]) -> str | None:
            return v.reset_gate(
                now=db_now,
                enabled=bool(cfg.enabled),
                cap_set=cfg.cap_minutes is not None,
                end_at=cfg.end_at,
                privacy_configured=privacy_ok,
                lms_reasons=lms_reasons,
            )

        def refuse_gate(code: str) -> _Refused:
            extra = {"field": "available_until"} if code == "end_date_passed" else {}
            return _Refused(fail(reset_refused("gate", gate=code, **extra)))

        # the database half first, live: a disabled activity is never rendered with its LMS
        # half (the render short-circuits), so waiting for one would never end (review 11b-1)
        code = gate_code([])
        if code is not None:
            raise refuse_gate(code)
        render = (await conn.execute(_RENDER, params)).first()
        inputs = render.inputs if render is not None else None
        fresh = (
            render is not None
            and is_fresh(render.age_s, settings.teacher_gate_max_age_s)
            and isinstance(inputs, dict)
            and isinstance(inputs.get("lms_reasons"), list)
            # evaluated with the REQ-091 date in force now (a moved date changes gate (d))
            and inputs.get("attested_at") == cfg.attested_at
        )
        if not fresh:
            # the LMS half is evaluated by the worker: ask for it and let the teacher retry
            raise _render_refusal(
                reset_refused("gate_inputs_stale", **retry_details(RETRY_AFTER_S)),
                reset_refused("gate_inputs_stale"),
                site, student, c, body.courseid,
            )  # fmt: skip
        code = gate_code([str(x) for x in inputs["lms_reasons"]])  # type: ignore[index]
        if code is not None:
            # the database half passed above: this refusal is the CACHED LMS half, which the
            # teacher may have just changed in the LMS. Re-evaluate it and hint (the client
            # retries at most once)
            raise _render_refusal(
                reset_refused("gate", gate=code, **retry_details(RETRY_AFTER_S)),
                reset_refused("gate", gate=code),
                site, student, c, body.courseid,
            )  # fmt: skip
        # the LOCKED row's close reason: the worker may have closed the session between the
        # unlocked read and the lock
        include = v.include_previous_turns(
            latest.close_reason, body.include_previous_turns
        )
        grant_id = uuid.uuid4()
        ct, kid = encrypt_reason(kek, "assess_reset_grants", grant_id, reason)
        done = (
            await conn.execute(
                _INSERT_GRANT,
                {
                    **params,
                    "id": grant_id,
                    "by": str(body.actor_userid),
                    "n": int(row.session_no),
                    "ct": ct,
                    "kid": kid,
                    "prev": include,
                },
            )
        ).first()
        if done is None:  # a concurrent reset of the same session won
            raise _Refused(fail(reset_refused("already_reset")))
        # the results page should show state (10) without waiting for the next refresh tick
        from vektra_assess.api.state import queue_render_check

        # follow_up: a check already running may have read before this grant
        await queue_render_check(conn, site, student, c, body.courseid, follow_up=True)
        await write_event(
            conn,
            AssessAuditEvent.TEACHER_RESET,
            actor="api",
            site_id=site,
            session_id=session_id,
            reason_code="reset",
            data={
                "cmid": c,
                "actor": body.actor_userid,
                "student": student,
                "reset_id": str(grant_id),
                "session_no": int(row.session_no),
                "include_previous_turns": include,
            },
        )
        log.info(
            "assess_teacher_reset",
            site_id=str(site),
            cmid=c,
            reset_id=str(grant_id),
        )
        return 201, {
            "reset_id": str(grant_id),
            "student_userid": student,
            "session_no_next": int(row.session_no) + 1,
            "include_previous_turns": include,
        }

    return await _run(request, key, c, body, "reset", action)


# --- terminate (REQ-081) ------------------------------------------------------------------------


@router.post("/activities/{cmid}/terminate")
async def terminate_sessions(
    cmid: str, request: Request, key: ApiKeyInfo = Depends(_guard)
) -> JSONResponse:
    c = path_id(cmid, "cmid")
    body = await parse_body(request, TerminatePost)
    reason, errors = v.check_reason(body.reason)
    if errors or reason is None:
        raise _invalid(errors)
    kek = transcript_key_of(request)

    async def action(conn: Any, site: UUID, gate: TeacherGate) -> tuple[int, Any]:
        # the activity row first: a Start holds it FOR SHARE until its STARTING row commits, so
        # an in-flight Start is either counted below (and rolled back) or sees the disable
        await conn.execute(_CONFIG, {"s": site, "c": c})
        live = (await conn.execute(_LIVE, {"s": site, "c": c})).all()
        active = [r for r in live if r.state in LIVE_STATES]
        if len(active) != body.expected_active_count:
            raise _Refused(fail(terminate_count_mismatch(len(active))))
        closed = 0
        for r in active:
            ok = (await conn.execute(_CLOSE, {"sid": r.id, "epoch": r.epoch})).scalar()
            if (
                ok is not True
            ):  # locked above: cannot happen; never report a close not done
                raise RuntimeError("a locked live session refused the close")
            closed += 1
        rolled = 0
        for r in live:
            if r.state == "STARTING":
                rolled += bool((await conn.execute(_ROLLBACK, {"sid": r.id})).scalar())
        # ``version`` is set only when this request actually switched the activity off: an
        # activity already disabled (or never configured) is not rewritten, re-versioned or
        # re-audited, and the termination records what happened, not what was asked
        version = None
        if body.also_disable:
            version = (
                await conn.execute(
                    _DISABLE, {"s": site, "c": c, "u": body.actor_userid}
                )
            ).scalar()
        term_id = uuid.uuid4()
        ct, kid = encrypt_reason(kek, "assess_terminations", term_id, reason)
        await conn.execute(
            _INSERT_TERMINATION,
            {
                "id": term_id,
                "s": site,
                "c": c,
                "ct": ct,
                "kid": kid,
                "closed": closed,
                "rolled": rolled,
                "dis": version is not None,
                "by": str(body.actor_userid),
            },
        )
        if version is not None:
            # the same audit row a config PUT writes for this field (REQ-085 old/new)
            await write_event(
                conn,
                AssessAuditEvent.TEACHER_CONFIG_CHANGED,
                actor="api",
                site_id=site,
                reason_code="enabled",
                data={
                    "field": "enabled",
                    "old": True,
                    "new": False,
                    "cmid": c,
                    "actor": body.actor_userid,
                    "version": int(version),
                    "termination_id": str(term_id),
                },
            )
        await write_event(
            conn,
            AssessAuditEvent.TEACHER_TERMINATE,
            actor="api",
            site_id=site,
            reason_code="terminate",
            data={
                "cmid": c,
                "actor": body.actor_userid,
                "termination_id": str(term_id),
                "closed": closed,
                "rolled_back": rolled,
                "also_disable": version is not None,
            },
        )
        log.info(
            "assess_teacher_terminate",
            site_id=str(site),
            cmid=c,
            termination_id=str(term_id),
            closed=closed,
            rolled_back=rolled,
        )
        return 200, {
            "termination_id": str(term_id),
            "closed": closed,
            "rolled_back": rolled,
            "disabled": version is not None,
        }

    return await _run(request, key, c, body, "terminate", action)


# --- extra time (REQ-078) -----------------------------------------------------------------------


@router.put("/activities/{cmid}/students/{userid}/extra-time")
async def put_extra_time(
    cmid: str, userid: str, request: Request, key: ApiKeyInfo = Depends(_guard)
) -> JSONResponse:
    c = path_id(cmid, "cmid")
    student = path_id(userid, "userid")
    body = await parse_body(request, ExtraTimePut)

    async def action(conn: Any, site: UUID, gate: TeacherGate) -> tuple[int, Any]:
        params = {"s": site, "c": c, "u": student}
        # the activity row first (FOR UPDATE): a Start holds it FOR SHARE until its STARTING
        # row commits, so a Start in flight is either seen below as STARTING (refused) or reads
        # the new extra time at admission; then the sessions (the lock order of every teacher
        # operation: no deadlock with a reset or a terminate)
        cfg = (await conn.execute(_CONFIG, {"s": site, "c": c})).first()
        sessions = (await conn.execute(_STUDENT_SESSIONS, params)).all()
        grants = int((await conn.execute(_GRANTS, params)).scalar_one())
        if cfg is None or cfg.cap_minutes is None:
            raise _Refused(_invalid([FieldError("extra_time", "cap_not_configured")]))
        cap = int(cfg.cap_minutes)
        errors = v.check_extra_time(
            cap,
            minutes=body.minutes,
            percent=body.percent,
            confirm_over_100=body.confirm_over_100,
        )
        if errors:
            raise _Refused(_invalid(errors))
        current = sessions[-1].state if sessions else None
        if current in OPEN_STATES or (current == "CLOSED" and grants < len(sessions)):
            # REQ-078: copied into the session at Start; a started (or used-up) session keeps
            # what it had
            raise _Refused(
                _invalid(
                    [
                        FieldError(
                            "extra_time", "session_locked", extra={"state": current}
                        )
                    ]
                )
            )
        old = (await conn.execute(_EXTRA, params)).scalar()
        new = v.extra_seconds(cap, minutes=body.minutes, percent=body.percent)
        await conn.execute(
            _UPSERT_EXTRA, {**params, "x": new, "by": str(body.actor_userid)}
        )
        await write_event(
            conn,
            AssessAuditEvent.TEACHER_EXTRA_TIME,
            actor="api",
            site_id=site,
            reason_code="extra_time",
            data={
                "cmid": c,
                "actor": body.actor_userid,
                "student": student,
                "old_s": None if old is None else int(old),
                "new_s": new,
            },
        )
        return 200, {
            "student_userid": student,
            "extra_seconds": new,
            "duration_s": session_duration_s(cap, new),
        }

    return await _run(request, key, c, body, "extra_time", action)


# --- opening fallback (REQ-091) -----------------------------------------------------------------


async def readable_close_flags(conn: Any, site: UUID) -> dict[str, bool]:
    """The capability-matrix answer for the BR-006(d) inputs of a quiz (fresh ``readable`` on
    the current release; partial, stale or absent is False)."""
    return {
        flag: isinstance(await capability(conn, site, flag), Readable)
        for flag in v.QUIZ_CLOSE_FLAGS
    }


async def opening_for(
    conn: Any, site: UUID, facts: Mapping[str, Any], attested: int | None
) -> Opening:
    """The activity's opening decision for every teacher route (``v.opening_of``: the Start
    gate's rule on the teacher facts merged with the capability matrix)."""
    return v.opening_of(facts, attested, await readable_close_flags(conn, site))


@router.put("/activities/{cmid}/opening-fallback")
async def put_opening_fallback(
    cmid: str, request: Request, key: ApiKeyInfo = Depends(_guard)
) -> JSONResponse:
    c = path_id(cmid, "cmid")
    body = await parse_body(request, OpeningFallbackPut)

    async def action(conn: Any, site: UUID, gate: TeacherGate) -> tuple[int, Any]:
        cfg = (await conn.execute(_CONFIG, {"s": site, "c": c})).first()
        if cfg is None:
            raise _Refused(
                _invalid([FieldError("opening_at", "activity_not_configured")])
            )
        # the same decision the views show: accepted exactly while a date opens the activity
        opening = await opening_for(conn, site, gate.facts, cfg.attested_at)
        started = bool((await conn.execute(_STARTED, {"s": site, "c": c})).scalar())
        errors = v.check_opening_fallback(
            body.opening_at,
            attestation=body.attestation,
            opening=opening,
            current_at=cfg.attested_at,
            started=started,
            end_at=cfg.end_at,
        )
        if errors:
            hint = gate_hint(gate)
            if hint is not None and all(e.code in v.OPENING_FACT_CODES for e in errors):
                # decided on the CACHED gate facts (the close date, its readability, the
                # automatic close): the teacher may have just changed them in the LMS. Re-read
                # after the refusal and hint iff it was queued (the client retries at most once)
                course, kind = hint
                raise _Refused(
                    _invalid(errors, RETRY_AFTER_S),
                    refresh=lambda q: queue_teacher_gate(
                        q, site, c, course, kind, follow_up=True
                    ),
                    unhinted=_invalid(errors),
                )
            raise _Refused(_invalid(errors))
        at = int(body.opening_at.timestamp())
        version = (
            await conn.execute(
                _ATTEST,
                {
                    "s": site,
                    "c": c,
                    "at": datetime.fromtimestamp(at, UTC),
                    "by": str(body.actor_userid),
                    "u": body.actor_userid,
                },
            )
        ).scalar()
        await write_event(
            conn,
            AssessAuditEvent.TEACHER_OPENING_ATTESTED,
            actor="api",
            site_id=site,
            reason_code="opening_fallback",
            data={
                "cmid": c,
                "actor": body.actor_userid,
                "old": cfg.attested_at,
                "new": at,
                "attestation_text_version": body.attestation_text_version,
                "version": int(version),
            },
        )
        return 200, {
            "opening_at": iso_time(at),
            "previous": iso_time(cfg.attested_at),
            "version": int(version),
        }

    return await _run(request, key, c, body, "opening_fallback", action)
