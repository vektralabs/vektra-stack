"""Review fix 2: concurrent replies beyond the API pool never stall it.

Each reply holds its claim connection for its whole life and needs a second one to persist; with
more concurrent replies than the pool can serve twice, every claim held a connection and every
persist waited for one. Replies now take a slot first (fewer slots than half the pool, with a
margin for the other requests); the extra ones wait for a slot, then complete.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakeStreamRunner
from support.sessions import seed_session
from support.world import world

from integration.test_session_preflight import _reset

pytestmark = pytest.mark.integration

#: (assignment, student) pairs with a recorded, gradeable submission
TARGETS = [("rubric_vis", "stu1"), ("simple", "stu1"), ("scale", "stu1")]


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


async def test_replies_beyond_the_pool_all_complete(pg, worker) -> None:  # type: ignore[no-untyped-def]
    runner = FakeStreamRunner(segment_delay_s=0.3)
    async with world(pg, assess_runner=runner) as w:
        seeded = [
            await seed_session(w, worker, "ACTIVE", assignment=a, user=u)
            for a, u in TARGETS
        ]
        # a small API pool: 2 connections, no overflow, a short checkout timeout
        # (bound and watched by the binding guard like the world's own engine)
        small = w.use_api_engine(
            create_async_engine(
                pg.url("api"), pool_size=2, max_overflow=0, pool_timeout=3
            )
        )
        w.app.state.assess_turn_driver = None
        try:
            for s in seeded:
                r = await w.client.post(
                    f"/api/v1/assess/sessions/{s.session_id}/turns",
                    json={"text": "uno"},
                    headers={"Authorization": f"Bearer {s.session_token}"},
                )
                assert r.status_code == 202, r.text
            driver = w.app.state.assess_turn_driver
            await asyncio.wait_for(
                asyncio.gather(*list(driver.tasks.values())), timeout=30
            )
            for s in seeded:
                last = await pg.run(
                    "super",
                    "SELECT role, kind FROM assess_turns WHERE session_id = :s "
                    "ORDER BY turn_no DESC LIMIT 1",
                    {"s": s.session_id},
                )
                assert tuple(last[0]) == ("bot", "normal"), s.session_id
        finally:
            await small.dispose()


async def test_the_cut_stays_at_the_deadline_however_long_the_reply_waited(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit review 5808100754 (turns.py 310-313): the stop for deadline + grace is an
    absolute time; a reply that waited for a slot is cut at the same moment, not later."""
    from vektra_assess.api.events import broker_of
    from vektra_assess.api.turns import driver_for_app

    runner = FakeStreamRunner(
        segments=tuple(f"Frase {i}. " for i in range(10)), segment_delay_s=0.3
    )
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        # Every time below is derived from the deadline the test sets (CodeRabbit 4499657940),
        # never from how long the POST takes: deadline D = 5 s ahead (room for the POST
        # before the input lock), grace G = 6 s, persist margin min(2, G / 2) = 2 s, so the
        # cut is at D + G - 2 = 9 s after the UPDATE. The slots are released RELEASE_BEFORE_CUT
        # before that cut: the reply streams for that long (one segment each 0.3 s), then is
        # cut. A cut computed from the start of the reply instead would let all 10 through.
        deadline_s, grace_s, margin_s = 5.0, 6, 2.0
        release_before_cut_s, segment_s = 1.2, 0.3
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await pg.run(
            "super",
            "UPDATE assess_sessions SET deadline_at = now() + make_interval(secs => :d), "
            "reply_grace_s = :g WHERE id = :s",
            {"s": s.session_id, "d": deadline_s, "g": grace_s},
        )
        cut_at = t0 + deadline_s + grace_s - margin_s
        driver = driver_for_app(w.app)
        n_slots = driver.reply_slots()
        for _ in range(n_slots):  # every reply slot busy until shortly before the cut
            await driver.slots.acquire()
        r = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/turns",
            json={"text": "x"},
            headers={"Authorization": f"Bearer {s.session_token}"},
        )
        assert r.status_code == 202, r.text
        await asyncio.sleep(max(0.0, cut_at - release_before_cut_s - loop.time()))
        for _ in range(n_slots):
            driver.slots.release()
        await asyncio.wait_for(asyncio.gather(*list(driver.tasks.values())), 15)

        class _R:
            app = w.app

        deltas = [
            e for e in broker_of(_R()).after(s.session_id, 0) if e.type == "reply_delta"
        ]
        # the reply streamed until the cut (1.2 s: about 4 segments, at least one even on a
        # slow runner) and was cut there, far before its 10 segments
        most = int(release_before_cut_s / segment_s) + 1
        assert 1 <= len(deltas) <= most, (len(deltas), most)
        last = await pg.run(
            "super",
            "SELECT role, kind FROM assess_turns WHERE session_id = :s "
            "ORDER BY turn_no DESC LIMIT 1",
            {"s": s.session_id},
        )
        assert tuple(last[0]) == ("bot", "interrupted")
