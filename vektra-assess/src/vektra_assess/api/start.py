"""``POST /api/v1/assess/sessions``: Start, asynchronous (ARCH-068, REQ-071).

The pre-session token is the only input. First, cheap ADVISORY refusals that write no session,
token or job row (each audited ``assess_start_refused`` with its reason code):

- the worker is not ready (heartbeat missing, stale or not ``ready``): 503;
- the activity is not enabled or has no cap (BR-013): 409 ``activity_not_configured``;
- admission: ``VEKTRA_ASSESS_ADMISSION_K`` or more non-closed sessions: 503 (NFR-020).

Then ONE transaction:

1. consume the jti: only a row that was minted for exactly this (site, sub, cmid), by a key that is
   still the site's mint key, unexpired and unconsumed (mint-to-Start correlation, ARCH-067);
2. refuse when the student already had a session for the activity and no reset grant covers a new
   one (REQ-087: session #N+1 exists only through a reset);
3. INSERT the STARTING row; the partial unique index on (site, sub, cmid) WHERE state <> 'CLOSED'
   makes a second concurrent Start fail here, which rolls the whole transaction back, so its jti
   is not consumed either;
4. INSERT the ``requested`` start attempt, ``enqueue_job('preflight')`` (INSERT + NOTIFY) and the
   ``assess_start`` audit event with the privacy-notice version.

202 returns the session id and a session-bound token. The worker pre-flight is authoritative: it
re-validates everything and commits or rolls back. Nothing on this path reads Moodle or content.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from vektra_assess.api.common import engine_of, fail
from vektra_assess.api.deps import settings_of, tokens_of
from vektra_assess.api.gate import verify_assess_token_pre
from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.errors import (
    assess_unavailable,
    capacity_reached,
    start_refused,
    token_invalid,
)
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
_LIVE = text("SELECT count(*) FROM assess_sessions WHERE state <> 'CLOSED'")
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

    def __init__(self, err: ErrorResponse) -> None:
        super().__init__(err.code)
        self.err = err


async def _refuse(
    request: Request,
    claims: PreClaims,
    reason: str,
    err: ErrorResponse,
) -> None:
    """Audit an advisory refusal (its own transaction: nothing else was written) and raise."""
    async with engine_of(request).begin() as conn:
        await write_event(
            conn,
            AssessAuditEvent.START_REFUSED,
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
        or (beat[1] is not None and beat[1] != "ready")
    ):
        await _refuse(
            request, claims, "worker_not_ready", assess_unavailable("worker_heartbeat")
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
            if consumed is None:
                raise _Refused(token_invalid())
            hist = (
                await conn.execute(
                    _HISTORY, {"s": claims.site, "u": claims.sub, "c": claims.cmid}
                )
            ).one()
            if int(hist.open):
                raise _Refused(start_refused("session_already_open"))
            if int(hist.closed) > int(hist.grants):
                raise _Refused(start_refused("already_discussed"))
            row = (
                await conn.execute(
                    _INSERT_SESSION,
                    {
                        "s": claims.site,
                        "c": claims.cmid,
                        "u": claims.sub,
                        "t": settings.start_t_s,
                        "nv": settings.privacy_notice_version,
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
                    "notice_version": settings.privacy_notice_version,
                },
            )
    except _Refused as exc:
        refused = exc.err
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
