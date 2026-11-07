"""M3.8: "Fine" closes finished, once."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.sessions import seed_session
from support.world import world

from integration.test_session_preflight import _reset

pytestmark = pytest.mark.integration


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("state", ["ACTIVE", "OUTAGE"])
async def test_fine_twice_is_one_close_and_one_transcript_job(pg, state: str) -> None:  # type: ignore[no-untyped-def]
    await _reset(pg)
    worker = create_async_engine(pg.url("worker"))
    try:
        async with world(pg) as w:
            s = await seed_session(w, worker, state)  # type: ignore[arg-type]
            url = f"/api/v1/assess/sessions/{s.session_id}/finish"
            # two tabs press Fine at once, then one presses it again
            first = await asyncio.gather(
                w.client.post(url, headers=bearer(s.session_token)),
                w.client.post(url, headers=bearer(s.session_token)),
            )
            again = await w.client.post(url, headers=bearer(s.session_token))
            for r in (*first, again):
                assert r.status_code == 200, r.text
                assert r.json() == {
                    "session_id": str(s.session_id),
                    "state": "CLOSED",
                    "close_reason": "finished",
                }
            row = (
                await pg.run(
                    "super",
                    "SELECT state, close_reason, epoch, (SELECT count(*) FROM assess_jobs WHERE "
                    "kind='transcript_generate' AND args->>'session_id' = :t), (SELECT count(*) "
                    "FROM assess_audit_events WHERE session_id = :s AND event = 'assess_close'), "
                    "(SELECT count(*) FROM assess_session_handoff WHERE session_id = :s) "
                    "FROM assess_sessions WHERE id = :s",
                    {"s": s.session_id, "t": str(s.session_id)},
                )
            )[0]
            assert tuple(row) == ("CLOSED", "finished", 2, 1, 1, 0)
    finally:
        await worker.dispose()


async def test_fine_during_a_slow_reply_stops_the_model(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """Review fix 7: a close stops the reply in flight, so the model stops generating."""
    import time

    from support.preflight import FakeStreamRunner

    runner = FakeStreamRunner(
        segments=tuple(f"Frase {i}. " for i in range(20)), segment_delay_s=0.3
    )
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        url = f"/api/v1/assess/sessions/{s.session_id}"
        auth = {"Authorization": f"Bearer {s.session_token}"}
        assert (
            await w.client.post(f"{url}/turns", json={"text": "x"}, headers=auth)
        ).status_code == 202
        await asyncio.sleep(0.5)
        task = w.app.state.assess_turn_driver.tasks[s.session_id]
        assert (await w.client.post(f"{url}/finish", headers=auth)).status_code == 200
        t0 = time.monotonic()
        await asyncio.wait({task}, timeout=5)
        assert task.done() and time.monotonic() - t0 < 1.0, "the reply kept generating"
