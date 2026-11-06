"""REQ-067: consumed == exists(persisted opening bot turn), checked on every session row.

The ``pg`` fixture runs ``CONSUMED_INVARIANT`` after the last test of EVERY integration module, so
the check covers the rows the whole suite leaves behind. This module drives every lifecycle exit
on one database (commit, rollback, close, outage, a Start never picked up) and runs the same
check, then shows the check bites on a row it must reject.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.sessions import seed_session
from support.world import world

from integration.conftest import CONSUMED_INVARIANT
from integration.test_session_preflight import _reset
from vektra_assess.worker.preflight import rollback_start

pytestmark = pytest.mark.integration


async def test_every_lifecycle_exit_keeps_consumed_equal_to_an_opening_turn(pg) -> None:  # type: ignore[no-untyped-def]
    await _reset(pg)
    worker = create_async_engine(pg.url("worker"))
    try:
        async with world(pg) as w:
            await seed_session(w, worker, "ACTIVE", assignment="rubric_vis")
            await seed_session(w, worker, "OUTAGE", assignment="simple")
            await seed_session(w, worker, "CLOSED", assignment="scale")
            await seed_session(w, worker, "STARTING", assignment="files")
            rb = await seed_session(w, worker, "STARTING", assignment="guide")
            async with worker.begin() as conn:
                assert await rollback_start(conn, rb.session_id, "internal_error")
        states = {
            r[0]
            for r in await pg.run("super", "SELECT DISTINCT state FROM assess_sessions")
        }
        assert {"ACTIVE", "OUTAGE", "CLOSED", "STARTING"} <= states
        assert await pg.run("super", CONSUMED_INVARIANT) == []
        # the check bites: a committed-looking row with no opening turn is reported (the
        # deferred constraint forbids writing one, so it is built inside a rolled-back
        # transaction with the constraint checked at the end, never committed)
        async with pg.conn("super") as conn:
            from sqlalchemy import text

            await conn.execute(text("SET CONSTRAINTS ALL DEFERRED"))
            await conn.execute(
                text(
                    "UPDATE assess_sessions SET state = 'ACTIVE' WHERE id = "
                    "(SELECT id FROM assess_sessions WHERE state = 'STARTING' LIMIT 1)"
                )
            )
            bad = (await conn.execute(text(CONSUMED_INVARIANT))).fetchall()
            await conn.rollback()
        assert len(bad) == 1
    finally:
        await worker.dispose()
