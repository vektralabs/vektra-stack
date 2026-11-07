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


async def test_an_internal_error_writes_the_interrupted_turn_and_the_next_turn_is_accepted(  # type: ignore[no-untyped-def]
    pg, worker
) -> None:
    """Review fix 5: an internal error at turn N appends the marked bot turn "[risposta
    interrotta]" through assess_append_turn, which clears the pending marker, so turn N+1 is
    accepted instead of a 409 forever. A provider OUTAGE keeps the turn instead (M4.1,
    test_session_outage)."""
    from vektra_assess.turn_markers import INTERRUPTED_REPLY

    runner = FakeStreamRunner(fail=RuntimeError("boom"))
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        url = f"/api/v1/assess/sessions/{s.session_id}"
        r = await w.client.post(
            f"{url}/turns", json={"text": "ciao"}, headers=bearer(s.session_token)
        )
        assert r.status_code == 202
        await _wait_idle(w)
        rows = await pg.run(
            "super",
            "SELECT turn_no, role, kind FROM assess_turns WHERE session_id=:s ORDER BY turn_no",
            {"s": s.session_id},
        )
        assert [tuple(x) for x in rows][2:] == [
            (3, "student", "normal"),
            (4, "bot", "interrupted"),
        ]
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_sessions WHERE id=:s AND pending_reply_since IS NULL",
                s=s.session_id,
            )
            == 1
        )
        got = (await w.client.get(url, headers=bearer(s.session_token))).json()
        assert got["turns"][3]["text"] == INTERRUPTED_REPLY and not got["reply_pending"]
        from vektra_assess.api.events import broker_of

        class R:
            app = w.app

        evs = broker_of(R()).after(s.session_id, 0)  # type: ignore[arg-type]
        assert [e.type for e in evs] == ["turn_accepted", "reply_interrupted"]
        assert evs[-1].data == {"reason": "internal_error", "turn_no": 4}
        # the provider is back: turn N+1 is accepted and answered
        runner.fail = None
        r = await w.client.post(
            f"{url}/turns", json={"text": "riprovo"}, headers=bearer(s.session_token)
        )
        assert r.status_code == 202 and r.json()["turn_no"] == 5
        await _wait_idle(w)
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_turns WHERE session_id=:s AND role='bot' AND kind='normal'",
                s=s.session_id,
            )
            == 1
        )


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


@pytest.mark.parametrize(
    ("text_", "reason"),
    [
        ("x" * 2001, "message_too_long"),
        ("   \n\t ", "message_empty"),
        ("", "string_too_short:text"),
    ],
)
async def test_an_over_length_or_blank_message_is_a_400_before_any_llm_call(  # type: ignore[no-untyped-def]
    pg, worker, text_: str, reason: str
) -> None:
    """Review fix 9: VEKTRA_ASSESS_MAX_MESSAGE_CHARS (provisional 2000, sized by M5.1)."""
    runner = FakeStreamRunner()
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        r = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/turns",
            json={"text": text_},
            headers=bearer(s.session_token),
        )
        assert r.status_code == 400, r.text
        assert (
            err(r)["code"] == "ERR-ASSESS-014" and err(r)["details"]["reason"] == reason
        )
        assert runner.turns == []
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_turns WHERE session_id=:s AND role='student'",
                s=s.session_id,
            )
            == 0
        )
        ok = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/turns",
            json={"text": "x" * 2000},
            headers=bearer(s.session_token),
        )
        assert ok.status_code == 202
        await _wait_idle(w)


async def _deadline_in(pg, sid, seconds: float, grace: int | None = None) -> None:  # type: ignore[no-untyped-def]
    sets = f"deadline_at = now() + make_interval(secs => {seconds})"
    if grace is not None:
        sets += f", reply_grace_s = {grace}"
    await pg.run(
        "super", f"UPDATE assess_sessions SET {sets} WHERE id = :s", {"s": sid}
    )


async def test_input_is_locked_from_the_deadline(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """M3.3: no student message is accepted from the deadline on (API and database)."""
    runner = FakeStreamRunner()
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        await _deadline_in(pg, s.session_id, -1)
        r = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/turns",
            json={"text": "troppo tardi"},
            headers=bearer(s.session_token),
        )
        # gate step 5 (M3.4): past the deadline with no reply pending the session is closed
        # lazily, timed_out, before the route runs; the turn is refused
        assert r.status_code == 409 and err(r)["code"] == "ERR-ASSESS-009"
        assert runner.turns == []
        row = (
            await pg.run(
                "super",
                "SELECT state, close_reason FROM assess_sessions WHERE id = :s",
                {"s": s.session_id},
            )
        )[0]
        assert tuple(row) == ("CLOSED", "timed_out")
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_turns WHERE session_id=:s AND role='student'",
                s=s.session_id,
            )
            == 0
        )


async def test_a_reply_in_flight_at_the_deadline_finishes_within_the_grace(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    runner = FakeStreamRunner(segments=("Uno. ", "Due. ", "Tre."), segment_delay_s=1.0)
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        await _deadline_in(pg, s.session_id, 1.5, grace=60)
        r = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/turns",
            json={"text": "ultima domanda"},
            headers=bearer(s.session_token),
        )
        assert r.status_code == 202
        await _wait_idle(w)  # finishes ~1.5 s after the deadline, inside the grace
        rows = await pg.run(
            "super",
            "SELECT role, kind FROM assess_turns WHERE session_id=:s ORDER BY turn_no DESC LIMIT 1",
            {"s": s.session_id},
        )
        assert tuple(rows[0]) == ("bot", "normal")


async def test_a_reply_past_the_grace_is_cut_and_marked(pg, worker) -> None:  # type: ignore[no-untyped-def]
    from vektra_assess.turn_markers import INTERRUPTED_REPLY

    runner = FakeStreamRunner(segments=("Uno. ", "Due. ", "Tre."), segment_delay_s=2.0)
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        await _deadline_in(
            pg, s.session_id, 1.0, grace=4
        )  # cut at deadline + 4 - 2 = ~3 s
        r = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/turns",
            json={"text": "ultima domanda"},
            headers=bearer(s.session_token),
        )
        assert r.status_code == 202
        await _wait_idle(w)
        # read before any further request: past the deadline the next one closes the session
        from vektra_assess.crypto import envelope

        dek = w.app.state.assess_dek_cache.get(s.session_id, 1)
        last = (
            await pg.run(
                "super",
                "SELECT turn_no, role, kind, ciphertext FROM assess_turns WHERE session_id = :s "
                "ORDER BY turn_no DESC LIMIT 1",
                {"s": s.session_id},
            )
        )[0]
        assert (last.role, last.kind) == ("bot", "interrupted")
        text_ = envelope.decrypt_turn(
            dek, s.session_id, last.turn_no, bytes(last.ciphertext)
        )
        assert text_.decode() == f"Uno. {INTERRUPTED_REPLY}"
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_sessions WHERE id=:s AND pending_reply_since IS NULL",
                s=s.session_id,
            )
            == 1
        )


async def test_the_database_enforces_the_input_lock_and_the_grace(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """M3.3 (DB test): student turns from the deadline on, and ordinary bot replies after
    deadline + grace, are refused by assess_append_turn itself; the marked interrupted turn is
    still accepted."""
    from integration.conftest import make_session

    sid = await make_session(pg)
    append = "SELECT assess_append_turn(:s, 1, :seq, :role, :kind, '\\x04', :h, :prev)"
    await pg.run(
        "super",
        "UPDATE assess_sessions SET deadline_at = now() - interval '1 second', reply_grace_s = 60 WHERE id = :s",
        {"s": sid},
    )
    assert (
        await pg.run(
            "api",
            append,
            {
                "s": sid,
                "seq": 0,
                "role": "student",
                "kind": "normal",
                "h": "h2",
                "prev": "h1",
            },
        )
    )[0][0] is None
    # a pending student turn written before the deadline: a bot reply inside the grace is accepted
    await pg.run(
        "super",
        "UPDATE assess_sessions SET deadline_at = now() + interval '1 hour' WHERE id = :s",
        {"s": sid},
    )
    assert (
        await pg.run(
            "api",
            append,
            {
                "s": sid,
                "seq": 0,
                "role": "student",
                "kind": "normal",
                "h": "h2",
                "prev": "h1",
            },
        )
    )[0][0] == 2
    await pg.run(
        "super",
        "UPDATE assess_sessions SET deadline_at = now() - interval '30 seconds' WHERE id = :s",
        {"s": sid},
    )
    assert (
        await pg.run(
            "api",
            append,
            {
                "s": sid,
                "seq": 1,
                "role": "bot",
                "kind": "normal",
                "h": "h3",
                "prev": "h2",
            },
        )
    )[0][0] == 3
    # past deadline + grace: an ordinary reply is refused, the marked turn is not (a second
    # student turn, written before the deadline, is pending: revision 0031 needs one)
    await pg.run(
        "super",
        "UPDATE assess_sessions SET deadline_at = now() + interval '1 hour' WHERE id = :s",
        {"s": sid},
    )
    assert (
        await pg.run(
            "api",
            append,
            {
                "s": sid,
                "seq": 2,
                "role": "student",
                "kind": "normal",
                "h": "h4",
                "prev": "h3",
            },
        )
    )[0][0] == 4
    await pg.run(
        "super",
        "UPDATE assess_sessions SET deadline_at = now() - interval '61 seconds' WHERE id = :s",
        {"s": sid},
    )
    assert (
        await pg.run(
            "api",
            append,
            {
                "s": sid,
                "seq": 3,
                "role": "bot",
                "kind": "normal",
                "h": "h5",
                "prev": "h4",
            },
        )
    )[0][0] is None
    assert (
        await pg.run(
            "worker",
            append,
            {
                "s": sid,
                "seq": 3,
                "role": "bot",
                "kind": "interrupted",
                "h": "h5",
                "prev": "h4",
            },
        )
    )[0][0] == 5


async def test_each_turn_hash_is_recorded_and_chained_at_persist(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """M5.8 / REQ-102: every persisted turn carries hash(prev, turn_no, role, kind, plaintext),
    chained on the previous turn's hash, so plan 10 can re-hash the transcript and detect any
    change."""
    from vektra_assess.crypto import envelope, hashing

    runner = FakeStreamRunner()
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        for text_ in ("primo", "secondo"):
            r = await w.client.post(
                f"/api/v1/assess/sessions/{s.session_id}/turns",
                json={"text": text_},
                headers=bearer(s.session_token),
            )
            assert r.status_code == 202
            await _wait_idle(w)
        dek = w.app.state.assess_dek_cache.get(s.session_id, 1)
        rows = await pg.run(
            "super",
            "SELECT turn_no, role, kind, ciphertext, turn_hash, prev_hash FROM assess_turns "
            "WHERE session_id = :s ORDER BY turn_no",
            {"s": s.session_id},
        )
        assert len(rows) == 6
        prev = rows[0].prev_hash
        for row in rows:
            assert row.prev_hash == prev
            plain = envelope.decrypt_turn(
                dek, s.session_id, row.turn_no, bytes(row.ciphertext)
            )
            assert row.turn_hash == hashing.turn_hash(
                prev, row.turn_no, row.role, row.kind, plain
            )
            prev = row.turn_hash


async def test_turn_notes_are_written_with_the_bot_turn(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """Plan 10: post-check marks, contest entries and the reply language of a bot turn land in
    ``assess_turn_notes`` in the turn's own transaction; a note-less turn still gets its row."""
    from vektra_assess.llm.types import ContestEntry

    runner = FakeStreamRunner(
        marks=("values", "values", "safeguard"),
        contests=(ContestEntry("C1", "proposed"),),
        language="en",
    )
    async with world(pg, assess_runner=runner, assess_sse_timing=TIMING) as w:
        s = await seed_session(w, worker, "ACTIVE")
        r = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/turns",
            json={"text": "Why did I lose a point on C1?"},
            headers=bearer(s.session_token),
        )
        assert r.status_code == 202, r.text
        await _wait_idle(w)
        rows = await pg.run(
            "super",
            "SELECT turn_no, marks, contests, lang FROM assess_turn_notes WHERE session_id = :s",
            {"s": s.session_id},
        )
        assert [tuple(r) for r in rows] == [
            (4, ["values", "safeguard"], [{"label": "C1", "status": "proposed"}], "en")
        ]
        # the turn itself is untouched (BR-015): its chain still verifies
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_turns WHERE session_id = :s AND marker_flags = 0",
                s=s.session_id,
            )
            == 4
        )
