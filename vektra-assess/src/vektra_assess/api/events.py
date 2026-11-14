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

An event id is ``<nonce>.<gen>:<n>``: ``nonce`` is this broker instance's, ``gen`` the channel's
generation, so an id from before an API restart or a channel sweep is foreign and replays the
whole ring instead of being ahead of every new id. A connection without a usable
``Last-Event-ID`` (a reload) replays the whole ring; the state-type events (``outage_start``,
``outage_end``, ``warning``) carry ``at``, the database time at which the state they report was
read, so a client drops those its snapshot (``server_now``, the same clock) already reflects.

State changes that happen elsewhere (the worker's commit or rollback, a close) are observed by
polling the session row (1 s): the broker remembers the last state per session and publishes one
event per CHANGE, however many connections poll. The broker is in-process: correct with a single
API process, which is today's deployment (plan Notes, risk for plan 13).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NamedTuple
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
from vektra_assess.timing import due_warnings, remaining_s
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
#: the live session states, between which reads are ordered by ``outage_key``
LIVE = frozenset({"ACTIVE", "OUTAGE"})
#: the only event whose payload may carry text (post-checked reply text)
CONTENT_EVENTS = frozenset({"reply_delta"})


def _digits(raw: str) -> bool:
    return raw.isascii() and raw.isdigit() and len(raw) < 12


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def outage_key(state: str | None, outage_count: int | None) -> int | None:
    """One monotonic order over the live outage states: ACTIVE after ``c`` intervals is
    ``2c``, OUTAGE in the ``c``-th is ``2c - 1``. ``None`` outside ACTIVE / OUTAGE or with the
    count unknown. ``key // 2`` is the number of intervals that have ended."""
    if outage_count is None or state not in ("ACTIVE", "OUTAGE"):
        return None
    return 2 * outage_count - (1 if state == "OUTAGE" else 0)


@dataclass(frozen=True)
class Event:
    id: int
    type: str
    data: dict[str, Any]
    #: the session epoch a content event was produced under (never serialized): a stream
    #: delivers it only to a token of the same epoch, re-checked against the row (M3.4)
    epoch: int | None = None

    def sse(self, prefix: str) -> str:
        """``prefix``: the channel's ``<nonce>.<gen>`` (``EventBroker.id_prefix``)."""
        payload = json.dumps(self.data, separators=(",", ":"), ensure_ascii=False)
        return f"id: {prefix}:{self.id}\nevent: {self.type}\ndata: {payload}\n\n"


@dataclass
class _Channel:
    ring: deque[Event]
    gen: int = 0  # the broker-wide generation this channel was created with
    next_id: int = 1
    last_state: str | None = None
    last_key: int | None = None  # the outage_key of the last observation, when known
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
        #: this broker instance's id, and a generation per channel created: an event id is
        #: ``<nonce>.<gen>:<n>``, so a Last-Event-ID from another instance (an API restart) or
        #: from a swept and re-created channel is foreign instead of ahead of every new id
        self.nonce = secrets.token_hex(8)
        self._gen = 0

    def _channel(self, sid: UUID) -> _Channel:
        ch = self._channels.get(sid)
        if ch is None:
            self._gen += 1
            ch = _Channel(ring=deque(maxlen=self._ring), gen=self._gen)
            self._channels[sid] = ch
        ch.touched = self._clock()
        return ch

    def id_prefix(self, sid: UUID) -> str:
        """The ``<nonce>.<gen>`` of the session's event ids (creates the channel)."""
        return f"{self.nonce}.{self._channel(sid).gen}"

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
        self,
        sid: UUID,
        state: str | None,
        data: dict[str, Any],
        *,
        outage_count: int | None = None,
        at: datetime | None = None,
    ) -> Event | None:
        """Report the polled state (``None``: the row is gone). Publishes on a change only.

        ``outage_count``: the session's outage intervals, counted with the state (``None``:
        unknown). Between live states the reads are ordered by ``outage_key`` alone: a read not
        above the last key is old news and ignored; a higher one publishes ``outage_end`` if an
        interval ended since, then ``outage_start`` if one is open now. This also sees an outage
        that ended and another that began between two reads (OUTAGE then OUTAGE), and a whole
        one between them. ``data`` of a live read carries the current ``deadline_at`` (the one
        an ``outage_end`` reports, moved by the credit).

        ``at``: the database time the state was read at (``clock_timestamp()`` of the read, or
        taken after the commit that decided it), sent as ``at`` on the outage events.

        ``closed`` is terminal: after a terminal event nothing more is published."""
        ch = self._channel(sid)
        if ch.ended_at is not None or ch.last_state == "CLOSED":
            return None
        timed = dict(data, at=_iso(at)) if at is not None else data
        prev, prev_key = ch.last_state, ch.last_key
        if state in LIVE:
            key = outage_key(state, outage_count)
            if key is None and prev_key is not None:
                # a live read without its count (the driver's fallback): the next state in the
                # order, or the same one
                key = prev_key + (0 if state == prev else 1)
            if key is not None and prev_key is not None:
                # REQ-086 (M4.1, M4.2): one ordering rule between live reads
                if key <= prev_key:
                    return None
                ch.last_state, ch.last_key = state, key
                out = None
                if (
                    key // 2 > prev_key // 2
                ):  # an interval ended since (the moved deadline)
                    out = self.publish(sid, "outage_end", timed)
                if key % 2:  # and one is open now
                    out = self.publish(sid, "outage_start", timed)
                return out
            # no order yet: the first live read (after STARTING, or the channel's first)
            ch.last_state, ch.last_key = state, key
            if prev == "STARTING":
                return self.publish(
                    sid, "committed" if state == "ACTIVE" else "state", timed
                )
            if prev in LIVE and prev != state:  # counts unknown on both sides
                return self.publish(
                    sid, "outage_start" if state == "OUTAGE" else "outage_end", timed
                )
            return (
                None  # the first observation: every connection already got a snapshot
            )
        ch.last_state = state
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
        return None if prev is None else self.publish(sid, "state", data)

    def warn(
        self,
        sid: UUID,
        deadline_at: datetime,
        now: datetime,
        duration_s: int,
        at: datetime | None = None,
    ) -> list[Event]:
        """Publish the 10 / 5 / 1 minute warnings due now, once per (session, threshold,
        deadline) however many connections poll (REQ-069). The deadline is the polled one, so
        an outage credit moves the warnings with it. ``at``: the read's database time (the
        client drops a warning its snapshot is not older than). None after a terminal event."""
        ch = self._channel(sid)
        if ch.ended_at is not None or ch.last_state == "CLOSED":
            return []
        out = []
        for minutes in due_warnings(deadline_at, now, duration_s, ch.warned):
            ch.warned.add((minutes, deadline_at.isoformat()))
            data: dict[str, Any] = {
                "minutes": minutes,
                "deadline_at": deadline_at.isoformat(),
            }
            if at is not None:
                data["at"] = at.isoformat()
            out.append(self.publish(sid, "warning", data))
        return out

    def last_event_id(self, sid: UUID, raw: str | None) -> int:
        """A ``Last-Event-ID`` read against this broker: the id to stream after. Only
        ``<nonce>.<gen>:<n>`` of this instance and of the session's current channel counts, and
        only below the channel's next id. Anything else (another instance, a swept channel, a
        bare ``<n>``, unparseable, ahead) is foreign: 0, the whole ring replays (the client
        discards what its snapshot already shows: replies by turn_no, state events by ``at``)."""
        prefix, _, n_raw = (raw or "").strip().rpartition(":")
        ch = self._channels.get(sid)
        if ch is None or prefix != f"{self.nonce}.{ch.gen}" or not _digits(n_raw):
            return 0
        n = int(n_raw)
        return n if n < ch.next_id else 0

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

#: the database clock: the ``at`` of an event no status read decides (``reply_interrupted``
#: without a turn); a snapshot whose ``server_now`` is not below it was taken after it
DB_NOW = text("SELECT clock_timestamp()")
#: the open outage interval of the session row in scope (the latest one not ended)
_OPEN_OUTAGE = (
    "FROM assess_outage_incidents i WHERE i.session_id = assess_sessions.id "
    "AND i.ended_at IS NULL ORDER BY i.started_at DESC LIMIT 1"
)
#: the one status read (the stream's poll, its opening snapshot, and the turn driver after it
#: opened an outage): state, the open outage's reason and start, and the session's outage
#: intervals counted (never sent to a client: ``observe`` orders reads by it; intervals are only
#: added while the session is live, purge and shred run after the close, so it only grows), all
#: in ONE statement so they always agree. ``read_at`` (``clock_timestamp()``, evaluated during
#: the statement, so not before the data it reads) is the ``at`` of the events published from it
_STATUS = text(
    "SELECT state, close_reason, epoch, deadline_at, duration_s, now() AS server_now, "
    "clock_timestamp() AS read_at, "
    # read in OUTAGE only: what the student is told, and where the time left froze
    f"CASE WHEN state = 'OUTAGE' THEN (SELECT i.reason_code {_OPEN_OUTAGE}) END AS outage_code, "
    f"CASE WHEN state = 'OUTAGE' THEN (SELECT i.started_at {_OPEN_OUTAGE}) END AS paused_at, "
    "CASE WHEN state IN ('ACTIVE', 'OUTAGE') THEN (SELECT count(*) "
    "FROM assess_outage_incidents i WHERE i.session_id = assess_sessions.id) END "
    "AS outage_count "
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


def status_payload(row: Any) -> dict[str, Any]:
    """The payload of a ``_STATUS`` row: the opening ``state`` snapshot and any event the
    broker publishes from an observation (``outage_end`` carries the moved ``deadline_at``)."""
    data = {
        "state": row.state,
        "close_reason": row.close_reason,
        "message_code": close_message_code(row.close_reason),
        "deadline_at": _iso(row.deadline_at),
        "server_now": _iso(row.server_now),
    }
    if row.state == "OUTAGE":
        # optional, OUTAGE only: provider_unavailable | service_misconfigured
        data["reason"] = outage_reason(getattr(row, "outage_code", None))
        # optional, OUTAGE only: the time left, frozen at the open outage's start (the same
        # computation as GET /sessions/{sid}); outage_start and the opening state carry it
        paused = getattr(row, "paused_at", None) or row.server_now
        if row.deadline_at is not None and paused is not None:
            data["remaining_s"] = remaining_s(row.deadline_at, paused)
    return data


async def read_status(conn: Any, sid: UUID) -> Any:
    """One ``_STATUS`` read (None: no row)."""
    return (await conn.execute(_STATUS, {"s": sid})).first()


def status_count(row: Any) -> int | None:
    return int(row.outage_count) if row.outage_count is not None else None


class Polled(NamedTuple):
    """One ``_STATUS`` read; ``state`` None: the row is gone (a rolled-back Start)."""

    state: str | None
    epoch: int | None
    data: dict[str, Any]
    outage_count: int | None = None
    at: datetime | None = None  # the read's database time (``read_at``)


async def _poll(request: Request, sid: UUID) -> Polled:
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
            return Polled(None, None, {"reason": reason})
    if row.state == "ACTIVE" and row.deadline_at is not None and row.duration_s:
        broker_of(request).warn(
            sid, row.deadline_at, row.server_now, int(row.duration_s), at=row.read_at
        )
    return Polled(
        str(row.state),
        int(row.epoch),
        status_payload(row),
        status_count(row),
        row.read_at,
    )


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
        ) as (conn, row)
    ):
        # the snapshot is the poll's own read (one statement: state, the open outage's reason
        # and the outage count agree), so the seeding below publishes what a poll would
        status = (await conn.execute(_STATUS, {"s": sid})).first()
    # (no row any more: a Start rolled back after the gate; the gate's row stands, the poll
    # reports the rollback)
    seen = str(status.state) if status is not None else row.state
    snapshot = status_payload(status if status is not None else row)
    broker = broker_of(request)
    # read before the seeding below creates a channel: an id of a channel that is gone is foreign
    last = broker.last_event_id(sid, request.headers.get("last-event-id"))
    # seed the channel with the state this connection saw, so the next poll reports a change
    # (STARTING -> ACTIVE is ``committed``) even when no other connection observed it first
    broker.observe(
        sid,
        seen,
        snapshot,
        outage_count=status_count(status) if status is not None else None,
        at=status.read_at if status is not None else None,
    )
    poll_s, keepalive_s, max_s = _settings(request)
    prefix = broker.id_prefix(sid)

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
                yield event.sse(prefix)
            if not any(e.type in TERMINAL for e in replay):
                yield f"event: closed\ndata: {payload}\n\n"
            return
        # the last row read and when it was taken (monotonic)
        polled = Polled(None, None, {})
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
                    broker.observe(
                        sid,
                        polled.state,
                        polled.data,
                        outage_count=polled.outage_count,
                        at=polled.at,
                    )
                state, epoch = polled.state, polled.epoch
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
                yield event.sse(prefix)
                if event.type in TERMINAL:
                    return
            now = time.monotonic()
            if now >= next_poll:
                polled = await _poll(request, sid)
                polled_at = time.monotonic()
                state, epoch = polled.state, polled.epoch
                broker.observe(
                    sid,
                    state,
                    polled.data,
                    outage_count=polled.outage_count,
                    at=polled.at,
                )
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
