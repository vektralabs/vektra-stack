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
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from vektra_assess.api.common import engine_of
from vektra_assess.api.gate import verify_assess_token_session
from vektra_assess.api.session_gate import ANY_OPEN, session_tx
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
        self, sid: UUID, type_: str, data: dict[str, Any] | None = None
    ) -> Event:
        if type_ not in EVENT_TYPES:
            raise ValueError(f"unknown event type {type_!r} (the catalogue is closed)")
        ch = self._channel(sid)
        event = Event(ch.next_id, type_, dict(data or {}))
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
        if prev is None:
            return (
                None  # the first observation: every connection already got a snapshot
            )
        return self.publish(sid, "state", data)

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
    "SELECT state, close_reason, epoch, deadline_at, now() AS server_now "
    "FROM assess_sessions WHERE id = :s"
)
_ROLLBACK_REASON = text(
    "SELECT reason_code FROM assess_start_attempts WHERE session_id = :s "
    "AND outcome = 'rolled_back' ORDER BY id DESC LIMIT 1"
)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


async def _poll(
    request: Request, sid: UUID
) -> tuple[str | None, int | None, dict[str, Any]]:
    async with engine_of(request).connect() as conn:
        row = (await conn.execute(_STATUS, {"s": sid})).first()
        if row is None:
            reason = (await conn.execute(_ROLLBACK_REASON, {"s": sid})).scalar()
            return None, None, {"reason": reason}
    return (
        str(row.state),
        int(row.epoch),
        {
            "state": row.state,
            "close_reason": row.close_reason,
            "deadline_at": _iso(row.deadline_at),
            "server_now": _iso(row.server_now),
        },
    )


def _last_event_id(request: Request) -> int:
    raw = request.headers.get("last-event-id", "0").strip()
    return int(raw) if raw.isdigit() and len(raw) < 12 else 0


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
        ) as (_conn, row)
    ):
        snapshot = {
            "state": row.state,
            "close_reason": row.close_reason,
            "deadline_at": _iso(row.deadline_at),
            "server_now": _iso(row.server_now),
        }
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
        while time.monotonic() - started < max_s:
            # the channel is never evicted here: every open tab and every reconnect must read the
            # terminal event; the opportunistic sweep removes closed channels after their TTL
            for event in broker.after(sid, last):
                yield event.sse()
                last = event.id
                if event.type in TERMINAL:
                    return
            now = time.monotonic()
            if now >= next_poll:
                state, epoch, data = await _poll(request, sid)
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
