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
        small = create_async_engine(
            pg.url("api"), pool_size=2, max_overflow=0, pool_timeout=3
        )
        w.app.state.assess_engine = small
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
