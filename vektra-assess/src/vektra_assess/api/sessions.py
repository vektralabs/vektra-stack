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
from sqlalchemy import text

from vektra_assess.api.common import engine_of, fail
from vektra_assess.api.deps import (
    dek_cache_of,
    handoff_private_of,
    settings_of,
    tokens_of,
)
from vektra_assess.api.events import broker_of, status_key
from vektra_assess.api.gate import verify_assess_token_any
from vektra_assess.api.material import Material, MaterialUnavailable, load_material
from vektra_assess.api.session_gate import (
    ANY_OPEN,
    LIVE,
    GateRejected,
    SessionRow,
    audit_rejection,
    enforce_clock,
    gate,
    on_session_closed,
)
from vektra_assess.api.start import CONSUME_JTI
from vektra_assess.errors import state_conflict, token_invalid
from vektra_assess.provider_alerts import outage_reason
from vektra_assess.timing import remaining_s
from vektra_assess.tokens import PreClaims, SessionClaims, TokenError

router = APIRouter()

_FLAGGED = text("SELECT turn_no FROM assess_turn_flags WHERE session_id = :s")
_OUTAGE_OPEN = text(
    "SELECT started_at, reason_code FROM assess_outage_incidents "
    "WHERE session_id = :s AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def view(
    row: SessionRow,
    material: Material | None,
    paused_at: Any = None,
    flagged: frozenset[int] = frozenset(),
    outage_code: Any = None,
) -> dict[str, Any]:
    """``paused_at``: the open outage's start while the session is in OUTAGE: the timer is
    paused there, so the time left is measured against it. ``outage_code``: that interval's
    ``reason_code``, served as the closed ``outage_reason`` (OUTAGE only, optional)."""
    turns = []
    if material is not None:
        turns = [
            {
                "turn_no": t.turn_no,
                "role": t.role,
                "kind": t.kind,
                "text": material.text_of(t),
                # REQ-103 "segnalato dallo studente": served back on reload and resume
                "flagged": t.turn_no in flagged,
            }
            for t in material.turns
        ]
    pending = bool(
        material is not None and material.turns and material.turns[-1].role == "student"
    )
    body: dict[str, Any] = {
        "session_id": str(row.id),
        "state": row.state,
        "close_reason": row.close_reason,
        "session_no": row.session_no,
        "deadline_at": _iso(row.deadline_at),
        "server_now": _iso(row.server_now),
        # on the server's clock only (REQ-069): the client never sends a time
        "remaining_s": (
            remaining_s(row.deadline_at, paused_at or row.server_now)
            if row.deadline_at is not None
            else None
        ),
        "committed_at": _iso(row.committed_at),
        "reply_pending": pending,
        "turns": turns,
    }
    if row.state == "OUTAGE":
        body["outage_reason"] = outage_reason(outage_code)
    return body


@router.get("/sessions/{sid}")
async def get_session(
    sid: UUID,
    request: Request,
    claims: SessionClaims | PreClaims = Depends(verify_assess_token_any),
) -> JSONResponse:
    resume = isinstance(claims, PreClaims)
    route = "resume_session" if resume else "get_session"
    start_t_s = settings_of(request).start_t_s
    await enforce_clock(request, claims, sid, start_t_s)  # gate step 5: the lazy close
    # the stream's cursor, read BEFORE the snapshot: the client's stream starts after it, so an
    # event the snapshot already reflects is not replayed as live, and an event published
    # meanwhile is at worst delivered once more (an event is never skipped)
    broker = broker_of(request)
    last_id, reply_mark = broker.cursor(sid)
    try:
        async with engine_of(request).begin() as conn:
            # the outage position read before the snapshot too (so never past it): the stream
            # skips only the outage events at or below it, which the snapshot already shows
            key = await status_key(conn, sid)
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
            row = await gate(
                conn,
                claims,
                sid,
                allowed=ANY_OPEN,
                lock=resume,
                start_t_s=start_t_s,
                token_budget=settings_of(request).session_token_budget,
                # gate step 6 here too: the history is decrypted and a
                # resume token minted only for a freshly confirmed enrolment (fail closed)
                enrolment_fresh_s=settings_of(request).enrolment_freshness_s,
            )
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
            outage = (
                (await conn.execute(_OUTAGE_OPEN, {"s": sid})).first()
                if row.state == "OUTAGE"
                else None
            )
            paused_at = outage.started_at if outage is not None else None
            flagged = (
                frozenset(int(r[0]) for r in await conn.execute(_FLAGGED, {"s": sid}))
                if material is not None
                else frozenset()
            )
            body = view(
                row,
                material,
                paused_at,
                flagged,
                outage.reason_code if outage is not None else None,
            )
            # every state this route serves: the Last-Event-ID the client's stream starts from.
            # A reply under way is not in the snapshot yet: its events from the first one on
            # are replayed (the client streams them into the pending reply)
            if body["reply_pending"] and reply_mark is not None:
                last_id = min(last_id, reply_mark)
            body["event_cursor"] = f"{broker.nonce}:{last_id}" + (
                f":{key}" if key is not None else ""
            )
            if resume:
                # minted INSIDE the transaction: a refusal (past deadline + grace) rolls the jti
                # consumption back, so the pre-session token is not burnt for nothing
                try:
                    minted = tokens_of(request).mint_session(
                        sub=row.student_userid,
                        site=row.site_id,
                        cmid=row.cmid,
                        sid=row.id,
                        seq=row.session_no,
                        epoch=row.epoch,
                        deadline=row.deadline_at,
                    )
                except TokenError:
                    raise GateRejected(token_invalid(), "past_deadline") from None
                body["token"] = minted.token
    except GateRejected as rejected:
        if rejected.step in ("closed", "epoch", "material"):
            on_session_closed(request.app, sid)
        await audit_rejection(
            request, rejected, route=route, site_id=claims.site, session_id=sid
        )
        raise fail(rejected.err) from None
    return JSONResponse(content=body, headers={"Cache-Control": "no-store"})
