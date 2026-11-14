"""M1.8: the SSE route on a real database: committed / start_rolled_back / closed, replay."""

from __future__ import annotations

import asyncio
import json
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


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


async def _stream(w, sid, token: str, last: int | None = None) -> str:  # type: ignore[no-untyped-def]
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
        assert [int(e["id"]) for e in events[1:]] == [1, 2]
        assert events[2]["data"]["close_reason"] == "finished"
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
        stream = asyncio.create_task(_stream(w, s.session_id, s.session_token, last=2))
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
        assert [int(e["id"]) for e in events[1:]] == [3, 4, 5]


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
            await asyncio.wait_for(_stream(w, s.session_id, s.session_token, last=3), 5)
        )
        assert [e["event"] for e in events] == ["state", "closed"]
        assert int(events[1]["id"]) == 4


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


async def test_a_reload_streams_from_the_snapshot_cursor(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """Live 2026-11-14: a reloaded page opened the stream without a cursor and the ring's past
    outage, warning and reply events were applied as live. ``GET /sessions/{sid}`` returns
    ``event_cursor``; a stream from it replays none of them, and still gets what comes next."""
    from vektra_assess.api.events import broker_of

    async with world(pg, assess_sse_timing=TIMING) as w:
        s = await seed_session(w, worker, "ACTIVE")
        sid, auth = s.session_id, {"Authorization": f"Bearer {s.session_token}"}
        enter, end = _outage_helpers(pg, worker, sid)

        class R:
            app = w.app

        broker = broker_of(R())  # type: ignore[arg-type]
        # the first page: a reply, a warning, and an outage that started and ended
        tab = asyncio.create_task(_stream(w, sid, s.session_token))
        await asyncio.sleep(0.3)
        broker.publish(sid, "reply_delta", {"text": "pezzo"}, epoch=1)
        broker.publish(sid, "reply_done", {"turn_no": 3})
        broker.publish(sid, "warning", {"minutes": 10, "deadline_at": "x"})
        await enter()
        await asyncio.sleep(0.3)
        await end()
        await asyncio.sleep(0.3)
        # the reload: the snapshot (ACTIVE) and its cursor
        r = await w.client.get(f"/api/v1/assess/sessions/{sid}", headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "ACTIVE"
        cursor = r.json()["event_cursor"]
        assert cursor == broker.cursor(sid) and cursor >= 5
        reload = asyncio.create_task(_stream(w, sid, s.session_token, last=cursor))
        await asyncio.sleep(0.3)
        await enter()  # live, after the snapshot: delivered
        await asyncio.sleep(0.3)
        async with worker.begin() as conn:
            await conn.execute(
                text("SELECT assess_close_session(:s, 1, 'finished')"), {"s": sid}
            )
        first = [e["event"] for e in parse(await asyncio.wait_for(tab, 15))]
        assert first[:1] == ["state"] and "outage_end" in first and "warning" in first
        events = parse(await asyncio.wait_for(reload, 15))
        assert [e["event"] for e in events] == ["state", "outage_start", "closed"]
        assert all(int(e["id"]) > cursor for e in events[1:])
        assert events[1]["data"]["reason"] == "provider_unavailable"


async def test_an_outage_ended_and_reopened_between_polls_is_published(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """Review of #222, finding 1: OUTAGE observed, then the outage ends and another begins before
    any poll: the next observation (here the seed of a new connection, from the same status
    read as a poll) sees OUTAGE again, and the outage count tells the change."""
    from vektra_assess.api.events import broker_of

    async with world(pg, assess_sse_timing=TIMING) as w:
        s = await seed_session(w, worker, "ACTIVE")
        sid = s.session_id
        enter, end = _outage_helpers(pg, worker, sid)

        class R:
            app = w.app

        broker = broker_of(R())  # type: ignore[arg-type]
        tab = asyncio.create_task(_stream(w, sid, s.session_token))
        await asyncio.sleep(0.3)
        await enter()
        await asyncio.sleep(0.3)  # observed: OUTAGE, one interval
        tab.cancel()  # no connection polls while the next two changes happen
        await asyncio.sleep(0.1)
        seen = broker.cursor(sid)
        await end()
        await enter()
        reload = asyncio.create_task(_stream(w, sid, s.session_token, last=seen))
        await asyncio.sleep(0.3)
        async with worker.begin() as conn:
            await conn.execute(
                text("SELECT assess_close_session(:s, 1, 'finished')"), {"s": sid}
            )
        events = parse(await asyncio.wait_for(reload, 15))
        assert [e["event"] for e in events] == [
            "state",
            "outage_end",
            "outage_start",
            "closed",
        ]
        assert events[0]["data"]["state"] == "OUTAGE"
        assert events[0]["data"]["reason"] == "provider_unavailable"
