"""``POST /api/v1/assess/sessions/{sid}/turns/{turn_no}/flag`` (REQ-103, plan M5.9; FEAT-034).

Thumbs up on one of the assistant's turns: body ``{"rating": "up"}``, while the session is ACTIVE
or OUTAGE only. It adds exactly one ``up`` row in ``assess_turn_flags`` and one content-free
``assess_flag_added`` audit row (``turn_no``, ``rating``), in one transaction under the gate's row
lock. A thumbs DOWN needs the student's reason, so it is not recorded here (400
``reason_required``): it goes through ``POST .../turns`` with ``contests_turn`` (``turns.py``),
which appends the reason as a student turn and rates the reply ``down`` in one transaction.

A rating is final: the same rating again is an idempotent 200 with nothing written, the other
one a 409 ``already_rated``. The turn itself is never touched, so its hash and every transcript
copy stay equal. After CLOSED the gate refuses it (409): copies made at the close are never
invalidated. Rate-limited per session (``VEKTRA_ASSESS_FLAG_RPM``).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from vektra_assess.api.common import fail, parse_body
from vektra_assess.api.deps import dek_cache_of, handoff_private_of, settings_of
from vektra_assess.api.gate import verify_assess_token_session
from vektra_assess.api.material import load_material
from vektra_assess.api.models import FlagRequest
from vektra_assess.api.session_gate import LIVE, session_tx
from vektra_assess.api.state import limiter_of
from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.errors import already_rated, bad_request, rate_limited
from vektra_assess.tokens import SessionClaims

router = APIRouter()

RATING_UP = "up"
RATING_DOWN = "down"

RATED = text(
    "SELECT rating FROM assess_turn_flags WHERE session_id = :s AND turn_no = :t"
)
INSERT_RATING = text(
    "INSERT INTO assess_turn_flags (session_id, turn_no, rating, reason_turn_no) "
    "VALUES (:s, :t, :r, :reason)"
)


@router.post("/sessions/{sid}/turns/{turn_no}/flag")
async def flag_turn(
    sid: UUID,
    turn_no: int,
    request: Request,
    claims: SessionClaims = Depends(verify_assess_token_session),
) -> JSONResponse:
    body = await parse_body(request, FlagRequest)
    if body.rating != RATING_UP:
        # a thumbs down carries its reason: POST .../turns with ``contests_turn``
        raise fail(bad_request("reason_required"))
    created = False
    async with session_tx(request, claims, sid, route="flag_turn", allowed=LIVE) as (
        conn,
        row,
    ):
        # counted only once the gate passed (review fix 3): a foreign or stale token on this
        # sid never uses up the owner's limit
        allowed, _headers = limiter_of(request, "flag").check(
            str(sid), settings_of(request).flag_rpm
        )
        if not allowed:
            raise fail(rate_limited(60, "flag_rate"))
        # the API reads turns only through the material function (no SELECT on assess_turns);
        # no text is decrypted here, only the turn's role is read
        material = await load_material(
            conn, sid, row.epoch, handoff_private_of(request), dek_cache_of(request)
        )
        role = next((t.role for t in material.turns if t.turn_no == turn_no), None)
        if role != "bot":
            # no such turn, or the student's own: only the assistant's turns are flagged
            raise fail(bad_request("not_a_bot_turn"))
        existing = (await conn.execute(RATED, {"s": sid, "t": turn_no})).first()
        if existing is not None and existing.rating != RATING_UP:
            raise fail(already_rated(str(existing.rating)))
        if existing is None:
            await conn.execute(
                INSERT_RATING,
                {"s": sid, "t": turn_no, "r": RATING_UP, "reason": None},
            )
            await write_event(
                conn,
                AssessAuditEvent.FLAG_ADDED,
                actor="api",
                site_id=row.site_id,
                session_id=sid,
                data={"turn_no": turn_no, "rating": RATING_UP},
            )
            created = True
    return JSONResponse(
        status_code=201 if created else 200,
        content={"session_id": str(sid), "turn_no": turn_no, "rating": RATING_UP},
        headers={"Cache-Control": "no-store"},
    )
