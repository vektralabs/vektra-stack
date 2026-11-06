"""M1.9: ``POST /sessions/{sid}/turns`` and the detached ``TurnDriver`` on a real database."""

from __future__ import annotations

import asyncio
import itertools
import json

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakeStreamRunner
from support.sessions import seed_session
from support.world import err, world

from integration.test_session_events_sse import TIMING, parse
from integration.test_session_preflight import _reset
from vektra_assess.llm.types import ProviderCallRecord, ProviderOutageError

pytestmark = pytest.mark.integration


async def n(pg, sql: str, **params) -> int:  # type: ignore[no-untyped-def]
    return int((await pg.run("super", sql, params))[0][0])


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _wait_idle(w) -> None:  # type: ignore[no-untyped-def]
    driver = getattr(w.app.state, "assess_turn_driver", None)
    if driver is not None:
        for task in list(driver.tasks.values()):
            await asyncio.wait_for(task, 15)


async def test_a_turn_is_answered_streamed_and_persisted(pg, worker) -> None:  # type: ignore[no-untyped-def]
    runner = FakeStreamRunner()
    async with world(pg, assess_runner=runner, assess_sse_timing=TIMING) as w:
        s = await seed_session(w, worker, "ACTIVE")
        url = f"/api/v1/assess/sessions/{s.session_id}"
        r = await w.client.post(
            f"{url}/turns",
            json={"text": "Ho scelto quella struttura perche'..."},
            headers=bearer(s.session_token),
        )
        assert r.status_code == 202, r.text
        assert r.json()["turn_no"] == 3
        await _wait_idle(w)
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_turns WHERE session_id = :s",
                s=s.session_id,
            )
            == 4
        )
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_sessions WHERE id = :s AND pending_reply_since IS NULL AND seq = 4",
                s=s.session_id,
            )
            == 1
        )
        got = (await w.client.get(url, headers=bearer(s.session_token))).json()
        assert [(t["role"], t["kind"]) for t in got["turns"]] == [
            ("bot", "opening"),
            ("bot", "opening"),
            ("student", "normal"),
            ("bot", "normal"),
        ]
        assert got["turns"][3]["text"] == "".join(runner.segments)
        assert not got["reply_pending"]
        # the runner got the frozen bundle and the history of what the student saw
        turn = runner.turns[0]
        assert turn.turn_index == 1 and [p.role for p in turn.history] == [
            "assistant",
            "assistant",
        ]
        # the events: accepted, the released segments, done (replayed from the ring)
        stream = asyncio.create_task(
            w.client.get(f"{url}/events", headers=bearer(s.session_token))
        )
        await asyncio.sleep(0.2)
        from sqlalchemy import text

        async with worker.begin() as conn:
            await conn.execute(
                text("SELECT assess_close_session(:s, 1, 'finished')"),
                {"s": s.session_id},
            )
        events = parse((await asyncio.wait_for(stream, 15)).text)
        types = [e["event"] for e in events]
        assert types[:5] == [
            "state",
            "turn_accepted",
            "reply_delta",
            "reply_delta",
            "reply_done",
        ], types
        assert events[4]["data"]["turn_no"] == 4
        # the hash chain of the whole session verifies (student and bot turns appended by the API)
        rows = await pg.run(
            "super",
            "SELECT turn_no, turn_hash, prev_hash FROM assess_turns WHERE session_id = :s ORDER BY turn_no",
            {"s": s.session_id},
        )
        for prev, cur in itertools.pairwise(rows):
            assert cur.prev_hash == prev.turn_hash


async def test_a_second_send_while_pending_is_409_and_adds_nothing(pg, worker) -> None:  # type: ignore[no-untyped-def]
    release = asyncio.Event()
    runner = FakeStreamRunner(release=release)
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        url = f"/api/v1/assess/sessions/{s.session_id}/turns"
        r1 = await w.client.post(
            url, json={"text": "uno"}, headers=bearer(s.session_token)
        )
        assert r1.status_code == 202
        # second tab, resumed token, same owner: serialized, not revoked
        pre = await w.presession(sub=s.sub, cmid=s.cmid)
        tab2 = (
            await w.client.get(
                f"/api/v1/assess/sessions/{s.session_id}", headers=bearer(pre)
            )
        ).json()
        assert tab2["reply_pending"] is True
        results = await asyncio.gather(
            w.client.post(url, json={"text": "due"}, headers=bearer(s.session_token)),
            w.client.post(url, json={"text": "tre"}, headers=bearer(tab2["token"])),
        )
        for r in results:
            assert r.status_code == 409 and err(r)["code"] == "ERR-ASSESS-010"
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_turns WHERE session_id=:s AND role='student'",
                s=s.session_id,
            )
            == 1
        )
        release.set()
        await _wait_idle(w)
        r = await w.client.post(
            url, json={"text": "quattro"}, headers=bearer(tab2["token"])
        )
        assert r.status_code == 202 and r.json()["turn_no"] == 5
        await _wait_idle(w)


async def test_the_reply_survives_the_client_going_away(pg, worker) -> None:  # type: ignore[no-untyped-def]
    release = asyncio.Event()
    runner = FakeStreamRunner(release=release)
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        r = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/turns",
            json={"text": "ciao"},
            headers=bearer(s.session_token),
        )
        assert r.status_code == 202
    # the client and its connection are gone; the detached driver still answers
    release.set()
    driver = w.app.state.assess_turn_driver
    for task in list(driver.tasks.values()):
        await asyncio.wait_for(task, 15)
    assert (
        await n(
            pg,
            "SELECT count(*) FROM assess_turns WHERE session_id=:s AND role='bot' AND kind='normal'",
            s=s.session_id,
        )
        == 1
    )


async def test_outage_on_the_thin_path_interrupts_and_keeps_the_student_turn(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    rec = ProviderCallRecord(now, None, now, "first_token_timeout")
    runner = FakeStreamRunner(fail=ProviderOutageError(rec))
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        r = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/turns",
            json={"text": "ciao"},
            headers=bearer(s.session_token),
        )
        assert r.status_code == 202
        await _wait_idle(w)
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_turns WHERE session_id=:s",
                s=s.session_id,
            )
            == 3
        )
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_sessions WHERE id=:s AND pending_reply_since IS NOT NULL",
                s=s.session_id,
            )
            == 1
        )
        from vektra_assess.api.events import broker_of

        class R:
            app = w.app

        evs = [e.type for e in broker_of(R()).after(s.session_id, 0)]  # type: ignore[arg-type]
        assert evs == ["turn_accepted", "reply_interrupted"]


@pytest.mark.parametrize("state", ["STARTING", "OUTAGE"])
async def test_turns_need_an_active_session(pg, worker, state: str) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, assess_runner=FakeStreamRunner()) as w:
        s = await seed_session(w, worker, state)  # type: ignore[arg-type]
        r = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/turns",
            json={"text": "x"},
            headers=bearer(s.session_token),
        )
        assert r.status_code == 409 and err(r)["code"] == "ERR-ASSESS-011"
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_turns WHERE session_id=:s AND role='student'",
                s=s.session_id,
            )
            == 0
        )


async def test_no_plaintext_reaches_the_database(pg, worker) -> None:  # type: ignore[no-untyped-def]
    canary = "CANARY-turn-text-5f1"
    async with world(
        pg, assess_runner=FakeStreamRunner(segments=(f"{canary} risposta",))
    ) as w:
        s = await seed_session(w, worker, "ACTIVE")
        r = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/turns",
            json={"text": f"{canary} domanda"},
            headers=bearer(s.session_token),
        )
        assert r.status_code == 202
        await _wait_idle(w)
    from integration.conftest import dump_data

    assert canary not in dump_data(pg)
    assert canary not in json.dumps(
        [
            list(r)
            for r in await pg.run(
                "super",
                "SELECT event, reason_code, data::text FROM assess_audit_events",
            )
        ]
    )


@pytest.mark.parametrize("failure", ["material", "db"])
async def test_a_failing_persist_is_reported_and_never_kills_the_task(  # type: ignore[no-untyped-def]
    pg, worker, monkeypatch, failure: str
) -> None:
    """Review fix 2: an error while persisting the bot turn publishes ``reply_interrupted``, logs
    content-free, and the driver task ends without an unread exception."""
    from vektra_assess.api import turns as turns_mod
    from vektra_assess.api.events import broker_of
    from vektra_assess.api.material import MaterialUnavailable

    release = asyncio.Event()
    runner = FakeStreamRunner(release=release)
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        r = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/turns",
            json={"text": "ciao"},
            headers=bearer(s.session_token),
        )
        assert r.status_code == 202
        exc = MaterialUnavailable() if failure == "material" else OSError("db gone")

        async def broken(*_a, **_k):  # type: ignore[no-untyped-def]
            raise exc

        monkeypatch.setattr(turns_mod, "load_material", broken)
        driver = w.app.state.assess_turn_driver
        task = driver.tasks[s.session_id]
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 15)
        assert task.exception() is None

        class R:
            app = w.app

        evs = [e.type for e in broker_of(R()).after(s.session_id, 0)]  # type: ignore[arg-type]
        assert evs[-1] == "reply_interrupted", evs
