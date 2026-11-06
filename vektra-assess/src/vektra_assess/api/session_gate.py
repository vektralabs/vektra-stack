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
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vektra_assess.api.common import engine_of, fail
from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.errors import (
    session_closed,
    session_not_found,
    state_conflict,
    token_invalid,
)
from vektra_assess.tokens import PreClaims, SessionClaims
from vektra_shared.errors import ErrorResponse

LIVE = frozenset({"ACTIVE", "OUTAGE"})
ANY_OPEN = frozenset({"STARTING", "ACTIVE", "OUTAGE"})

_ROW = (
    "SELECT id, site_id, cmid, student_userid, session_no, epoch, state, close_reason, "
    "deadline_at, committed_at, seq, frozen_policy, duration_s, now() AS server_now "
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
) -> SessionRow:
    """Steps 2-6. Raises ``GateRejected``; never touches session material."""
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
        if claims.epoch != row.epoch:
            raise GateRejected(token_invalid(), "epoch")
    # 4. state
    if row.state == "CLOSED":
        raise GateRejected(session_closed(), "closed")
    if row.state not in allowed:
        raise GateRejected(state_conflict(row.state), "state")
    # 5. deadline and outage cap: the lazy close (plan M3.4) is bound here
    # 6. enrolment freshness: the 150 s re-check gate (plan M3.7) is bound here
    return row


@asynccontextmanager
async def session_tx(
    request: Request,
    claims: SessionClaims | PreClaims,
    sid: UUID,
    *,
    route: str,
    allowed: Collection[str],
    lock: bool = True,
) -> AsyncIterator[tuple[AsyncConnection, SessionRow]]:
    """One transaction: the gate (steps 2-6) and then the route's own work."""
    try:
        async with engine_of(request).begin() as conn:
            row = await gate(conn, claims, sid, allowed=allowed, lock=lock)
            yield conn, row
    except GateRejected as rejected:
        await audit_rejection(
            request, rejected, route=route, site_id=claims.site, session_id=sid
        )
        raise fail(rejected.err) from None
