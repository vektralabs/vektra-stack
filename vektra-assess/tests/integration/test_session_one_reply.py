"""Review fix 1: one bot turn per student turn, whatever races (API drivers, reconcile)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakeStreamRunner
from support.sessions import seed_session
from support.world import world

from integration.test_session_preflight import _reset
from vektra_assess.api.turns import TurnDriver

pytestmark = pytest.mark.integration


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def bot_turns(pg, sid) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    rows = await pg.run(
        "super",
        "SELECT role, kind FROM assess_turns WHERE session_id = :s AND turn_no > 2 "
        "ORDER BY turn_no",
        {"s": sid},
    )
    return [tuple(r) for r in rows]


async def test_two_drivers_on_one_turn_append_one_bot_turn(pg, worker) -> None:  # type: ignore[no-untyped-def]
    release = asyncio.Event()
    runner = FakeStreamRunner(release=release)
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        r = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/turns",
            json={"text": "uno"},
            headers=bearer(s.session_token),
        )
        assert r.status_code == 202
        first = w.app.state.assess_turn_driver
        for _ in range(100):  # the first driver holds the claim and waits in the runner
            if runner.turns:
                break
            await asyncio.sleep(0.02)
        # a second driver (another API process) on the same turn
        second = TurnDriver(
            engine=first.engine,
            broker=first.broker,
            runner=runner,
            handoff_private=first.handoff_private,
            cache=first.cache,
        )
        other = second.start(s.session_id, 1, None, runner.turns[0])  # type: ignore[arg-type]
        await asyncio.wait_for(other, 10)  # it does not wait for the claim: it leaves
        release.set()
        for task in list(first.tasks.values()):
            await asyncio.wait_for(task, 15)
        assert len(runner.turns) == 1, "the second driver never called the model"
        assert await bot_turns(pg, s.session_id) == [
            ("student", "normal"),
            ("bot", "normal"),
        ]


async def test_the_database_refuses_a_second_bot_turn(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """reconcile_reply's marker racing a finished reply (or any second append): refused."""
    runner = FakeStreamRunner()
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        r = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/turns",
            json={"text": "uno"},
            headers=bearer(s.session_token),
        )
        assert r.status_code == 202
        for task in list(w.app.state.assess_turn_driver.tasks.values()):
            await asyncio.wait_for(task, 15)
        last = (
            await pg.run(
                "super",
                "SELECT s.seq, t.turn_hash FROM assess_sessions s JOIN assess_turns t "
                "ON t.session_id = s.id WHERE s.id = :s ORDER BY t.turn_no DESC LIMIT 1",
                {"s": s.session_id},
            )
        )[0]
        for kind in ("interrupted", "normal"):
            got = await pg.run(
                "worker",
                "SELECT assess_append_turn(:s, 1, :q, 'bot', :k, '\\x05'::bytea, 'hx', :p)",
                {"s": s.session_id, "q": last.seq, "k": kind, "p": last.turn_hash},
            )
            assert got[0][0] is None, kind
        assert await bot_turns(pg, s.session_id) == [
            ("student", "normal"),
            ("bot", "normal"),
        ]


async def test_a_late_driver_never_generates_for_an_answered_turn(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit review 5808100754 (turns.py 292-296): a driver that gets the claim after the
    turn was answered checks the pending turn first and does not call the model."""
    runner = FakeStreamRunner()
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        r = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/turns",
            json={"text": "uno"},
            headers=bearer(s.session_token),
        )
        assert r.status_code == 202
        first = w.app.state.assess_turn_driver
        for task in list(first.tasks.values()):
            await asyncio.wait_for(task, 15)
        assert len(runner.turns) == 1
        late = TurnDriver(
            engine=first.engine,
            broker=first.broker,
            runner=runner,
            handoff_private=first.handoff_private,
            cache=first.cache,
        )
        # the turn it was given: the student turn appended at seq 3 (two opening turns before)
        task = late.start(s.session_id, 1, None, runner.turns[0], expected_seq=3)  # type: ignore[arg-type]
        await asyncio.wait_for(task, 10)
        assert len(runner.turns) == 1, "the late driver called the model"
