"""``POST /api/v1/assess/sessions/{sid}/retry``: "Riprova" (REQ-086, plan M4.4).

It only asks the server to try now: the ``PendingTurnDriver`` polls at once instead of at its
next interval. It never changes the session's state, never ends an OUTAGE (only the worker's
probe does, with the credit) and never touches the deadline. Rate-limited per session
(``VEKTRA_ASSESS_RETRY_RPM``): a button pressed in a loop is refused with 429, nothing done.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from vektra_assess.api.common import fail
from vektra_assess.api.deps import settings_of
from vektra_assess.api.gate import verify_assess_token_session
from vektra_assess.api.pending import pending_driver_of
from vektra_assess.api.session_gate import LIVE, session_tx
from vektra_assess.api.state import limiter_of
from vektra_assess.errors import rate_limited
from vektra_assess.tokens import SessionClaims

router = APIRouter()


@router.post("/sessions/{sid}/retry", status_code=202)
async def retry(
    sid: UUID,
    request: Request,
    claims: SessionClaims = Depends(verify_assess_token_session),
) -> JSONResponse:
    allowed, _headers = limiter_of(request, "retry").check(
        str(sid), settings_of(request).retry_rpm
    )
    if not allowed:
        raise fail(rate_limited(60, "retry_rate"))
    async with session_tx(
        request, claims, sid, route="retry", allowed=LIVE, lock=False
    ) as (_conn, row):
        state, pending = row.state, row.pending_reply_since is not None
    driver = pending_driver_of(request.app)
    if driver is not None and pending:
        driver.nudge()
    return JSONResponse(
        status_code=202,
        content={"session_id": str(sid), "state": state, "reply_pending": pending},
        headers={"Cache-Control": "no-store"},
    )
