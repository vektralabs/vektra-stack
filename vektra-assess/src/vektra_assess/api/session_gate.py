"""The fixed gate order of every session route (COMP-002), before any decrypt.

1. signature, kid, aud, iss, expiry, stage, ``loginas: false`` (``verify_assess_token_*``);
2. binding claims: a session token names this very ``sid`` (and its site, cmid, sub);
3. the live row, locked with ``SELECT ... FOR UPDATE``, matching owner, site, cmid, session
   number and epoch (an older epoch means the token was revoked);
4. state (per route);
5. deadline and outage cap (the lazy close lands here, plan M3.4);
6. enrolment freshness (plan M3.7).

Only then may a route call ``load_material`` (the one caller of ``get_session_material``). Any
failure is an in-session gate rejection: the transaction is rolled back (state unchanged), one
content-free audit row is written in its own transaction, and the request is refused. Client-sent
Moodle or attempt ids do not exist on any route: the attempt was selected server-side at Start.

``session_tx`` runs steps 2-6 inside the caller's transaction and yields the locked row, so the
route's own writes (a turn, a close) commit or roll back together with the gate's lock.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vektra_assess.api.common import engine_of, fail
from vektra_assess.api.deps import settings_of
from vektra_assess.api.material import MaterialUnavailable
from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.errors import (
    session_closed,
    session_not_found,
    start_refused,
    state_conflict,
    token_invalid,
)
from vektra_assess.timing import outage_cap_s
from vektra_assess.tokens import PreClaims, SessionClaims
from vektra_shared.errors import ErrorResponse

LIVE = frozenset({"ACTIVE", "OUTAGE"})
ANY_OPEN = frozenset({"STARTING", "ACTIVE", "OUTAGE"})

_ROW = (
    "SELECT id, site_id, cmid, student_userid, session_no, epoch, state, close_reason, "
    "deadline_at, committed_at, seq, frozen_policy, duration_s, reply_grace_s, "
    "pending_reply_since, credited_outage_s, created_at, now() AS server_now "
    "FROM assess_sessions WHERE id = :sid"
)
_ROW_LOCKED = text(_ROW + " FOR UPDATE")
_ROW_PLAIN = text(_ROW)


@dataclass(frozen=True)
class SessionRow:
    id: UUID
    site_id: UUID
    cmid: int
    student_userid: int
    session_no: int
    epoch: int
    state: str
    close_reason: str | None
    deadline_at: datetime | None
    committed_at: datetime | None
    seq: int
    frozen_policy: dict[str, Any] | None
    duration_s: int | None
    reply_grace_s: int
    pending_reply_since: datetime | None
    credited_outage_s: int
    created_at: datetime
    server_now: datetime


class GateRejected(Exception):
    def __init__(
        self,
        err: ErrorResponse,
        step: str,
        event: AssessAuditEvent = AssessAuditEvent.IN_SESSION_GATE_REJECTED,
    ) -> None:
        super().__init__(step)
        self.err = err
        self.step = step
        self.event = event


async def audit_rejection(
    request: Request,
    rejected: GateRejected,
    *,
    route: str,
    site_id: UUID | None,
    session_id: UUID | None,
) -> None:
    """The rejection's audit row, in its own transaction (the gate's was rolled back)."""
    async with engine_of(request).begin() as conn:
        await write_event(
            conn,
            rejected.event,
            actor="api",
            site_id=site_id,
            session_id=session_id,
            reason_code=rejected.step,
            data={"route": route},
        )


async def gate(
    conn: AsyncConnection,
    claims: SessionClaims | PreClaims,
    sid: UUID,
    *,
    allowed: Collection[str],
    lock: bool = True,
    allow_closed: bool = False,
    start_t_s: int = 60,
) -> SessionRow:
    """Steps 2-6. Raises ``GateRejected``; never touches session material.

    ``allow_closed``: only the event stream, which never decrypts, lets a CLOSED session through
    so a reconnecting tab can still drain its ``closed`` event.

    Epoch and close (plan M3.4): every close bumps the epoch, so a token minted before the close
    carries an older epoch. On a CLOSED row the owner's older-epoch token is reported as CLOSED
    (409, or a content-free ``closed`` on the event stream), never as a revoked token; on a live
    row any epoch mismatch is a revocation (401)."""
    # 2. binding: a session token is bound to exactly one sid
    if isinstance(claims, SessionClaims) and claims.sid != sid:
        raise GateRejected(
            session_not_found(),
            "sid_mismatch",
            AssessAuditEvent.OWNERSHIP_MISMATCH_REJECTED,
        )
    # 3. the live row, matching owner, site and activity (and number and epoch for a session token)
    raw = (
        await conn.execute(_ROW_LOCKED if lock else _ROW_PLAIN, {"sid": sid})
    ).first()
    if raw is None:
        raise GateRejected(session_not_found(), "not_found")
    row = SessionRow(**raw._mapping)
    if (row.site_id, row.student_userid, row.cmid) != (
        claims.site,
        claims.sub,
        claims.cmid,
    ):
        raise GateRejected(
            session_not_found(),
            "not_owner",
            AssessAuditEvent.OWNERSHIP_MISMATCH_REJECTED,
        )
    if isinstance(claims, SessionClaims):
        if claims.seq != row.session_no:
            raise GateRejected(session_not_found(), "session_no")
        if claims.epoch != row.epoch and not (
            row.state == "CLOSED" and claims.epoch < row.epoch
        ):
            raise GateRejected(token_invalid(), "epoch")
    # 4. state
    if row.state == "CLOSED" and not allow_closed:
        raise GateRejected(session_closed(), "closed")
    if row.state not in allowed:
        raise GateRejected(state_conflict(row.state), "state")
    # 5. deadline and outage cap. The lazy close itself runs in ``enforce_clock`` (its own,
    #    committed transaction) before this gate; here a STARTING row past T is reported as
    #    rolled back (the reaper deletes it)
    if (
        row.state == "STARTING"
        and (row.server_now - row.created_at).total_seconds() >= start_t_s
    ):
        raise GateRejected(start_refused("start_timeout"), "start_timeout")
    # 6. enrolment freshness: the 150 s re-check gate (plan M3.7) is bound here
    return row


_CLOSE = text("SELECT assess_close_session(:sid, :epoch, :reason)")
_OUTAGE_SPENT = text(
    "SELECT coalesce(sum(extract(epoch FROM coalesce(ended_at, now()) - started_at)), 0) "
    "FROM assess_outage_incidents WHERE session_id = :sid"
)


async def due_close(conn: AsyncConnection, row: SessionRow) -> str | None:
    """The close reason the session's clock calls for now, or None (REQ-070, REQ-086).

    - ACTIVE past its deadline: ``timed_out``, unless a reply is still pending inside the grace
      (the input is locked meanwhile, the reply may finish; then the close happens);
    - OUTAGE whose cumulative outage passed C: ``service_failure``. An OUTAGE session is never
      timed out (its clock is frozen; the credit moves the deadline when it ends).
    """
    if row.state == "ACTIVE" and row.deadline_at is not None:
        if row.server_now >= row.deadline_at:
            in_grace = row.pending_reply_since is not None and row.server_now <= (
                row.deadline_at + timedelta(seconds=row.reply_grace_s)
            )
            if not in_grace:
                return "timed_out"
    if row.state == "OUTAGE" and row.duration_s:
        spent = float((await conn.execute(_OUTAGE_SPENT, {"sid": row.id})).scalar_one())
        if spent > outage_cap_s(int(row.duration_s)):
            return "service_failure"
    return None


async def enforce_clock(
    request: Request, claims: SessionClaims | PreClaims, sid: UUID, start_t_s: int
) -> str | None:
    """Gate step 5, the lazy close (M3.4): on any session request whose owner checks pass, a
    session whose clock is over is closed NOW, in its own committed transaction, through the
    plan-2 close function (state, reason, epoch bump, hand-off delete, transcript job, audit).
    The route's own gate then sees a CLOSED row. Rejections are left to that gate (it audits
    them once)."""
    async with engine_of(request).begin() as conn:
        try:
            row = await gate(
                conn,
                claims,
                sid,
                allowed=ANY_OPEN | {"CLOSED"},
                allow_closed=True,
                lock=True,
                start_t_s=start_t_s,
            )
        except GateRejected:
            return None
        reason = await due_close(conn, row)
        if reason is not None:
            await conn.execute(
                _CLOSE, {"sid": sid, "epoch": row.epoch, "reason": reason}
            )
    if reason is not None:
        cache = getattr(request.app.state, "assess_dek_cache", None)
        if cache is not None:
            cache.evict(sid)
    return reason


@asynccontextmanager
async def session_tx(
    request: Request,
    claims: SessionClaims | PreClaims,
    sid: UUID,
    *,
    route: str,
    allowed: Collection[str],
    lock: bool = True,
    allow_closed: bool = False,
) -> AsyncIterator[tuple[AsyncConnection, SessionRow]]:
    """One transaction: the gate (steps 2-6) and then the route's own work (after the lazy
    close of step 5, which commits on its own)."""
    start_t_s = settings_of(request).start_t_s
    await enforce_clock(request, claims, sid, start_t_s)
    try:
        async with engine_of(request).begin() as conn:
            row = await gate(
                conn,
                claims,
                sid,
                allowed=allowed,
                lock=lock,
                allow_closed=allow_closed,
                start_t_s=start_t_s,
            )
            try:
                yield conn, row
            except MaterialUnavailable:
                # live by its row but no material at this epoch (a hand-off being re-sealed, a
                # close racing this request): refused like a state conflict, nothing decrypted
                raise GateRejected(state_conflict(row.state), "material") from None
    except GateRejected as rejected:
        if rejected.step in ("closed", "epoch", "material"):
            cache = getattr(request.app.state, "assess_dek_cache", None)
            if cache is not None:
                cache.evict(
                    sid
                )  # a closed or revoked session's DEK leaves the process now
        await audit_rejection(
            request, rejected, route=route, site_id=claims.site, session_id=sid
        )
        raise fail(rejected.err) from None
