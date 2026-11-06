"""``POST /api/v1/assess/sessions/{sid}/turns/{turn_no}/flag`` (REQ-103, could; plan M5.9).

"Segnala un errore dell'assistente": while the session is ACTIVE or OUTAGE only, the student may
flag one of the assistant's turns. It adds exactly one marker ("segnalato dallo studente") in
``assess_turn_flags`` and one content-free ``assess_flag_added`` audit row, in one transaction
under the gate's row lock. A second flag of the same turn is an idempotent 200 with nothing
written. The turn itself is never touched, so its hash and every transcript copy stay equal.
After CLOSED the gate refuses it (409): copies made at the close are never invalidated.
Rate-limited per session (``VEKTRA_ASSESS_FLAG_RPM``).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from vektra_assess.api.common import fail
from vektra_assess.api.deps import dek_cache_of, handoff_private_of, settings_of
from vektra_assess.api.gate import verify_assess_token_session
from vektra_assess.api.material import load_material
from vektra_assess.api.session_gate import LIVE, session_tx
from vektra_assess.api.state import limiter_of
from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.errors import bad_request, rate_limited
from vektra_assess.tokens import SessionClaims

router = APIRouter()

_FLAGGED = text(
    "SELECT 1 FROM assess_turn_flags WHERE session_id = :s AND turn_no = :t"
)
_INSERT = text("INSERT INTO assess_turn_flags (session_id, turn_no) VALUES (:s, :t)")


@router.post("/sessions/{sid}/turns/{turn_no}/flag")
async def flag_turn(
    sid: UUID,
    turn_no: int,
    request: Request,
    claims: SessionClaims = Depends(verify_assess_token_session),
) -> JSONResponse:
    allowed, _headers = limiter_of(request, "flag").check(
        str(sid), settings_of(request).flag_rpm
    )
    if not allowed:
        raise fail(rate_limited(60, "flag_rate"))
    created = False
    async with session_tx(request, claims, sid, route="flag_turn", allowed=LIVE) as (
        conn,
        row,
    ):
        # the API reads turns only through the material function (no SELECT on assess_turns);
        # no text is decrypted here, only the turn's role is read
        material = await load_material(
            conn, sid, row.epoch, handoff_private_of(request), dek_cache_of(request)
        )
        role = next((t.role for t in material.turns if t.turn_no == turn_no), None)
        if role != "bot":
            # no such turn, or the student's own: only the assistant's turns are flagged
            raise fail(bad_request("not_a_bot_turn"))
        if (await conn.execute(_FLAGGED, {"s": sid, "t": turn_no})).first() is None:
            await conn.execute(_INSERT, {"s": sid, "t": turn_no})
            await write_event(
                conn,
                AssessAuditEvent.FLAG_ADDED,
                actor="api",
                site_id=row.site_id,
                session_id=sid,
                data={"turn_no": turn_no},
            )
            created = True
    return JSONResponse(
        status_code=201 if created else 200,
        content={"session_id": str(sid), "turn_no": turn_no, "flagged": True},
        headers={"Cache-Control": "no-store"},
    )
