"""``GET /api/v1/assess/state``: the advisory REQ-068 button state for (token sub, cmid).

The plugin calls it on every results-page view with a pre-session token; the jti is NOT consumed
(those tokens are discarded and expire). The answer is built only from rows the API may read:

- the activity configuration (not enabled / not configured);
- the student's own sessions (an open one: ``active`` / ``starting``; more closed ones than reset
  grants: ``closed``, before any render state);
- otherwise the worker-computed render state (``assess_render_state`` and the showable date in
  ``assess_gate_inputs``), when fresh. Missing or stale: ``unavailable`` with reason ``checking``.

Every call queues at most one ``render_check`` per (site, user, cmid) (a pending row plus a deduped
job), so the next view is fresh. The response is ids, a state, a code and at most one date, and
that date is only ever the activity's own close or the student's own override: no other student's
date, name or count can appear (the worker never stores one where this route reads).

Rate limit (``VEKTRA_ASSESS_STATE_RPM`` per site, student and activity, assess-local limiter whose
empty windows the housekeeping sweep removes): 429 with ``retry_after`` beyond it. The global
per-key limiter (``app.state.rate_limiter``) stays unset.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from vektra_assess.api.common import engine_of, fail
from vektra_assess.api.deps import settings_of
from vektra_assess.api.gate import verify_assess_token_pre
from vektra_assess.errors import assess_unavailable, rate_limited
from vektra_assess.jobs import JobKind, enqueue_job, render_dedupe_key
from vektra_assess.tokens import PreClaims

router = APIRouter()


class Limiter(Protocol):
    def check(self, key: Any, rpm_limit: int | None) -> tuple[bool, dict[str, str]]: ...


def limiter_of(request: Request, name: str) -> Limiter:
    """An assess-local limiter injected by the composition root (``vektra_app``): never the
    dormant global per-key limiter. Missing is a configuration error, refused (fail closed)."""
    limiters = getattr(request.app.state, "assess_limiters", None) or {}
    limiter = limiters.get(name)
    if limiter is None:
        raise fail(assess_unavailable(f"rate_limiter_{name}"))
    return limiter  # type: ignore[no-any-return]


_FACTS = text(
    "SELECT c.enabled, c.cap_minutes, now() AS db_now, "
    "(SELECT count(*) FROM assess_sessions s WHERE s.site_id = c.site_id AND s.cmid = c.cmid "
    "AND s.student_userid = :u AND s.state = 'CLOSED') AS closed, "
    "(SELECT count(*) FROM assess_reset_grants g WHERE g.site_id = c.site_id AND g.cmid = c.cmid "
    "AND g.student_userid = :u) AS grants, "
    "(SELECT s.id FROM assess_sessions s WHERE s.site_id = c.site_id AND s.cmid = c.cmid "
    "AND s.student_userid = :u AND s.state <> 'CLOSED') AS open_sid, "
    "(SELECT s.state FROM assess_sessions s WHERE s.site_id = c.site_id AND s.cmid = c.cmid "
    "AND s.student_userid = :u AND s.state <> 'CLOSED') AS open_state, "
    "rs.status, rs.reason_code, rs.computed_at, "
    "extract(epoch FROM now() - rs.computed_at) AS age_s, gi.inputs "
    "FROM assess_activity_config c "
    "LEFT JOIN assess_render_state rs ON rs.site_id = c.site_id AND rs.cmid = c.cmid "
    "AND rs.student_userid = :u "
    "LEFT JOIN assess_gate_inputs gi ON gi.site_id = c.site_id AND gi.cmid = c.cmid "
    "AND gi.student_userid = :u "
    "WHERE c.site_id = :s AND c.cmid = :c"
)
_HINT = text("SELECT courseid_hint FROM assess_token_jti WHERE jti = :j")
_PENDING = text(
    "INSERT INTO assess_render_checks (site_id, student_userid, cmid) VALUES (:s, :u, :c)"
)
_PENDING_INDEX = "uq_assess_render_checks_pending"


async def queue_render_check(
    conn: Any, site: Any, userid: int, cmid: int, courseid: int | None
) -> None:
    """One pending render check and one deduped job per (site, user, cmid)."""
    try:
        async with conn.begin_nested():
            await conn.execute(_PENDING, {"s": site, "u": userid, "c": cmid})
    except IntegrityError as exc:
        if _PENDING_INDEX not in str(exc.orig):
            raise
    args: dict[str, str | int] = {
        "site_id": str(site),
        "student_userid": userid,
        "cmid": cmid,
    }
    if courseid:
        args["courseid"] = courseid
    await enqueue_job(
        conn,
        JobKind.RENDER_CHECK,
        args,
        dedupe_key=render_dedupe_key(site, userid, cmid),
    )


def _iso(epoch: Any) -> str | None:
    if not isinstance(epoch, int):
        return None
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def state_view(row: Any, fresh_s: int) -> dict[str, Any]:
    if row is None:
        return {"state": "not_enabled", "reason": "not_enabled"}
    # the student's own sessions decide before the configuration: disabling an activity never
    # hides a started session (BR-006) nor the transcript links of a closed one (state 6)
    if row.open_sid is not None:
        return {
            "state": "starting" if row.open_state == "STARTING" else "active",
            "reason": "session_open",
            "session_id": str(row.open_sid),
        }
    if int(row.closed) > int(row.grants):
        # the student's own CLOSED session with no reset granted since: decided here, before any
        # (possibly stale) render row (REQ-068 state 6)
        return {"state": "closed", "reason": "already_discussed"}
    if not row.enabled or row.cap_minutes is None:
        return {"state": "not_enabled", "reason": "not_enabled"}
    if row.status is None or row.age_s is None or float(row.age_s) > fresh_s:
        return {"state": "unavailable", "reason": "checking"}
    view: dict[str, Any] = {
        "state": row.status,
        "reason": row.reason_code,
        "computed_at": row.computed_at.isoformat(),
    }
    inputs = row.inputs or {}
    if row.reason_code == "close_not_passed" and isinstance(
        inputs.get("opens_at"), int
    ):
        view["opens_at"] = _iso(
            inputs["opens_at"]
        )  # the activity's own or the student's own
    if inputs.get("session_no_next", 1) > 1:
        view["session_no_next"] = int(inputs["session_no_next"])
    return view


@router.get("/state")
async def get_state(
    request: Request, claims: PreClaims = Depends(verify_assess_token_pre)
) -> JSONResponse:
    settings = settings_of(request)
    # keyed by (site, student, activity): a jti changes on every page view, so it would never
    # limit anything; stale keys are dropped by the opportunistic housekeeping sweep
    allowed, _headers = limiter_of(request, "state").check(
        (str(claims.site), claims.sub, claims.cmid), settings.state_rpm
    )
    if not allowed:
        raise fail(rate_limited(60, "state_rate"))
    params = {"s": claims.site, "u": claims.sub, "c": claims.cmid}
    async with engine_of(request).begin() as conn:
        row = (await conn.execute(_FACTS, params)).first()
        view = state_view(row, settings.render_fresh_s)
        if view["reason"] not in ("not_enabled", "session_open", "already_discussed"):
            hint = (await conn.execute(_HINT, {"j": claims.jti})).scalar()
            await queue_render_check(conn, claims.site, claims.sub, claims.cmid, hint)
    return JSONResponse(content=view, headers={"Cache-Control": "no-store"})
