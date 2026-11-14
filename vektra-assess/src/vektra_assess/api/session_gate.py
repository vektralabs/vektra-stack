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
LMS or attempt ids do not exist on any route: the attempt was selected server-side at Start.

``session_tx`` runs steps 2-6 inside the caller's transaction and yields the locked row, so the
route's own writes (a turn, a close) commit or roll back together with the gate's lock.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vektra_assess.api.common import engine_of, fail, is_fresh
from vektra_assess.api.deps import settings_of
from vektra_assess.api.material import MaterialUnavailable
from vektra_assess.api.site_binding import bind_site
from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.errors import (
    assess_unavailable,
    session_closed,
    session_not_found,
    start_refused,
    state_conflict,
    token_invalid,
)
from vektra_assess.tokens import PreClaims, SessionClaims
from vektra_shared.errors import ErrorResponse

LIVE = frozenset({"ACTIVE", "OUTAGE"})
ANY_OPEN = frozenset({"STARTING", "ACTIVE", "OUTAGE"})

_ROW = (
    "SELECT id, site_id, cmid, student_userid, session_no, epoch, state, close_reason, "
    "deadline_at, committed_at, seq, frozen_policy, duration_s, reply_grace_s, "
    "pending_reply_since, credited_outage_s, created_at, tokens_used, now() AS server_now "
    "FROM assess_sessions WHERE id = :sid"
)
_EXISTS = text("SELECT assess_session_exists(:sid)")
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
    tokens_used: int
    server_now: datetime


async def load_row(
    conn: AsyncConnection, sid: UUID, *, lock: bool = True
) -> SessionRow | None:
    """The session row as the gate reads it, for server-side work with no token (the
    ``PendingTurnDriver``): no owner check is possible or needed there."""
    raw = (
        await conn.execute(_ROW_LOCKED if lock else _ROW_PLAIN, {"sid": sid})
    ).first()
    return SessionRow(**raw._mapping) if raw is not None else None


def on_session_closed(app: Any, sid: UUID) -> None:
    """The ONE API-side hook after a session is closed or its token revoked (review fix 7):
    the DEK leaves the process and the reply in flight, if any, is cancelled so the model stops
    generating (the runner closes the provider stream on cancellation). Called by every API
    path that closes (Fine, the lazy close, budget_exhausted, the third failed call) or sees a
    session closed or revoked. A reply never cancels itself: the caller inside the reply task
    just ends. Closes made by the worker (sweeper, probe, enrolment) reach a running reply
    through its own refused append."""
    cache = getattr(app.state, "assess_dek_cache", None)
    if cache is not None:
        cache.evict(sid)
    driver = getattr(app.state, "assess_turn_driver", None)
    task = driver.tasks.get(sid) if driver is not None else None
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()


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
    enrolment_fresh_s: int | None = None,
    check_clock: bool = True,
    token_budget: int | None = None,
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
    # 3. the live row, matching owner, site and activity (and number and epoch for a session token).
    #    Read in a transaction bound to the token's site (ADR-0033 D9.2): the verifier bound the
    #    request already; binding here too makes the gate hold on its own, whoever calls it
    await bind_site(conn, claims.site)
    raw = (
        await conn.execute(_ROW_LOCKED if lock else _ROW_PLAIN, {"sid": sid})
    ).first()
    if raw is None:
        # RLS hid the row or there is none. The answer is the same not-found envelope; only the
        # audit tells the two apart, as it did before RLS: a session of another site is an
        # ownership mismatch (assess_session_exists, a definer function, one boolean)
        if (await conn.execute(_EXISTS, {"sid": sid})).scalar():
            raise GateRejected(
                session_not_found(),
                "not_owner",
                AssessAuditEvent.OWNERSHIP_MISMATCH_REJECTED,
            )
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
    # the clock again, in THIS transaction: a close that fell due
    # after the lazy close ran is refused as closed here, never answered with material (the
    # next request closes it). ``enforce_clock`` itself reads with check_clock=False.
    if (
        check_clock
        and row.state in LIVE
        and await due_close(conn, row, token_budget) is not None
    ):
        raise GateRejected(session_closed(), "closed")
    # 6. enrolment freshness (M3.7, NFR-014): the last worker re-check must say 'enrolled' and be
    #    at most ``enrolment_fresh_s`` old (database clock). A stopped worker or an unreadable
    #    enrolment fails closed: refused, state unchanged, audited.
    if enrolment_fresh_s is not None and row.state in LIVE:
        check = (await conn.execute(_ENROLMENT, {"sid": sid})).first()
        if (
            check is None
            or check.result != "enrolled"
            or not is_fresh(check.age_s, enrolment_fresh_s)
        ):
            raise GateRejected(assess_unavailable("enrolment_freshness"), "enrolment")
    return row


_ENROLMENT = text(
    "SELECT result, extract(epoch FROM clock_timestamp() - checked_at) AS age_s "
    "FROM assess_enrolment_checks WHERE session_id = :sid ORDER BY checked_at DESC LIMIT 1"
)


_CLOSE = text("SELECT assess_close_session(:sid, :epoch, :reason)")
#: the one definition of C and of the spent outage (revision 0032)
_OVER_CAP = text("SELECT assess_outage_spent(:sid) > assess_outage_cap(:d)")


async def due_close(
    conn: AsyncConnection, row: SessionRow, token_budget: int | None = None
) -> str | None:
    """The close reason the session's clock calls for now, or None (REQ-070, REQ-086).

    - ACTIVE past its deadline: ``timed_out``, unless a reply is still pending inside the grace
      (the input is locked meanwhile, the reply may finish; then the close happens);
    - OUTAGE whose cumulative outage passed C: ``service_failure``. An OUTAGE session is never
      timed out (its clock is frozen; the credit moves the deadline when it ends).
    """
    # REQ-083: usage already at the budget (the close after the last reply was lost: an
    # audit failure, a crash) is enforced here, before another turn
    if (
        row.state in LIVE
        and token_budget
        and row.tokens_used >= token_budget
        and row.pending_reply_since is None
    ):
        return "budget_exhausted"
    if row.state == "ACTIVE" and row.deadline_at is not None:
        if row.server_now >= row.deadline_at:
            in_grace = row.pending_reply_since is not None and row.server_now <= (
                row.deadline_at + timedelta(seconds=row.reply_grace_s)
            )
            if not in_grace:
                return "timed_out"
    if row.state == "OUTAGE":
        # a NULL duration means C = 0 (assess_outage_cap): closed, never left unbounded
        over = (
            await conn.execute(_OVER_CAP, {"sid": row.id, "d": row.duration_s})
        ).scalar_one()
        if over:
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
    budget = settings_of(request).session_token_budget
    # read without a lock first (review fix 5): only a session whose clock is over is locked,
    # re-read and closed; with nothing due, a lock=False route takes no row lock at all
    async with engine_of(request).connect() as conn:
        try:
            row = await gate(
                conn,
                claims,
                sid,
                allowed=ANY_OPEN | {"CLOSED"},
                allow_closed=True,
                lock=False,
                start_t_s=start_t_s,
                check_clock=False,
            )
        except GateRejected:
            return None
        reason = await due_close(conn, row, budget)
    if reason is None:
        return None
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
                check_clock=False,
            )
        except GateRejected:
            return None
        reason = await due_close(conn, row, budget)  # re-checked under the lock
        if reason is not None:
            await conn.execute(
                _CLOSE, {"sid": sid, "epoch": row.epoch, "reason": reason}
            )
    if reason is not None:
        on_session_closed(request.app, sid)
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
    require_enrolment: bool = False,
) -> AsyncIterator[tuple[AsyncConnection, SessionRow]]:
    """One transaction: the gate (steps 2-6) and then the route's own work (after the lazy
    close of step 5, which commits on its own)."""
    settings = settings_of(request)
    start_t_s = settings.start_t_s
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
                enrolment_fresh_s=(
                    settings.enrolment_freshness_s if require_enrolment else None
                ),
                token_budget=settings.session_token_budget,
            )
            try:
                yield conn, row
            except MaterialUnavailable:
                # live by its row but no material at this epoch (a hand-off being re-sealed, a
                # close racing this request): refused like a state conflict, nothing decrypted
                raise GateRejected(state_conflict(row.state), "material") from None
    except GateRejected as rejected:
        if rejected.step in ("closed", "epoch", "material"):
            on_session_closed(
                request.app, sid
            )  # closed or revoked: DEK out, reply stopped
        await audit_rejection(
            request, rejected, route=route, site_id=claims.site, session_id=sid
        )
        raise fail(rejected.err) from None
