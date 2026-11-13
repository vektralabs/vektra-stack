"""``POST /api/v1/assess/sessions``: Start, asynchronous (ARCH-068, REQ-071).

The pre-session token is the only input. First, cheap ADVISORY refusals that write no session,
token or job row (each audited ``assess_start_refused`` with its reason code):

- the worker is not ready (heartbeat missing, stale or not ``ready``): 503;
- the activity is not enabled or has no cap (BR-013): 409 ``activity_not_configured``;
- admission: ``VEKTRA_ASSESS_ADMISSION_K`` or more non-closed sessions: 503 (NFR-020);
- a FRESH, definitive negative worker-computed render state (``GET /state``; the ``_REFUSING``
  codes): 409 with that gate's code. Stale, missing, unreadable or unknown: the pre-flight decides.

Then ONE transaction:

1. consume the jti: only a row that was minted for exactly this (site, sub, cmid), by a key that is
   still the site's mint key, unexpired and unconsumed (mint-to-Start correlation, ARCH-067);
2. refuse when the student already had a session for the activity and no reset grant covers a new
   one (REQ-087: session #N+1 exists only through a reset); refuse with 409 ``ERR-ASSESS-029``
   ``privacy_notice_changed`` (audited) when the token carries the notice version presented at
   mint and the site's current one differs (plugin request P3);
3. INSERT the STARTING row; the partial unique index on (site, sub, cmid) WHERE state <> 'CLOSED'
   makes a second concurrent Start fail here, which rolls the whole transaction back, so its jti
   is not consumed either;
4. INSERT the ``requested`` start attempt, ``enqueue_job('preflight')`` (INSERT + NOTIFY) and the
   ``assess_start`` audit event with the privacy-notice version.

202 returns the session id and a session-bound token. The worker pre-flight is authoritative: it
re-validates everything and commits or rolls back. Nothing on this path reads the LMS or content.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from vektra_assess import privacy
from vektra_assess.api.common import engine_of, fail
from vektra_assess.api.deps import settings_of, tokens_of
from vektra_assess.api.gate import verify_assess_token_pre
from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.errors import (
    assess_unavailable,
    capacity_reached,
    privacy_notice_changed,
    rate_limited,
    start_breaker_open,
    start_refused,
    token_invalid,
)
from vektra_assess.gates import Code
from vektra_assess.jobs import JobKind, enqueue_job
from vektra_assess.tokens import PreClaims
from vektra_shared.errors import ErrorResponse

router = APIRouter()

_HEARTBEAT = text(
    "SELECT extract(epoch FROM now() - beat_at), details->>'state' FROM assess_heartbeat "
    "WHERE component = 'worker'"
)
_ACTIVITY = text(
    "SELECT enabled, cap_minutes FROM assess_activity_config WHERE site_id = :s AND cmid = :c"
)
#: the authoritative re-read of the activity inside the Start transaction, under a SHARE lock
#: held until the STARTING row commits: a teacher's terminate / disable / extra time (which lock
#: the row FOR UPDATE first) either sees this Start's row or runs before it and is seen by it
#: (review 11b-3, 11b-5)
ACTIVITY_LOCK = text(
    "SELECT enabled, cap_minutes FROM assess_activity_config WHERE site_id = :s AND cmid = :c "
    "FOR SHARE"
)
_BREAKER = text(
    "SELECT b.state, b.half_open_at <= now() AS half_open, "
    "extract(epoch FROM b.half_open_at - now()) AS wait_s, (SELECT count(*) FROM assess_sessions "
    "s WHERE s.site_id = b.site_id AND s.state = 'STARTING') AS starting "
    "FROM assess_start_breaker b WHERE b.site_id = :s"
)
_BREAKER_LOCK = text(
    "SELECT pg_advisory_xact_lock(hashtextextended('assess_start_breaker:' || "
    "CAST(:s AS text), 0)) FROM assess_start_breaker WHERE site_id = CAST(:s AS uuid) "
    "AND state = 'open'"
)
_STUDENT_LOCK = text(
    "SELECT pg_advisory_xact_lock(hashtextextended('assess_start:' || :s || ':' || :u || ':' "
    "|| :c, 0))"
)
_START_CAP = text(
    "SELECT count(*) AS n, extract(epoch FROM min(created_at) + interval '1 hour' - now()) "
    "AS wait_s FROM assess_start_attempts WHERE site_id = :s AND student_userid = :u "
    "AND cmid = :c AND outcome = 'requested' AND created_at > now() - interval '1 hour'"
)
#: the deployment-wide admission cap counts every site's live sessions: a definer function,
#: because the API role's own read of assess_sessions is bound to this site (ADR-0033 D9.2)
_LIVE = text("SELECT assess_live_session_count()")
_RENDER = text(
    "SELECT status, reason_code, extract(epoch FROM now() - computed_at) AS age_s "
    "FROM assess_render_state WHERE site_id = :s AND student_userid = :u AND cmid = :c"
)
#: the render codes that refuse a Start before anything is written: definitive negative states
#: of a LMS-side or teacher-set gate. Everything else (``unreadable``, ``close_unreadable``, a
#: NULL code, a code this release does not know, and the codes the Start transaction re-checks on
#: its own rows) goes on to the pre-flight, which re-reads and decides (review fix 8).
_REFUSING = frozenset(
    c.value
    for c in (
        Code.TEAM_SUBMISSION,
        Code.MARKING_GUIDE,
        Code.UNSUPPORTED_GRADING,
        Code.ATTEMPT_IN_PROGRESS,
        Code.NO_SUBMITTED_ATTEMPT,
        Code.REGRADE_IN_PROGRESS,
        Code.NOT_FULLY_GRADED,
        Code.NOT_RELEASED,
        Code.NO_CLOSE_DATE,
        Code.CLOSE_NOT_PASSED,
        Code.NOT_ENROLLED,
        Code.END_DATE_PASSED,
    )
)
CONSUME_JTI = text(
    "UPDATE assess_token_jti t SET consumed_at = now() "
    "WHERE t.jti = :jti AND t.consumed_at IS NULL AND t.expires_at > now() "
    "AND t.site_id = :site AND t.student_userid = :sub AND t.cmid = :cmid "
    "AND EXISTS (SELECT 1 FROM assess_site_keys k WHERE k.api_key_id = t.mint_key_id "
    "AND k.site_id = t.site_id AND k.purpose = 'mint') "
    "RETURNING t.courseid_hint"
)
_HISTORY = text(
    "SELECT count(*) FILTER (WHERE state <> 'CLOSED') AS open, "
    "count(*) FILTER (WHERE state = 'CLOSED') AS closed, "
    "(SELECT count(*) FROM assess_reset_grants WHERE site_id = :s "
    "AND student_userid = :u AND cmid = :c) AS grants "
    "FROM assess_sessions WHERE site_id = :s AND student_userid = :u AND cmid = :c"
)
_INSERT_SESSION = text(
    "INSERT INTO assess_sessions (site_id, cmid, student_userid, state, starting_expires_at, "
    "notice_version, courseid_hint) VALUES (:s, :c, :u, 'STARTING', "
    "now() + make_interval(secs => :t), :nv, :course) RETURNING id, session_no, epoch"
)
_ATTEMPT = text(
    "INSERT INTO assess_start_attempts (site_id, cmid, student_userid, outcome, session_id) "
    "VALUES (:s, :c, :u, 'requested', :sid)"
)
_ONE_OPEN = "uq_assess_sessions_one_open"


class _Refused(Exception):
    """Raised inside the Start transaction so that it rolls back (the jti stays unconsumed)."""

    def __init__(
        self,
        err: ErrorResponse,
        audit_reason: str | None = None,
        event: AssessAuditEvent = AssessAuditEvent.START_REFUSED,
        audit_data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(err.code)
        self.err = err
        self.event = event
        self.audit_data = audit_data or {}
        self.audit_reason = (
            audit_reason  # audited after the rollback, in its own transaction
        )


async def _refuse(
    request: Request,
    claims: PreClaims,
    reason: str,
    err: ErrorResponse,
    event: AssessAuditEvent = AssessAuditEvent.START_REFUSED,
) -> None:
    """Audit an advisory refusal (its own transaction: nothing else was written) and raise."""
    async with engine_of(request).begin() as conn:
        await write_event(
            conn,
            event,
            actor="api",
            site_id=claims.site,
            reason_code=reason,
            data={"cmid": claims.cmid},
        )
    raise fail(err)


async def _advisory(request: Request, conn: AsyncConnection, claims: PreClaims) -> None:
    settings = settings_of(request)
    beat = (await conn.execute(_HEARTBEAT)).first()
    if (
        beat is None
        or beat[0] is None
        or float(beat[0]) > settings.heartbeat_max_age_s
        or beat[1] != "ready"  # a missing state is not readiness (fail closed)
    ):
        await _refuse(
            request, claims, "worker_not_ready", assess_unavailable("worker_heartbeat")
        )
    # the per-site Start breaker (M5.4): open -> refused fast, nothing consumed; half-open after
    # the cooldown -> one Start at a time goes through to the pre-flight
    breaker = (await conn.execute(_BREAKER, {"s": claims.site})).first()
    if breaker is not None and breaker.state == "open":
        if not breaker.half_open or int(breaker.starting):
            await _refuse(
                request,
                claims,
                "start_breaker_open",
                start_breaker_open(int(float(breaker.wait_s or 0))),
            )
    # REQ-071 abuse cap: Start attempts per (student, activity) per hour
    cap = (
        await conn.execute(
            _START_CAP, {"s": claims.site, "u": claims.sub, "c": claims.cmid}
        )
    ).one()
    if int(cap.n) >= settings.start_rate_per_hour:
        wait_s = max(int(float(cap.wait_s or 0)), 1)
        await _refuse(
            request,
            claims,
            "start_rate_cap",
            rate_limited(wait_s, "start_rate_cap"),
            AssessAuditEvent.START_RATE_CAP_HIT,
        )
    activity = (
        await conn.execute(_ACTIVITY, {"s": claims.site, "c": claims.cmid})
    ).first()
    if activity is None or not activity.enabled or activity.cap_minutes is None:
        await _refuse(
            request,
            claims,
            "activity_not_configured",
            start_refused("activity_not_configured"),
        )
    live = int((await conn.execute(_LIVE)).scalar_one())
    if live >= settings.admission_k:
        await _refuse(request, claims, "capacity", capacity_reached())
    render = (
        await conn.execute(
            _RENDER, {"s": claims.site, "u": claims.sub, "c": claims.cmid}
        )
    ).first()
    if (
        render is not None
        and float(render.age_s) <= settings.render_fresh_s
        and render.reason_code in _REFUSING
    ):
        # a FRESH negative render state of a LMS-side gate: refused before anything is
        # written; stale or missing goes on to the pre-flight, which re-reads (authoritative)
        await _refuse(
            request,
            claims,
            str(render.reason_code),
            start_refused(str(render.reason_code)),
        )


@router.post("/sessions", status_code=202)
async def start_session(
    request: Request, claims: PreClaims = Depends(verify_assess_token_pre)
) -> JSONResponse:
    settings = settings_of(request)
    engine = engine_of(request)
    async with engine.connect() as conn:
        await _advisory(request, conn, claims)
    refused: ErrorResponse | None = None
    row: Any = None
    try:
        async with engine.begin() as conn:
            consumed = (
                await conn.execute(
                    CONSUME_JTI,
                    {
                        "jti": claims.jti,
                        "site": claims.site,
                        "sub": claims.sub,
                        "cmid": claims.cmid,
                    },
                )
            ).first()
            # per-site breaker, half-open (review fix 8): "one Start at a time" decided under a
            # per-site transaction lock, held until this Start's STARTING row is committed, so
            # two concurrent Starts cannot both see no probe in flight
            # (two statements: the count must be read AFTER the lock, in a new snapshot)
            await conn.execute(_BREAKER_LOCK, {"s": str(claims.site)})
            breaker = (await conn.execute(_BREAKER, {"s": claims.site})).first()
            if breaker is not None and breaker.state == "open":
                if not breaker.half_open or int(breaker.starting):
                    raise _Refused(
                        start_breaker_open(int(float(breaker.wait_s or 0))),
                        "start_breaker_open",
                    )
            # REQ-071 Start cap, authoritative: under a lock per
            # (site, student, activity) held until this Start commits, the attempts are counted
            # again in a new statement, so concurrent Starts that passed the advisory check
            # cannot add a sixth attempt between them
            params_cap = {"s": claims.site, "u": claims.sub, "c": claims.cmid}
            await conn.execute(
                _STUDENT_LOCK, {k: str(v) for k, v in params_cap.items()}
            )
            cap = (await conn.execute(_START_CAP, params_cap)).one()
            if int(cap.n) >= settings.start_rate_per_hour:
                raise _Refused(
                    rate_limited(max(int(float(cap.wait_s or 0)), 1), "start_rate_cap"),
                    "start_rate_cap",
                    AssessAuditEvent.START_RATE_CAP_HIT,
                )
            if consumed is None:
                # mint-to-Start correlation (M5.5): a correctly signed token whose jti was never
                # minted through /tokens for this (site, sub, cmid) by the site's CURRENT mint
                # key, or was already used or expired
                raise _Refused(token_invalid(), "mint_correlation")
            activity = (
                await conn.execute(ACTIVITY_LOCK, {"s": claims.site, "c": claims.cmid})
            ).first()
            if activity is None or not activity.enabled or activity.cap_minutes is None:
                raise _Refused(start_refused("activity_not_configured"))
            hist = (
                await conn.execute(
                    _HISTORY, {"s": claims.site, "u": claims.sub, "c": claims.cmid}
                )
            ).one()
            if int(hist.open):
                raise _Refused(start_refused("session_already_open"))
            if int(hist.closed) > int(hist.grants):
                raise _Refused(start_refused("already_discussed"))
            # REQ-089: the notice the student saw is the site's current one; the deployment
            # value stands in only for a site with no settings (development override)
            notice_version = await privacy.shown_notice_version(
                conn, claims.site, settings.privacy_notice_version
            )
            # plugin request P3: a token minted with the version the student was shown is
            # refused when the notice changed since (rolled back: nothing consumed, audited)
            presented = claims.notice_version
            if presented is not None and presented != notice_version:
                raise _Refused(
                    privacy_notice_changed(),
                    "privacy_notice_changed",
                    audit_data={
                        "presented_notice_version": presented,
                        "current_notice_version": notice_version,
                    },
                )
            row = (
                await conn.execute(
                    _INSERT_SESSION,
                    {
                        "s": claims.site,
                        "c": claims.cmid,
                        "u": claims.sub,
                        "t": settings.start_t_s,
                        "nv": notice_version,
                        "course": consumed.courseid_hint,
                    },
                )
            ).one()
            sid: UUID = row.id
            params = {"s": claims.site, "c": claims.cmid, "u": claims.sub}
            await conn.execute(_ATTEMPT, {**params, "sid": sid})
            await enqueue_job(
                conn,
                JobKind.PREFLIGHT,
                {"session_id": str(sid)},
                dedupe_key=f"preflight:{sid}",
                max_attempts=2,
            )
            await write_event(
                conn,
                AssessAuditEvent.START,
                actor="api",
                site_id=claims.site,
                session_id=sid,
                data={
                    "cmid": claims.cmid,
                    "session_no": int(row.session_no),
                    "notice_version": notice_version,
                    **(
                        {"presented_notice_version": presented}
                        if presented is not None
                        else {}
                    ),
                },
            )
    except _Refused as exc:
        refused = exc.err
        if exc.audit_reason is not None:
            async with engine.begin() as conn:
                await write_event(
                    conn,
                    exc.event,
                    actor="api",
                    site_id=claims.site,
                    reason_code=exc.audit_reason,
                    data={"cmid": claims.cmid, **exc.audit_data},
                )
    except IntegrityError as exc:
        if _ONE_OPEN not in str(exc.orig):
            raise
        refused = start_refused("session_already_open")
    if refused is not None:
        raise fail(refused)
    minted = tokens_of(request).mint_session(
        sub=claims.sub,
        site=claims.site,
        cmid=claims.cmid,
        sid=row.id,
        seq=int(row.session_no),
        epoch=int(row.epoch),
        deadline=None,
    )
    return JSONResponse(
        status_code=202,
        content={
            "session_id": str(row.id),
            "state": "STARTING",
            "token": minted.token,
            "session_no": int(row.session_no),
        },
        headers={"Cache-Control": "no-store"},
    )
