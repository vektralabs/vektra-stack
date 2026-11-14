"""Per-session event stream (ARCH-068, ADR-0030): ``EventBroker`` and the SSE route.

``GET /api/v1/assess/sessions/{sid}/events`` is ``text/event-stream`` read with ``fetch`` (the token
travels in the Authorization header, never in the URL). Every event of a session carries a
monotonic per-session ``id``; a reconnect with ``Last-Event-ID`` replays what the bounded ring
still holds after that id. A connection always starts with a ``state`` snapshot (no id, so it does
not move the client's cursor); persisted turns come from ``GET /sessions/{sid}``, never from here.

The event catalogue is owned by this plan (plan 9 consumes it and adds none): ``state``,
``committed``, ``start_rolled_back``, ``capacity_refused``, ``turn_accepted``, ``reply_delta``,
``reply_done``, ``reply_interrupted``, ``warning``, ``budget_warning``, ``outage_start``,
``outage_end``, ``closed``. Payloads are content-free (ids, states, timestamps, codes) except
``reply_delta``, which carries post-checked text only.

State changes that happen elsewhere (the worker's commit or rollback, a close) are observed by
polling the session row (1 s): the broker remembers the last state per session and publishes one
event per CHANGE, however many connections poll. The broker is in-process: correct with a single
API process, which is today's deployment (plan Notes, risk for plan 13).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from vektra_assess.api.common import engine_of
from vektra_assess.api.deps import settings_of
from vektra_assess.api.gate import verify_assess_token_session
from vektra_assess.api.session_gate import ANY_OPEN, session_tx
from vektra_assess.errors import close_message_code
from vektra_assess.provider_alerts import outage_reason
from vektra_assess.start_rules import SYSTEM_ROLLBACK_REASONS
from vektra_assess.timing import due_warnings
from vektra_assess.tokens import SessionClaims

EVENT_TYPES = frozenset(
    {
        "state",
        "committed",
        "start_rolled_back",
        "capacity_refused",
        "turn_accepted",
        "reply_delta",
        "reply_done",
        "reply_interrupted",
        "warning",
        "budget_warning",
        "outage_start",
        "outage_end",
        "closed",
    }
)
TERMINAL = frozenset({"closed", "start_rolled_back"})
#: the only event whose payload may carry text (post-checked reply text)
CONTENT_EVENTS = frozenset({"reply_delta"})


@dataclass(frozen=True)
class Event:
    id: int
    type: str
    data: dict[str, Any]
    #: the session epoch a content event was produced under (never serialized): a stream
    #: delivers it only to a token of the same epoch, re-checked against the row (M3.4)
    epoch: int | None = None

    def sse(self) -> str:
        payload = json.dumps(self.data, separators=(",", ":"), ensure_ascii=False)
        return f"id: {self.id}\nevent: {self.type}\ndata: {payload}\n\n"


@dataclass
class _Channel:
    ring: deque[Event]
    next_id: int = 1
    last_state: str | None = None
    touched: float = 0.0
    ended_at: float | None = None  # when a terminal event was published
    warned: set[tuple[int, str]] = field(default_factory=set)  # (minutes, deadline ISO)
    signal: asyncio.Event = field(default_factory=asyncio.Event)


class EventBroker:
    def __init__(
        self,
        *,
        ring_size: int = 512,
        idle_ttl_s: float = 3 * 3600.0,
        closed_ttl_s: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """A channel ended by ``closed`` / ``start_rolled_back`` stays readable for
        ``closed_ttl_s`` (every open tab and every reconnect drains it); ``sweep_idle`` then drops
        it, and drops any channel idle for ``idle_ttl_s``."""
        self._ring = ring_size
        self._ttl = idle_ttl_s
        self._closed_ttl = closed_ttl_s
        self._clock = clock
        self._channels: dict[UUID, _Channel] = {}

    def _channel(self, sid: UUID) -> _Channel:
        ch = self._channels.get(sid)
        if ch is None:
            ch = _Channel(ring=deque(maxlen=self._ring))
            self._channels[sid] = ch
        ch.touched = self._clock()
        return ch

    def publish(
        self,
        sid: UUID,
        type_: str,
        data: dict[str, Any] | None = None,
        *,
        epoch: int | None = None,
    ) -> Event:
        if type_ not in EVENT_TYPES:
            raise ValueError(f"unknown event type {type_!r} (the catalogue is closed)")
        ch = self._channel(sid)
        if type_ in CONTENT_EVENTS and epoch is None:
            raise ValueError("a content event carries the epoch it was produced under")
        event = Event(ch.next_id, type_, dict(data or {}), epoch)
        ch.next_id += 1
        ch.ring.append(event)
        if type_ in TERMINAL and ch.ended_at is None:
            ch.ended_at = self._clock()
        ch.signal.set()
        ch.signal = asyncio.Event()
        return event

    def observe(
        self, sid: UUID, state: str | None, data: dict[str, Any]
    ) -> Event | None:
        """Report the polled state (``None``: the row is gone). Publishes on a change only."""
        ch = self._channel(sid)
        prev, ch.last_state = ch.last_state, state
        if prev == state:
            return None
        if state is None:
            return (
                self.publish(sid, "start_rolled_back", data)
                if prev in (None, "STARTING")
                else None
            )
        if state == "CLOSED":
            return self.publish(sid, "closed", data)
        if prev == "STARTING" and state == "ACTIVE":
            return self.publish(sid, "committed", data)
        # REQ-086 (M4.1, M4.2): the outage and its end; ``outage_end`` carries the moved deadline
        if prev == "ACTIVE" and state == "OUTAGE":
            return self.publish(sid, "outage_start", data)
        if prev == "OUTAGE" and state == "ACTIVE":
            return self.publish(sid, "outage_end", data)
        if prev is None:
            return (
                None  # the first observation: every connection already got a snapshot
            )
        return self.publish(sid, "state", data)

    def warn(
        self, sid: UUID, deadline_at: datetime, now: datetime, duration_s: int
    ) -> list[Event]:
        """Publish the 10 / 5 / 1 minute warnings due now, once per (session, threshold,
        deadline) however many connections poll (REQ-069). The deadline is the polled one, so
        an outage credit moves the warnings with it."""
        ch = self._channel(sid)
        out = []
        for minutes in due_warnings(deadline_at, now, duration_s, ch.warned):
            ch.warned.add((minutes, deadline_at.isoformat()))
            out.append(
                self.publish(
                    sid,
                    "warning",
                    {"minutes": minutes, "deadline_at": deadline_at.isoformat()},
                )
            )
        return out

    def after(self, sid: UUID, last_id: int) -> list[Event]:
        ch = self._channels.get(sid)
        if ch is None:
            return []
        return [e for e in ch.ring if e.id > last_id]

    async def wait(self, sid: UUID, last_id: int, timeout: float) -> None:
        ch = self._channel(sid)
        if any(e.id > last_id for e in ch.ring):
            return
        try:
            async with asyncio.timeout(timeout):
                await ch.signal.wait()
        except TimeoutError:
            pass

    def evict(self, sid: UUID) -> None:
        self._channels.pop(sid, None)

    def sweep_idle(self) -> int:
        """Drop channels ended more than ``closed_ttl_s`` ago or idle over ``idle_ttl_s``."""
        now = self._clock()
        gone = [
            sid
            for sid, ch in self._channels.items()
            if now - ch.touched > self._ttl
            or (ch.ended_at is not None and now - ch.ended_at > self._closed_ttl)
        ]
        for sid in gone:
            del self._channels[sid]
        return len(gone)

    def channels(self) -> list[UUID]:
        return list(self._channels)

    def __contains__(self, sid: object) -> bool:
        return sid in self._channels


def broker_of(request: Request) -> EventBroker:
    broker = getattr(request.app.state, "assess_events", None)
    if broker is None:
        broker = EventBroker()
        request.app.state.assess_events = broker
    return broker


# --- SSE route -------------------------------------------------------------------------------

router = APIRouter()

POLL_S = 1.0
KEEPALIVE_S = 15.0
MAX_STREAM_S = 3600.0  # the client reconnects with Last-Event-ID

_STATUS = text(
    "SELECT state, close_reason, epoch, deadline_at, duration_s, now() AS server_now, "
    # the open outage interval's reason (meaningful in OUTAGE only): what the student is told
    "(SELECT i.reason_code FROM assess_outage_incidents i "
    "WHERE i.session_id = assess_sessions.id AND i.ended_at IS NULL "
    "ORDER BY i.started_at DESC LIMIT 1) AS outage_code "
    "FROM assess_sessions WHERE id = :s"
)
_ROLLBACK_REASON = text(
    "WITH rb AS (SELECT site_id, cmid, student_userid, reason_code FROM assess_start_attempts "
    "WHERE session_id = :s AND outcome = 'rolled_back' ORDER BY id DESC LIMIT 1) "
    "SELECT rb.reason_code, (SELECT count(*) FROM (SELECT a.outcome, a.reason_code, "
    "sum(CASE WHEN a.outcome = 'rolled_back' AND (a.reason_code = ANY(:sys) "
    "OR a.reason_code LIKE 'capture_failed%') THEN 0 ELSE 1 END) "
    "OVER (ORDER BY a.id DESC) AS broken FROM assess_start_attempts a "
    "WHERE a.site_id = rb.site_id AND a.cmid = rb.cmid AND a.student_userid = rb.student_userid "
    "AND a.outcome IN ('committed', 'rolled_back')) h WHERE h.broken = 0) AS streak FROM rb"
).bindparams(sys=list(SYSTEM_ROLLBACK_REASONS))


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


async def _poll(
    request: Request, sid: UUID
) -> tuple[str | None, int | None, dict[str, Any]]:
    async with engine_of(request).connect() as conn:
        row = (await conn.execute(_STATUS, {"s": sid})).first()
        if row is None:
            rb = (await conn.execute(_ROLLBACK_REASON, {"s": sid})).first()
            reason = rb.reason_code if rb is not None else None
            if rb is not None and int(rb.streak) >= int(
                getattr(settings_of(request), "rollback_notify_after", 3)
            ):
                # REQ-071: "Problema tecnico persistente" while the streak of system rollbacks
                # of this student and activity lasts (the teacher and operator were notified)
                reason = "technical_problem_persistent"
            return None, None, {"reason": reason}
    if row.state == "ACTIVE" and row.deadline_at is not None and row.duration_s:
        broker_of(request).warn(
            sid, row.deadline_at, row.server_now, int(row.duration_s)
        )
    data = {
        "state": row.state,
        "close_reason": row.close_reason,
        "message_code": close_message_code(row.close_reason),
        "deadline_at": _iso(row.deadline_at),
        "server_now": _iso(row.server_now),
    }
    if row.state == "OUTAGE":
        # optional, OUTAGE only: provider_unavailable | service_misconfigured
        data["reason"] = outage_reason(row.outage_code)
    return str(row.state), int(row.epoch), data


def _last_event_id(request: Request) -> int:
    raw = request.headers.get("last-event-id", "0").strip()
    return int(raw) if raw.isascii() and raw.isdigit() and len(raw) < 12 else 0


def _settings(request: Request) -> tuple[float, float, float]:
    tune = getattr(request.app.state, "assess_sse_timing", None) or {}
    return (
        float(tune.get("poll_s", POLL_S)),
        float(tune.get("keepalive_s", KEEPALIVE_S)),
        float(tune.get("max_s", MAX_STREAM_S)),
    )


@router.get("/sessions/{sid}/events")
async def session_events(
    sid: UUID,
    request: Request,
    claims: SessionClaims = Depends(verify_assess_token_session),
) -> StreamingResponse:
    # the gate runs once, before the stream opens (steps 2-6; no decrypt on this route at all)
    async with (
        session_tx(
            request,
            claims,
            sid,
            route="session_events",
            allowed=ANY_OPEN | {"CLOSED"},
            lock=False,
            allow_closed=True,  # a reconnect may still drain ``closed``; nothing is decrypted here
            # the stream carries reply text: opening or reconnecting needs a fresh 'enrolled'
            # check (gate step 6, live rows only, so a CLOSED session still drains)
            require_enrolment=True,
        ) as (_conn, row)
    ):
        snapshot = {
            "state": row.state,
            "close_reason": row.close_reason,
            "message_code": close_message_code(row.close_reason),
            "deadline_at": _iso(row.deadline_at),
            "server_now": _iso(row.server_now),
        }
        if row.state == "OUTAGE":
            # the poll's own statement (one SQL text for the reason)
            status = (await _conn.execute(_STATUS, {"s": sid})).first()
            snapshot["reason"] = outage_reason(status.outage_code if status else None)
    broker = broker_of(request)
    # seed the channel with the state this connection saw, so the next poll reports a change
    # (STARTING -> ACTIVE is ``committed``) even when no other connection observed it first
    broker.observe(sid, row.state, snapshot)
    poll_s, keepalive_s, max_s = _settings(request)
    last = _last_event_id(request)

    async def stream() -> AsyncIterator[str]:
        nonlocal last
        payload = json.dumps(snapshot, separators=(",", ":"))
        yield f"event: state\ndata: {payload}\n\n"
        started = time.monotonic()
        next_poll = 0.0
        next_ping = time.monotonic() + keepalive_s
        if row.state == "CLOSED":
            # a reconnect after the close: replay what the ring still holds, end with ``closed``
            # content-free events only: after the close no reply text is served (GET is 409 too)
            replay = [
                e for e in broker.after(sid, last) if e.type not in CONTENT_EVENTS
            ]
            for event in replay:
                yield event.sse()
            if not any(e.type in TERMINAL for e in replay):
                yield f"event: closed\ndata: {payload}\n\n"
            return
        # the last row read (state, epoch, data) and when it was taken (monotonic)
        polled: tuple[str | None, int | None, dict[str, Any]] = (None, None, {})
        polled_at = float("-inf")
        while time.monotonic() - started < max_s:
            # the channel is never evicted here: every open tab and every reconnect must read the
            # terminal event; the opportunistic sweep removes closed channels after their TTL
            batch = broker.after(sid, last)
            live = True
            if any(e.type in CONTENT_EVENTS for e in batch):
                # reply text is delivered only while the row is live AT THIS TOKEN's epoch: a
                # close or a revocation (both bump the epoch) stops it, and an event produced
                # under another epoch is never sent. The row read used for
                # that check is at most poll_s old (review fix 6): a burst of deltas reuses the
                # last read instead of one read per batch, so a revocation can let through at
                # most poll_s of reply text, never more
                if time.monotonic() - polled_at >= poll_s:
                    polled = await _poll(request, sid)
                    polled_at = time.monotonic()
                    broker.observe(sid, polled[0], polled[2])
                state, epoch, _data = polled
                live = state in ("ACTIVE", "OUTAGE") and epoch == claims.epoch
                if state in ("ACTIVE", "OUTAGE") and epoch != claims.epoch:
                    return  # revoked under this connection: nothing more for this token
                batch = broker.after(sid, last)
            for event in batch:
                last = event.id
                if event.type in CONTENT_EVENTS and (
                    not live or event.epoch != claims.epoch
                ):
                    continue
                yield event.sse()
                if event.type in TERMINAL:
                    return
            now = time.monotonic()
            if now >= next_poll:
                polled = await _poll(request, sid)
                polled_at = time.monotonic()
                state, epoch, data = polled
                broker.observe(sid, state, data)
                next_poll = now + poll_s
                if state is not None and state != "CLOSED" and epoch != claims.epoch:
                    return  # revoked under this connection: the client must re-authenticate
                if broker.after(sid, last):
                    continue
            if now >= next_ping:
                yield ": keep-alive\n\n"
                next_ping = now + keepalive_s
            await broker.wait(
                sid, last, max(0.01, min(next_poll, next_ping) - time.monotonic())
            )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
