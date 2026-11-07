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
from vektra_assess.timing import minutes_left
from vektra_assess.tokens import PreClaims

router = APIRouter()


#: the assess-local limiters the composition root must inject (REQ-083, plan 8 M5.3), each keyed
#: by a hashable of the session or the student, never the API key: ``state`` by (site, student,
#: activity), ``turns`` / ``retry`` / ``flag`` by session id, ``ui_page`` (the public discussion
#: UI page, plan 9) by client address, ``download`` (plan 10) by transcript and requester
LIMITER_NAMES = ("state", "turns", "retry", "flag", "ui_page", "download")


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


_ACTIVE = text(
    "SELECT s.session_no, s.state, s.deadline_at, s.duration_s, now() AS db_now, "
    "(SELECT max(i.started_at) FROM assess_outage_incidents i WHERE i.session_id = s.id "
    "AND i.ended_at IS NULL) AS paused_at FROM assess_sessions s WHERE s.id = :sid"
)
_CLOSED_AT = text(
    "SELECT max(closed_at) FROM assess_sessions WHERE site_id = :s AND cmid = :c "
    "AND student_userid = :u AND state = 'CLOSED'"
)
_PRE = text(
    "SELECT c.cap_minutes, c.rag_enabled, c.end_at, coalesce(x.extra_seconds, 0) AS extra_s "
    "FROM assess_activity_config c LEFT JOIN assess_extra_time x ON x.site_id = c.site_id "
    "AND x.cmid = c.cmid AND x.student_userid = :u WHERE c.site_id = :s AND c.cmid = :c"
)
_PREDICTED = ("readable", "partial", "unreadable")
_TRANSCRIPTS = text(
    "SELECT s.id, s.session_no, t.id AS tid, t.shredded_at FROM assess_sessions s "
    "LEFT JOIN assess_transcripts t ON t.session_id = s.id WHERE s.site_id = :s "
    "AND s.cmid = :c AND s.student_userid = :u AND s.state = 'CLOSED' ORDER BY s.session_no"
)


async def transcripts_of(conn: Any, params: dict[str, Any]) -> list[dict[str, Any]]:
    """The student's own transcripts, one per CLOSED session (plan 10): ``tid`` (the transcript
    id, which is its session id), ``n`` (the session number), ``available`` (generated and not
    shredded) and ``purged`` (shredded: plan 12's purge; false until then)."""
    rows = (await conn.execute(_TRANSCRIPTS, params)).all()
    return [
        {
            "tid": str(r.id),
            "n": int(r.session_no),
            "available": r.tid is not None and r.shredded_at is None,
            "purged": r.tid is not None and r.shredded_at is not None,
        }
        for r in rows
    ]


async def enrich(
    conn: Any, view: dict[str, Any], row: Any, params: dict[str, Any], settings: Any
) -> dict[str, Any]:
    """The extended contract agreed with the plugin (plan 9): optional fields, present only in
    the states that call for them, data only (the plugin owns every text).

    - ``active{session_no, minutes_left?, deadline_at?}``: active and starting;
    - ``selected_attempt{attempt_no}``: when the render worker recorded the selected attempt
      (the plugin compares it with the attempt its page shows: REQ-068 state 9);
    - ``pre_session{duration_min, recipients?, course_material_used, limits, privacy,
      previous_sessions, files?}``: eligible and reset_granted;
    - ``closed_at``: closed (state 6); ``ended_at``: ended (state 11);
    - ``transcripts[{tid, n, available, purged}]``: whenever the student has a CLOSED session
      (plan 10), whatever the state; ``pre_session.recipients[{role, name}]`` comes from the
      worker-written gate_inputs (the render check reads the course's graders), never an address.
    """
    state = view["state"]
    inputs = (row.inputs or {}) if row is not None else {}
    if state in ("active", "starting") and view.get("session_id"):
        s = (await conn.execute(_ACTIVE, {"sid": view["session_id"]})).first()
        if s is not None:
            active: dict[str, Any] = {"session_no": int(s.session_no)}
            if s.deadline_at is not None:
                now = s.paused_at or s.db_now  # the timer is frozen in OUTAGE
                active["minutes_left"] = minutes_left(
                    (s.deadline_at - now).total_seconds()
                )
                active["deadline_at"] = s.deadline_at.isoformat()
            elif s.duration_s:
                active["minutes_left"] = minutes_left(int(s.duration_s))
            view["active"] = active
    if row is not None and int(row.closed):
        transcripts = await transcripts_of(conn, params)
        if transcripts:
            view["transcripts"] = transcripts
    if state == "closed":
        closed_at = (await conn.execute(_CLOSED_AT, params)).scalar()
        if closed_at is not None:
            view["closed_at"] = closed_at.isoformat()
    attempt_no = inputs.get("selected_attempt_no")
    if isinstance(attempt_no, int) and state not in ("not_enabled", "closed"):
        view["selected_attempt"] = {"attempt_no": attempt_no}
    if state in ("ended", "eligible", "reset_granted"):
        pre = (await conn.execute(_PRE, params)).first()
        if state == "ended" and pre is not None and pre.end_at is not None:
            view["ended_at"] = pre.end_at.isoformat()
        if state in ("eligible", "reset_granted") and pre is not None:
            privacy: dict[str, Any] = {
                "notice_version": settings.privacy_notice_version
            }
            if settings.privacy_notice_url:
                privacy["notice_url"] = settings.privacy_notice_url
            pre_session: dict[str, Any] = {
                "duration_min": (int(pre.cap_minutes or 0) * 60 + int(pre.extra_s))
                // 60,
                "course_material_used": bool(pre.rag_enabled),
                "limits": {
                    "max_message_chars": settings.max_message_chars,
                    "messages_per_min": settings.messages_per_min,
                },
                "privacy": privacy,
                "previous_sessions": int(row.closed) if row is not None else 0,
            }
            recipients = inputs.get("recipients")
            if isinstance(recipients, list):
                pre_session["recipients"] = [
                    {"role": str(r["role"]), "name": str(r["name"])}
                    for r in recipients
                    if isinstance(r, dict) and {"role", "name"} <= set(r)
                ]
            files = inputs.get("files")
            if isinstance(files, list):
                pre_session["files"] = [
                    {"name": str(f["name"]), "predicted": str(f["predicted"])}
                    for f in files
                    if isinstance(f, dict) and f.get("predicted") in _PREDICTED
                ]
            view["pre_session"] = pre_session
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
        view = await enrich(conn, view, row, params, settings)
        if view["reason"] not in ("not_enabled", "session_open", "already_discussed"):
            hint = (await conn.execute(_HINT, {"j": claims.jti})).scalar()
            await queue_render_check(conn, claims.site, claims.sub, claims.cmid, hint)
    return JSONResponse(content=view, headers={"Cache-Control": "no-store"})
