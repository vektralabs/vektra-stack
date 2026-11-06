"""``GET /api/v1/assess/sessions/{sid}``: state, deadline, server time and turns, owner only.

With a SESSION token: the gate (steps 1-6) then, for a live session, the turns decrypted through
``get_session_material``. A STARTING session has no turns yet; a CLOSED one is 409 and nothing is
returned (the transcript is delivered by email, plan 10).

With a PRE-SESSION token: the RESUME path (REQ-069). One transaction consumes the jti, runs the
same gate (owner, site, activity; any open state) and re-mints a session token bound to the row's
current epoch and capped at its deadline plus grace. A CLOSED session is 409 and is never
re-minted (and the jti stays unconsumed, the transaction rolls back). Resume does NOT bump the
epoch: a second tab of the same owner keeps working and is serialized by the pending-reply rule.

``server_now`` is the database clock; the client's clock is never read.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from vektra_assess.api.common import engine_of, fail
from vektra_assess.api.deps import (
    dek_cache_of,
    handoff_private_of,
    tokens_of,
)
from vektra_assess.api.gate import verify_assess_token_any
from vektra_assess.api.material import Material, MaterialUnavailable, load_material
from vektra_assess.api.session_gate import (
    ANY_OPEN,
    LIVE,
    GateRejected,
    SessionRow,
    audit_rejection,
    gate,
)
from vektra_assess.api.start import CONSUME_JTI
from vektra_assess.errors import state_conflict, token_invalid
from vektra_assess.tokens import PreClaims, SessionClaims, TokenError

router = APIRouter()


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def view(row: SessionRow, material: Material | None) -> dict[str, Any]:
    turns = []
    if material is not None:
        turns = [
            {
                "turn_no": t.turn_no,
                "role": t.role,
                "kind": t.kind,
                "text": material.text_of(t),
            }
            for t in material.turns
        ]
    pending = bool(
        material is not None and material.turns and material.turns[-1].role == "student"
    )
    return {
        "session_id": str(row.id),
        "state": row.state,
        "close_reason": row.close_reason,
        "session_no": row.session_no,
        "deadline_at": _iso(row.deadline_at),
        "server_now": _iso(row.server_now),
        "committed_at": _iso(row.committed_at),
        "reply_pending": pending,
        "turns": turns,
    }


@router.get("/sessions/{sid}")
async def get_session(
    sid: UUID,
    request: Request,
    claims: SessionClaims | PreClaims = Depends(verify_assess_token_any),
) -> JSONResponse:
    resume = isinstance(claims, PreClaims)
    route = "resume_session" if resume else "get_session"
    try:
        async with engine_of(request).begin() as conn:
            if resume:
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
                    raise GateRejected(token_invalid(), "jti")
            row = await gate(conn, claims, sid, allowed=ANY_OPEN, lock=resume)
            material = None
            if row.state in LIVE:
                try:
                    material = await load_material(
                        conn,
                        sid,
                        row.epoch,
                        handoff_private_of(request),
                        dek_cache_of(request),
                    )
                except MaterialUnavailable:
                    raise GateRejected(state_conflict(row.state), "material") from None
            body = view(row, material)
    except GateRejected as rejected:
        if rejected.step in ("closed", "epoch", "material"):
            dek_cache_of(request).evict(sid)
        await audit_rejection(
            request, rejected, route=route, site_id=claims.site, session_id=sid
        )
        raise fail(rejected.err) from None
    if resume:
        try:
            minted = tokens_of(request).mint_session(
                sub=row.student_userid,
                site=row.site_id,
                cmid=row.cmid,
                sid=row.id,
                seq=row.session_no,
                epoch=row.epoch,
                deadline=row.deadline_at,
                now=row.server_now,
            )
        except TokenError:
            raise fail(
                token_invalid()
            ) from None  # past deadline + grace: nothing to resume
        body["token"] = minted.token
    return JSONResponse(content=body, headers={"Cache-Control": "no-store"})
