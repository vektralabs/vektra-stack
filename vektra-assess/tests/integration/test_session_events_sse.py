"""M1.8: the SSE route on a real database: committed / start_rolled_back / closed, replay."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakePartB, FakeSource, activate, deps, job_context
from support.sessions import seed_session
from support.world import world

from integration.test_session_preflight import _reset, start_source
from vektra_assess.context.capture.capture import Failed, FailReason
from vektra_assess.worker.preflight import make_preflight_handler

pytestmark = pytest.mark.integration

TIMING = {"poll_s": 0.05, "keepalive_s": 0.2, "max_s": 15}


def parse(body: str) -> list[dict[str, Any]]:
    out = []
    for block in body.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        fields: dict[str, Any] = {}
        for line in block.splitlines():
            key, _, value = line.partition(": ")
            fields[key] = value
        fields["data"] = json.loads(fields.get("data", "{}"))
        out.append(fields)
    return out


def _id(event: dict[str, Any]) -> int:
    """The number of an event id ``<broker nonce>:<n>``."""
    return int(str(event["id"]).rsplit(":", 1)[-1])


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


async def _stream(w, sid, token: str, last: int | str | None = None) -> str:  # type: ignore[no-untyped-def]
    headers = {"Authorization": f"Bearer {token}"}
    if last is not None:
        headers["Last-Event-ID"] = str(last)
    r = await w.client.get(f"/api/v1/assess/sessions/{sid}/events", headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    return r.text


async def test_starting_then_committed_then_closed(pg, worker) -> None:  # type: ignore[no-untyped-def]
    await activate(pg)
    async with world(pg, assess_sse_timing=TIMING) as w:
        s = await seed_session(w, worker, "STARTING")
        stream = asyncio.create_task(_stream(w, s.session_id, s.session_token))
        await asyncio.sleep(0.2)
        handler = make_preflight_handler(
            deps(w.settings, w.handoff_public, start_source(worker), FakePartB())
        )
        await handler(job_context(worker, s.session_id))
        await asyncio.sleep(0.3)
        async with worker.begin() as conn:
            await conn.execute(
                text("SELECT assess_close_session(:s, 1, 'finished')"),
                {"s": s.session_id},
            )
        events = parse(await asyncio.wait_for(stream, 15))
        types = [e["event"] for e in events]
        assert types == ["state", "committed", "closed"], types
        assert events[0]["data"]["state"] == "STARTING" and "id" not in events[0]
        assert [_id(e) for e in events[1:]] == [1, 2]
        assert events[2]["data"]["close_reason"] == "finished"
        # committed carries its read time: a replayed one is told from a live one by it
        assert datetime.fromisoformat(events[1]["data"]["at"])
        # no event payload carries opening-turn text (only reply_delta may carry text)
        body = json.dumps(events)
        assert FakePartB().text not in body and "Sessione" not in body


async def test_a_rolled_back_start_is_reported_with_its_reason(pg, worker) -> None:  # type: ignore[no-untyped-def]
    await activate(pg)
    async with world(pg, assess_sse_timing=TIMING) as w:
        s = await seed_session(w, worker, "STARTING")
        stream = asyncio.create_task(_stream(w, s.session_id, s.session_token))
        await asyncio.sleep(0.2)
        handler = make_preflight_handler(
            deps(
                w.settings,
                w.handoff_public,
                FakeSource(Failed(FailReason.ATTEMPT_UNREADABLE)),
            )
        )
        await handler(job_context(worker, s.session_id))
        events = parse(await asyncio.wait_for(stream, 15))
        assert [e["event"] for e in events] == ["state", "start_rolled_back"]
        assert events[1]["data"]["reason"] == "capture_failed_attempt_unreadable"


async def test_reconnect_replays_after_last_event_id(pg, worker) -> None:  # type: ignore[no-untyped-def]
    from vektra_assess.api.events import broker_of

    async with world(pg, assess_sse_timing=TIMING) as w:
        s = await seed_session(w, worker, "ACTIVE")

        class R:
            app = w.app

        broker = broker_of(R())  # type: ignore[arg-type]
        for i in range(3):
            broker.publish(s.session_id, "reply_delta", {"text": f"pezzo {i}"}, epoch=1)
        broker.publish(s.session_id, "reply_done", {"turn_no": 4})
        stream = asyncio.create_task(
            _stream(
                w,
                s.session_id,
                s.session_token,
                last=f"{broker.id_prefix(s.session_id)}:2",
            )
        )
        await asyncio.sleep(0.2)
        async with worker.begin() as conn:
            await conn.execute(
                text("SELECT assess_close_session(:s, 1, 'finished')"),
                {"s": s.session_id},
            )
        events = parse(await asyncio.wait_for(stream, 15))
        assert [e["event"] for e in events] == [
            "state",
            "reply_delta",
            "reply_done",
            "closed",
        ]
        assert [_id(e) for e in events[1:]] == [3, 4, 5]


async def test_events_need_a_session_token_in_the_header(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, assess_sse_timing=TIMING) as w:
        s = await seed_session(w, worker, "ACTIVE")
        r = await w.client.get(
            f"/api/v1/assess/sessions/{s.session_id}/events?token={s.session_token}"
        )
        assert r.status_code == 401
        pre = await w.presession(sub=s.sub, cmid=s.cmid)
        r = await w.client.get(
            f"/api/v1/assess/sessions/{s.session_id}/events",
            headers={"Authorization": f"Bearer {pre}"},
        )
        assert r.status_code == 401  # a pre-session token is not a session token


async def test_two_streams_both_receive_closed_and_a_reconnect_drains_it(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """Review fix 1: the stream that delivers ``closed`` must not evict the channel, or the other
    tab (whose cursor is past the re-created channel's ids) never sees it and polls for an hour."""
    from vektra_assess.api.events import broker_of

    async with world(pg, assess_sse_timing=TIMING) as w:
        s = await seed_session(w, worker, "ACTIVE")

        class R:
            app = w.app

        broker = broker_of(R())  # type: ignore[arg-type]
        for i in range(3):
            broker.publish(s.session_id, "reply_delta", {"text": f"pezzo {i}"}, epoch=1)
        tab1 = asyncio.create_task(_stream(w, s.session_id, s.session_token))
        tab2 = asyncio.create_task(_stream(w, s.session_id, s.session_token))
        await asyncio.sleep(0.3)
        async with worker.begin() as conn:
            await conn.execute(
                text("SELECT assess_close_session(:s, 1, 'finished')"),
                {"s": s.session_id},
            )
        bodies = await asyncio.wait_for(asyncio.gather(tab1, tab2), 5)
        for body in bodies:
            assert [e["event"] for e in parse(body)][-1] == "closed"
        # a tab reconnecting after the close with Last-Event-ID still drains ``closed``
        events = parse(
            await asyncio.wait_for(
                _stream(
                    w,
                    s.session_id,
                    s.session_token,
                    last=f"{broker.id_prefix(s.session_id)}:3",
                ),
                5,
            )
        )
        assert [e["event"] for e in events] == ["state", "closed"]
        assert _id(events[1]) == 4


async def test_a_reconnect_after_close_never_replays_reply_text(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4493120886: a CLOSED session's stream returns content-free events only."""
    from vektra_assess.api.events import broker_of

    async with world(pg, assess_sse_timing=TIMING) as w:
        s = await seed_session(w, worker, "ACTIVE")

        class R:
            app = w.app

        broker = broker_of(R())  # type: ignore[arg-type]
        broker.publish(
            s.session_id, "reply_delta", {"text": "CANARY-reply-after-close"}, epoch=1
        )
        broker.publish(s.session_id, "reply_done", {"turn_no": 4})
        async with worker.begin() as conn:
            await conn.execute(
                text("SELECT assess_close_session(:s, 1, 'finished')"),
                {"s": s.session_id},
            )
        body = await asyncio.wait_for(
            _stream(w, s.session_id, s.session_token, last=0), 5
        )
        assert "CANARY-reply-after-close" not in body
        assert [e["event"] for e in parse(body)] == ["state", "reply_done", "closed"]


async def test_the_one_minute_warning_reaches_both_tabs_once(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """M3.2: the warning is computed from the polled deadline and published once."""
    async with world(pg, assess_sse_timing=TIMING) as w:
        s = await seed_session(w, worker, "ACTIVE")
        await pg.run(
            "super",
            "UPDATE assess_sessions SET deadline_at = now() + interval '61 seconds' WHERE id = :s",
            {"s": s.session_id},
        )
        tabs = [
            asyncio.create_task(_stream(w, s.session_id, s.session_token))
            for _ in range(2)
        ]
        await asyncio.sleep(3)
        async with worker.begin() as conn:
            await conn.execute(
                text("SELECT assess_close_session(:s, 1, 'finished')"),
                {"s": s.session_id},
            )
        ids = set()
        for body in await asyncio.wait_for(asyncio.gather(*tabs), 10):
            warnings = [e for e in parse(body) if e["event"] == "warning"]
            assert [e["data"]["minutes"] for e in warnings] == [1]
            ids |= {e["id"] for e in warnings}
        assert len(ids) == 1, ids  # the same event in both tabs, published once


def _outage_helpers(pg, worker, sid):  # type: ignore[no-untyped-def]
    async def enter() -> None:
        got = await pg.run(
            "api", "SELECT assess_enter_outage(:s, 1, now(), 'error')", {"s": sid}
        )
        assert got[0][0] == "outage", got

    async def end() -> None:
        async with worker.begin() as conn:
            await conn.execute(text("SELECT assess_end_outage(:s, 0)"), {"s": sid})

    return enter, end


def _broker(w):  # type: ignore[no-untyped-def]
    from vektra_assess.api.events import broker_of

    class R:
        app = w.app

    return broker_of(R())  # type: ignore[arg-type]


async def _close(worker, sid) -> None:  # type: ignore[no-untyped-def]
    async with worker.begin() as conn:
        await conn.execute(
            text("SELECT assess_close_session(:s, 1, 'finished')"), {"s": sid}
        )


async def test_state_events_carry_the_read_time_against_the_snapshot(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """Live 2026-11-14: a reload replays the ring, and past outage events were applied as live.
    Outage events carry ``at`` (the database time of the read that saw the state); a past
    one's ``at`` is not after the reload's snapshot ``server_now``, a live one's is."""
    async with world(pg, assess_sse_timing=TIMING) as w:
        s = await seed_session(w, worker, "ACTIVE")
        sid, auth = s.session_id, {"Authorization": f"Bearer {s.session_token}"}
        enter, end = _outage_helpers(pg, worker, sid)
        tab = asyncio.create_task(_stream(w, sid, s.session_token))
        await asyncio.sleep(0.3)
        await enter()
        await asyncio.sleep(0.3)
        await end()
        await asyncio.sleep(0.3)
        snap = (
            await w.client.get(f"/api/v1/assess/sessions/{sid}", headers=auth)
        ).json()
        assert snap["state"] == "ACTIVE"
        await enter()  # after the snapshot
        await asyncio.sleep(0.3)
        await _close(worker, sid)
        events = [
            e
            for e in parse(await asyncio.wait_for(tab, 15))
            if e["event"] in ("outage_start", "outage_end")
        ]
        assert [e["event"] for e in events] == [
            "outage_start",
            "outage_end",
            "outage_start",
        ]
        server_now = datetime.fromisoformat(snap["server_now"])
        at = [datetime.fromisoformat(e["data"]["at"]) for e in events]
        assert at[0] <= at[1] <= server_now < at[2]
        # outage_end reports the deadline the credit moved
        assert events[1]["data"]["deadline_at"]


async def test_an_outage_ended_and_reopened_between_polls_is_published(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """Review of #222, finding 1: OUTAGE observed, then the outage ends and another begins before
    any poll: the next observation (here a new connection's seed, the same status read as a
    poll) sees OUTAGE again, and the outage count tells the change."""
    async with world(pg, assess_sse_timing=TIMING) as w:
        s = await seed_session(w, worker, "ACTIVE")
        sid = s.session_id
        enter, end = _outage_helpers(pg, worker, sid)
        broker = _broker(w)
        # what a poll saw: ACTIVE, then OUTAGE in the first interval; no poll runs after it
        broker.observe(sid, "ACTIVE", {}, outage_count=0)
        await enter()
        broker.observe(sid, "OUTAGE", {"state": "OUTAGE"}, outage_count=1)
        seen = f"{broker.id_prefix(sid)}:{len(broker.after(sid, 0))}"
        await end()
        await enter()
        reload = asyncio.create_task(_stream(w, sid, s.session_token, last=seen))
        await asyncio.sleep(0.3)
        frozen = (
            await w.client.get(
                f"/api/v1/assess/sessions/{sid}",
                headers={"Authorization": f"Bearer {s.session_token}"},
            )
        ).json()
        await _close(worker, sid)
        events = parse(await asyncio.wait_for(reload, 15))
        assert [e["event"] for e in events] == [
            "state",
            "outage_end",
            "outage_start",
            "closed",
        ]
        assert events[0]["data"]["reason"] == "provider_unavailable"
        assert events[1]["data"]["deadline_at"]
        # the opening state and the new outage_start carry the time left frozen at the open
        # outage's start, the same number GET /sessions/{sid} gives
        assert frozen["state"] == "OUTAGE"
        assert events[0]["data"]["remaining_s"] == frozen["remaining_s"]
        assert events[2]["data"]["remaining_s"] == frozen["remaining_s"]


@pytest.mark.parametrize("how", ["restart", "sweep", "bare"])
async def test_a_foreign_last_event_id_replays_the_ring(pg, worker, how) -> None:  # type: ignore[no-untyped-def]
    """Review of 8f9385b0, C: an id from before an API restart or a channel sweep, or a bare
    number, is foreign: the whole ring replays, nothing new is skipped."""
    from vektra_assess.api.events import EventBroker

    async with world(pg, assess_sse_timing=TIMING) as w:
        s = await seed_session(w, worker, "ACTIVE")
        sid = s.session_id
        old = _broker(w)
        for _ in range(20):
            old.publish(sid, "warning", {"minutes": 10, "deadline_at": "x"})
        last = f"{old.id_prefix(sid)}:20"
        if how == "restart":
            w.app.state.assess_events = EventBroker()
        elif how == "sweep":
            old.evict(sid)  # what sweep_idle does to an idle or ended channel
        else:
            last = "20"
        new = _broker(w)
        new.publish(sid, "turn_accepted", {"turn_no": 2})
        tab = asyncio.create_task(_stream(w, sid, s.session_token, last=last))
        await asyncio.sleep(0.3)
        await _close(worker, sid)
        events = parse(await asyncio.wait_for(tab, 15))
        types = [e["event"] for e in events]
        if how == "bare":  # the same channel: its whole ring
            assert types == ["state"] + ["warning"] * 20 + ["turn_accepted", "closed"]
        else:
            assert types == ["state", "turn_accepted", "closed"]
            assert events[1]["id"] == f"{new.id_prefix(sid)}:1"
