"""``POST /api/v1/assess/sessions/{sid}/finish``: the student's "Fine" (REQ-072).

Closes the session ``finished`` through the plan-2 close function (state, reason, epoch bump,
hand-off delete, transcript job and audit event in one transaction). Idempotent: a second "Fine"
(another tab, a retry, the same token whose epoch the first close moved) finds the session
CLOSED and answers the same 200 without a second close or a second transcript job. There is no
cancel endpoint: the confirmation dialog is UI-only and does not touch the deadline.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from vektra_assess.api.common import fail
from vektra_assess.api.deps import dek_cache_of
from vektra_assess.api.gate import verify_assess_token_session
from vektra_assess.api.session_gate import LIVE, session_tx
from vektra_assess.errors import state_conflict
from vektra_assess.tokens import SessionClaims

router = APIRouter()

_CLOSE = text("SELECT assess_close_session(:sid, :epoch, 'finished')")


@router.post("/sessions/{sid}/finish")
async def finish_session(
    sid: UUID,
    request: Request,
    claims: SessionClaims = Depends(verify_assess_token_session),
) -> JSONResponse:
    async with session_tx(
        request,
        claims,
        sid,
        route="finish_session",
        allowed=LIVE | {"CLOSED"},
        allow_closed=True,  # a second Fine is an idempotent 200, not an error
    ) as (conn, row):
        if row.state != "CLOSED":
            ok = (await conn.execute(_CLOSE, {"sid": sid, "epoch": row.epoch})).scalar()
            if not ok:
                raise fail(state_conflict(row.state))
            reason = "finished"
        else:
            reason = row.close_reason or "finished"
    dek_cache_of(request).evict(sid)
    return JSONResponse(
        content={"session_id": str(sid), "state": "CLOSED", "close_reason": reason},
        headers={"Cache-Control": "no-store"},
    )
