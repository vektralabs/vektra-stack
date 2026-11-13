"""The teacher's session list of one activity (plan assess-teacher-ops 11b, REQ-076).

``GET /api/v1/assess/activities/{cmid}/sessions``, ``assess:teacher`` keys only, the actor, the
refresh hints and the page cursor as headers (``X-Assess-Actor``, ``X-Assess-Course``,
``X-Assess-Activity-Type``, ``X-Assess-Cursor``: no assess route reads the query string), the
actor verified as a fresh GRADER of the activity.

Per student (LMS user id only: the plugin resolves names), in user-id order, ``PAGE_SIZE``
students per page (``next_cursor`` = the last user id of the page, or null): sessions #1..#N with
state, close reason, the transcript id and its delivery status per recipient (``recipient_kind``
and ``status``, ``smtp_code`` and the error class; never an address nor its ref), the resets
with their reasons (decrypted here, for this grader view only), continuity choice and
``studente non avvisato via email`` (REQ-092 is out of v1), and the extra time. Activity level: the terminations with their reasons, and the gate panel: the
effective opening (the activity close, or the REQ-091 date when it decides), the C7 fallback
reason when Vektra cannot read overrides / extensions, the override holder's user id only for an
actor allowed to see it (never its reason, which is never stored), and ``N sessioni attive``.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from vektra_assess.api.common import (
    engine_of,
    fail,
    iso_time,
    opening_json,
    site_of_key,
)
from vektra_assess.api.deps import settings_of
from vektra_assess.api.teacher_ops import opening_for
from vektra_assess.api.teachers import (
    TeacherGate,
    ascii_id,
    can_view_overrides,
    header_int,
    path_id,
    verify_teacher,
)
from vektra_assess.api.transcripts import transcript_key_of
from vektra_assess.crypto.envelope import EnvelopeError, InvalidTag
from vektra_assess.crypto.kek import KeyNotAvailable
from vektra_assess.crypto.teacher_reason import Table, decrypt_reason
from vektra_assess.errors import bad_request
from vektra_assess.keys import AssessKeyring
from vektra_assess.privacy.purge import PURGED_KID
from vektra_shared.auth import ApiKeyInfo, require_exact_scope

log = structlog.get_logger(__name__)
router = APIRouter()

_guard = require_exact_scope("assess:teacher")

PAGE_SIZE = 50
#: REQ-076 / REQ-091 C7 wording, shown as is by the plugin
FALLBACK_REASON = "Vektra non può verificare proroghe/override per questa attività"
#: REQ-092 is out of v1 (product owner 2026-11-08): no reset is ever notified by email
NOT_NOTIFIED = "studente non avvisato via email"

_STUDENTS = text(
    "SELECT u FROM ("
    " SELECT student_userid AS u FROM assess_sessions WHERE site_id = :s AND cmid = :c"
    " UNION SELECT student_userid FROM assess_extra_time WHERE site_id = :s AND cmid = :c"
    " UNION SELECT student_userid FROM assess_reset_grants WHERE site_id = :s AND cmid = :c"
    ") AS t WHERE u > :after ORDER BY u LIMIT :n"
)
_SESSIONS = text(
    "SELECT s.id, s.student_userid, s.session_no, s.state, s.close_reason, s.committed_at, "
    "s.closed_at, t.id AS transcript_id FROM assess_sessions s "
    "LEFT JOIN assess_transcripts t ON t.session_id = s.id AND t.shredded_at IS NULL "
    "WHERE s.site_id = :s AND s.cmid = :c AND s.student_userid = ANY(:us) "
    "ORDER BY s.student_userid, s.session_no"
)
_DELIVERIES = text(
    "SELECT transcript_id, round, recipient_kind, status, attempts, smtp_code, "
    "last_error_class, sent_at FROM assess_deliveries WHERE kind = 'transcript' "
    "AND transcript_id = ANY(:ts) ORDER BY transcript_id, round, created_at"
)
_RESETS = text(
    "SELECT g.id, g.student_userid, g.session_no, g.reason_ct, g.reason_kid, "
    "g.include_previous_turns, "
    "g.granted_by, g.granted_at FROM assess_reset_grants g "
    "WHERE g.site_id = :s AND g.cmid = :c AND g.student_userid = ANY(:us) "
    "ORDER BY g.student_userid, g.session_no"
)
_EXTRA = text(
    "SELECT student_userid, extra_seconds FROM assess_extra_time WHERE site_id = :s "
    "AND cmid = :c AND student_userid = ANY(:us)"
)
_TERMINATIONS = text(
    "SELECT id, reason_ct, closed_count, rolled_back_count, also_disable, created_by, "
    "created_at FROM assess_terminations WHERE site_id = :s AND cmid = :c "
    "ORDER BY created_at DESC LIMIT 50"
)
_CONFIG = text(
    "SELECT extract(epoch FROM attested_opening_at)::bigint AS attested_at "
    "FROM assess_activity_config WHERE site_id = :s AND cmid = :c"
)
_ACTIVE = text(
    "SELECT count(*) FROM assess_sessions WHERE site_id = :s AND cmid = :c "
    "AND state IN ('ACTIVE', 'OUTAGE')"
)


def _reason(kek: AssessKeyring, table: Table, row_id: Any, blob: Any) -> dict[str, Any]:
    """``{reason, reason_state}``: the decrypted reason, or ``None`` with ``unreadable`` when it
    does not open (a retired kid, tampering): one row never takes the whole list down (review
    11b-4). Alerted, content-free."""
    try:
        return {
            "reason": decrypt_reason(kek, table, row_id, bytes(blob)),
            "reason_state": "readable",
        }
    except (InvalidTag, KeyNotAvailable, EnvelopeError, UnicodeDecodeError) as exc:
        log.error(
            "assess.teacher.alert",
            alert="teacher_reason_unreadable",
            kind=table,  # the closed table name, under an allowlisted key
            row_id=str(row_id),
            error_type=type(exc).__name__,
        )
        return {"reason": None, "reason_state": "unreadable"}


async def _gate_panel(
    conn: Any, site: UUID, cmid: int, gate: TeacherGate, actor: int
) -> dict[str, Any]:
    facts = gate.facts
    attested = (await conn.execute(_CONFIG, {"s": site, "c": cmid})).scalar()
    # the opening Start actually waits for, by the gate's own rule (the automatic close when
    # every input is readable, else the REQ-091 fallback); the holder's id stays behind
    # can_view_overrides
    opening = await opening_for(conn, site, facts, attested)
    holder = facts.get("override_holder")
    panel: dict[str, Any] = {
        "opening": opening_json(opening),
        # REQ-091: the fallback applies (PUT opening-fallback accepts a date); without one the
        # activity is not available. Both read from the same decision as ``opening``
        "fallback_reason": FALLBACK_REASON if opening.fallback_applies else None,
        "fallback_date_missing": opening.fallback_required,
        "active_sessions": int(
            (await conn.execute(_ACTIVE, {"s": site, "c": cmid})).scalar() or 0
        ),
    }
    if (
        isinstance(holder, int)
        and not isinstance(holder, bool)
        and can_view_overrides(gate, actor)
    ):
        panel["override_holder"] = holder
    return panel


async def _page(
    conn: Any,
    site: UUID,
    cmid: int,
    after: int,
    kek: AssessKeyring,
) -> tuple[list[dict[str, Any]], int | None]:
    users = [
        int(r.u)
        for r in (
            await conn.execute(
                _STUDENTS, {"s": site, "c": cmid, "after": after, "n": PAGE_SIZE + 1}
            )
        ).all()
    ]
    more = len(users) > PAGE_SIZE
    users = users[:PAGE_SIZE]
    if not users:
        return [], None
    p = {"s": site, "c": cmid, "us": users}
    sessions = (await conn.execute(_SESSIONS, p)).all()
    tids = [r.transcript_id for r in sessions if r.transcript_id is not None]
    deliveries: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    if tids:
        for d in (await conn.execute(_DELIVERIES, {"ts": tids})).all():
            deliveries[d.transcript_id].append(
                {
                    "round": int(d.round),
                    "recipient_kind": d.recipient_kind,
                    "status": d.status,
                    "attempts": int(d.attempts),
                    "smtp_code": d.smtp_code,
                    "error_class": d.last_error_class,
                    "sent_at": iso_time(d.sent_at),
                }
            )
    per: dict[int, dict[str, Any]] = {
        u: {"userid": u, "sessions": [], "resets": [], "extra_seconds": 0}
        for u in users
    }
    for r in sessions:
        per[int(r.student_userid)]["sessions"].append(
            {
                "session_id": str(r.id),
                "session_no": int(r.session_no),
                "state": r.state,
                "close_reason": r.close_reason,
                "committed_at": iso_time(r.committed_at),
                "closed_at": iso_time(r.closed_at),
                "transcript_id": str(r.transcript_id) if r.transcript_id else None,
                "deliveries": deliveries.get(r.transcript_id, [])
                if r.transcript_id
                else [],
            }
        )
    for g in (await conn.execute(_RESETS, p)).all():
        per[int(g.student_userid)]["resets"].append(
            {
                "reset_id": str(g.id),
                "after_session_no": int(g.session_no),
                # the retention purge removed the reason with the session's content (plan 12)
                **(
                    {"reason": None, "reason_state": "purged"}
                    if g.reason_kid == PURGED_KID
                    else _reason(kek, "assess_reset_grants", g.id, g.reason_ct)
                ),
                "include_previous_turns": bool(g.include_previous_turns),
                "granted_by": ascii_id(str(g.granted_by)),
                "granted_at": iso_time(g.granted_at),
                "student_notice": NOT_NOTIFIED,
            }
        )
    for x in (await conn.execute(_EXTRA, p)).all():
        per[int(x.student_userid)]["extra_seconds"] = int(x.extra_seconds)
    out = []
    for u in users:
        entry = per[u]
        entry["reset_count"] = len(entry["resets"])
        out.append(entry)
    return out, (users[-1] if more else None)


async def _terminations(
    conn: Any, site: UUID, cmid: int, kek: AssessKeyring
) -> list[dict[str, Any]]:
    return [
        {
            "termination_id": str(t.id),
            **_reason(kek, "assess_terminations", t.id, t.reason_ct),
            "closed": int(t.closed_count),
            "rolled_back": int(t.rolled_back_count),
            "also_disable": bool(t.also_disable),
            "by": ascii_id(str(t.created_by)),
            "at": iso_time(t.created_at),
        }
        for t in (await conn.execute(_TERMINATIONS, {"s": site, "c": cmid})).all()
    ]


@router.get("/activities/{cmid}/sessions")
async def list_sessions(
    cmid: str, request: Request, key: ApiKeyInfo = Depends(_guard)
) -> JSONResponse:
    c = path_id(cmid, "cmid")
    actor = header_int(request, "X-Assess-Actor")
    if actor is None:
        raise fail(bad_request("x_assess_actor"))
    courseid = header_int(request, "X-Assess-Course")
    kind = request.headers.get("X-Assess-Activity-Type")
    # a header, not ``?cursor=``: no assess route reads the query string (ADR-0030 static rule)
    raw_cursor = request.headers.get("X-Assess-Cursor")
    after = 0
    if raw_cursor is not None:
        parsed = ascii_id(raw_cursor)
        if parsed is None:
            raise fail(bad_request("cursor"))
        after = parsed
    kek = transcript_key_of(request)
    settings = settings_of(request)
    refused: Any = None
    async with engine_of(request).begin() as conn:
        site = await site_of_key(conn, key, "teacher")
        gate, refused = await verify_teacher(
            conn, site=site, cmid=c, actor=actor, courseid=courseid, kind=kind,
            max_age_s=settings.teacher_gate_max_age_s, route="list_sessions",
            role="grader",
        )  # fmt: skip
        if refused is None:
            students, next_cursor = await _page(conn, site, c, after, kek)
            view = {
                "cmid": c,
                "students": students,
                "next_cursor": next_cursor,
                "terminations": await _terminations(conn, site, c, kek)
                if after == 0
                else [],
                "gate": await _gate_panel(conn, site, c, gate, actor),
            }
    if refused is not None:
        raise refused
    return JSONResponse(view, headers={"Cache-Control": "no-store"})
