"""M4.3: the API's ``PendingTurnDriver`` answers a pending turn with no client (REQ-086)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakeStreamRunner
from support.sessions import seed_session
from support.world import world

from integration.test_session_preflight import _reset
from vektra_assess.api.events import broker_of
from vektra_assess.api.pending import PendingTurnDriver
from vektra_assess.llm.types import ProviderCallRecord, ProviderOutageError
from vektra_assess.turn_markers import REPLY_CLAIM_KEY_SQL
from vektra_assess.worker.outage_probe import outage_tick

pytestmark = pytest.mark.integration


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _Req:
    def __init__(self, app) -> None:  # type: ignore[no-untyped-def]
        self.app = app


async def wait_idle(w) -> None:  # type: ignore[no-untyped-def]
    driver = getattr(w.app.state, "assess_turn_driver", None)
    if driver is not None:
        for task in list(driver.tasks.values()):
            await asyncio.wait_for(task, 15)


async def ok_probe() -> bool:
    return True


async def turns(pg, sid):  # type: ignore[no-untyped-def]
    return [
        tuple(r)
        for r in await pg.run(
            "super",
            "SELECT role, kind FROM assess_turns WHERE session_id = :s ORDER BY turn_no",
            {"s": sid},
        )
    ]


async def into_outage_with_a_pending_turn(pg, w, worker, runner):  # type: ignore[no-untyped-def]
    s = await seed_session(w, worker, "ACTIVE")
    now = datetime.now(UTC)
    runner.fail = ProviderOutageError(
        ProviderCallRecord(now, None, now, "first_token_timeout")
    )
    r = await w.client.post(
        f"/api/v1/assess/sessions/{s.session_id}/turns",
        json={"text": "la mia scelta era motivata"},
        headers=bearer(s.session_token),
    )
    assert r.status_code == 202, r.text
    await wait_idle(w)
    state = await pg.run(
        "super", "SELECT state FROM assess_sessions WHERE id = :s", {"s": s.session_id}
    )
    assert state[0][0] == "OUTAGE"
    return s


async def test_after_the_outage_the_pending_turn_is_answered_without_a_client(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    runner = FakeStreamRunner()
    async with world(pg, assess_runner=runner) as w:
        s = await into_outage_with_a_pending_turn(pg, w, worker, runner)
        sid = s.session_id
        runner.fail = None  # the provider is back
        await outage_tick(worker.connect, ok_probe, min_credit_s=20)
        pending = PendingTurnDriver(w.app, min_age_s=0)
        assert await pending.run_once() == [sid]
        await wait_idle(w)
        assert (await turns(pg, sid))[2:] == [("student", "normal"), ("bot", "normal")]
        row = await pg.run(
            "super",
            "SELECT state, pending_reply_since FROM assess_sessions WHERE id = :s",
            {"s": sid},
        )
        assert tuple(row[0]) == ("ACTIVE", None)
        # the runner was given the student's own pending turn, the history before it
        last = runner.turns[-1]
        assert last.text == "la mia scelta era motivata"
        assert last.turn_index == 1 and len(last.history) == 2
        types = [e.type for e in broker_of(_Req(w.app)).after(sid, 0)]  # type: ignore[arg-type]
        assert types[-3:] == ["reply_delta", "reply_delta", "reply_done"], types
        # nothing left to answer
        assert await pending.run_once() == []


async def test_a_claimed_or_fresh_turn_is_not_answered_twice(pg, worker) -> None:  # type: ignore[no-untyped-def]
    runner = FakeStreamRunner()
    async with world(pg, assess_runner=runner) as w:
        s = await into_outage_with_a_pending_turn(pg, w, worker, runner)
        sid = s.session_id
        runner.fail = None
        await outage_tick(worker.connect, ok_probe, min_credit_s=20)
        # fresh: the outage end restarted the pending clock, the default min age waits
        assert await PendingTurnDriver(w.app).run_once() == []
        # claimed by a live reply elsewhere (another API process): skipped
        holder = create_async_engine(pg.url("api"))
        try:
            async with holder.connect() as conn:
                await conn.execute(
                    text(f"SELECT pg_advisory_lock({REPLY_CLAIM_KEY_SQL})"),
                    {"sid": str(sid)},
                )
                await conn.commit()
                pending = PendingTurnDriver(w.app, min_age_s=0)
                assert await pending.run_once() == [sid]
                await wait_idle(w)
                assert (await turns(pg, sid))[-1] == ("student", "normal")
                calls = len(runner.turns)
                await conn.execute(
                    text(f"SELECT pg_advisory_unlock({REPLY_CLAIM_KEY_SQL})"),
                    {"sid": str(sid)},
                )
                await conn.commit()
        finally:
            await holder.dispose()
        assert len(runner.turns) == calls
        # released: answered once
        assert await PendingTurnDriver(w.app, min_age_s=0).run_once() == [sid]
        await wait_idle(w)
        assert (await turns(pg, sid))[-1] == ("bot", "normal")


async def test_the_loop_runs_until_stopped(pg, worker) -> None:  # type: ignore[no-untyped-def]
    runner = FakeStreamRunner()
    async with world(pg, assess_runner=runner) as w:
        s = await into_outage_with_a_pending_turn(pg, w, worker, runner)
        runner.fail = None
        await outage_tick(worker.connect, ok_probe, min_credit_s=20)
        pending = PendingTurnDriver(w.app, interval_s=0.05, min_age_s=0)
        pending.start()
        try:
            for _ in range(100):
                if (await turns(pg, s.session_id))[-1] == ("bot", "normal"):
                    break
                await asyncio.sleep(0.05)
            assert (await turns(pg, s.session_id))[-1] == ("bot", "normal")
        finally:
            await pending.stop()
        await wait_idle(w)
