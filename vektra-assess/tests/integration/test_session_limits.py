"""M5.2: REQ-083 pre-LLM rejections and the session token budget, on a real database."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakeStreamRunner
from support.sessions import seed_session
from support.world import default_settings, err, world

from integration.test_session_preflight import _reset
from vektra_assess.api.events import broker_of

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


async def deadline_of(pg, sid):  # type: ignore[no-untyped-def]
    return (
        await pg.run(
            "super", "SELECT deadline_at FROM assess_sessions WHERE id = :s", {"s": sid}
        )
    )[0][0]


async def test_over_length_and_blank_are_refused_before_any_model_call(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    runner = FakeStreamRunner()
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        before = await deadline_of(pg, s.session_id)
        url = f"/api/v1/assess/sessions/{s.session_id}/turns"
        for body, reason in (
            ("x" * (w.settings.max_message_chars + 1), "message_too_long"),
            ("   \n\t", "message_empty"),
        ):
            r = await w.client.post(
                url, json={"text": body}, headers=bearer(s.session_token)
            )
            assert r.status_code == 400 and err(r)["details"]["reason"] == reason
        assert runner.turns == []  # 0 model calls
        assert await deadline_of(pg, s.session_id) == before


async def test_messages_per_minute_answers_wait_a_few_seconds(pg, worker) -> None:  # type: ignore[no-untyped-def]
    runner = FakeStreamRunner()
    async with world(
        pg, default_settings(VEKTRA_ASSESS_MESSAGES_PER_MIN=1), assess_runner=runner
    ) as w:
        s = await seed_session(w, worker, "ACTIVE")
        before = await deadline_of(pg, s.session_id)
        url = f"/api/v1/assess/sessions/{s.session_id}/turns"
        r = await w.client.post(
            url, json={"text": "uno"}, headers=bearer(s.session_token)
        )
        assert r.status_code == 202
        await wait_idle(w)
        r = await w.client.post(
            url, json={"text": "due"}, headers=bearer(s.session_token)
        )
        assert r.status_code == 429
        e = err(r)
        assert (
            e["code"] == "ERR-ASSESS-012" and e["message"] == "Attendi qualche secondo."
        )
        assert e["details"] == {"reason": "message_rate"}
        assert len(runner.turns) == 1  # the refused message reached no model
        assert await deadline_of(pg, s.session_id) == before


async def test_budget_warning_at_80_and_budget_exhausted_at_100(pg, worker) -> None:  # type: ignore[no-untyped-def]
    runner = FakeStreamRunner()  # each answered turn: 200 + 20 estimated tokens
    async with world(
        pg,
        default_settings(VEKTRA_ASSESS_SESSION_TOKEN_BUDGET=10_000),
        assess_runner=runner,
    ) as w:
        s = await seed_session(w, worker, "ACTIVE")
        sid = s.session_id
        broker = broker_of(_Req(w.app))  # type: ignore[arg-type]
        broker.observe(sid, "ACTIVE", {})
        url = f"/api/v1/assess/sessions/{sid}/turns"

        async def turn(start_at: int) -> None:
            await pg.run(
                "super",
                "UPDATE assess_sessions SET tokens_used = :n WHERE id = :s",
                {"s": sid, "n": start_at},
            )
            r = await w.client.post(
                url, json={"text": "ok"}, headers=bearer(s.session_token)
            )
            assert r.status_code == 202, r.text
            await wait_idle(w)

        await turn(7_700)  # 7_920 < 8_000: nothing yet
        await turn(7_900)  # crosses 80%
        await turn(8_500)  # above 80% already: no second warning
        warnings = [e for e in broker.after(sid, 0) if e.type == "budget_warning"]
        assert [e.data for e in warnings] == [{"percent": 80}]
        audit = await pg.run(
            "super",
            "SELECT count(*) FROM assess_audit_events WHERE session_id = :s "
            "AND event = 'assess_budget_warning'",
            {"s": sid},
        )
        assert audit[0][0] == 1
        await turn(9_900)  # crosses 100%: closed, once
        row = await pg.run(
            "super",
            "SELECT state, close_reason, tokens_used, (SELECT count(*) FROM assess_jobs j "
            "WHERE j.kind = 'transcript_generate' AND j.args->>'session_id' = :t) "
            "FROM assess_sessions WHERE id = :s",
            {"s": sid, "t": str(sid)},
        )
        assert tuple(row[0]) == ("CLOSED", "budget_exhausted", 10_120, 1)
        closed = [e for e in broker.after(sid, 0) if e.type == "closed"]
        assert len(closed) == 1
        assert closed[0].data["message_code"] == "closed_budget_exhausted"


async def test_failed_calls_add_nothing_to_the_budget(pg, worker) -> None:  # type: ignore[no-untyped-def]
    runner = FakeStreamRunner(fail=RuntimeError("boom"))
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        r = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/turns",
            json={"text": "x"},
            headers=bearer(s.session_token),
        )
        assert r.status_code == 202
        await wait_idle(w)
        used = await pg.run(
            "super",
            "SELECT tokens_used FROM assess_sessions WHERE id = :s",
            {"s": s.session_id},
        )
        assert used[0][0] == 0


async def test_a_message_refused_as_pending_does_not_use_the_rate(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """Review fix 4: 409 reply_pending is decided before the messages-per-minute limiter."""
    release = asyncio.Event()
    runner = FakeStreamRunner(release=release)
    async with world(
        pg, default_settings(VEKTRA_ASSESS_MESSAGES_PER_MIN=2), assess_runner=runner
    ) as w:
        s = await seed_session(w, worker, "ACTIVE")
        url = f"/api/v1/assess/sessions/{s.session_id}/turns"
        auth = bearer(s.session_token)
        assert (
            await w.client.post(url, json={"text": "uno"}, headers=auth)
        ).status_code == 202
        for _ in range(3):
            r = await w.client.post(url, json={"text": "ancora"}, headers=auth)
            assert r.status_code == 409 and err(r)["code"] == "ERR-ASSESS-010", r.text
        release.set()
        await wait_idle(w)
        r = await w.client.post(url, json={"text": "due"}, headers=auth)
        assert r.status_code == 202, r.text
