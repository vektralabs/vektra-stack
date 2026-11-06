"""M3.5: the sweeper closes abandoned sessions timed_out, and never an OUTAGE one."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.sessions import seed_session
from support.world import world

from integration.test_session_preflight import _reset
from vektra_assess.worker.sweeper import sweep_timed_out

pytestmark = pytest.mark.integration


async def test_a_browser_closed_session_is_closed_with_its_transcript_job(pg) -> None:  # type: ignore[no-untyped-def]
    await _reset(pg)
    worker = create_async_engine(pg.url("worker"))
    try:
        async with world(pg) as w:
            gone = await seed_session(w, worker, "ACTIVE", assignment="rubric_vis")
            out = await seed_session(w, worker, "OUTAGE", assignment="simple")
            inside = await seed_session(w, worker, "ACTIVE", assignment="scale")
            # nobody is connected: the deadline + grace (60 s) passed 1 s ago for two of them
            for sid in (gone.session_id, out.session_id):
                await pg.run(
                    "super",
                    "UPDATE assess_sessions SET deadline_at = now() - interval '61 seconds', "
                    "reply_grace_s = 60 WHERE id = :s",
                    {"s": sid},
                )
            # inside the grace: not yet
            await pg.run(
                "super",
                "UPDATE assess_sessions SET deadline_at = now() - interval '30 seconds' WHERE id = :s",
                {"s": inside.session_id},
            )
            assert await sweep_timed_out(worker.connect) == 1
            assert await sweep_timed_out(worker.connect) == 0  # idempotent
            rows = {
                r.id: (r.state, r.close_reason, r.epoch, r.jobs)
                for r in await pg.run(
                    "super",
                    "SELECT s.id, s.state, s.close_reason, s.epoch, (SELECT count(*) FROM assess_jobs j "
                    "WHERE j.kind = 'transcript_generate' AND j.args->>'session_id' = s.id::text) AS jobs "
                    "FROM assess_sessions s WHERE s.id IN (:a, :b, :c)",
                    {"a": gone.session_id, "b": out.session_id, "c": inside.session_id},
                )
            }
            assert rows[gone.session_id] == ("CLOSED", "timed_out", 2, 1)
            assert (
                rows[out.session_id][0] == "OUTAGE"
            )  # the sweeper never closes OUTAGE
            assert rows[inside.session_id][0] == "ACTIVE"
    finally:
        await worker.dispose()


async def test_a_session_that_entered_outage_after_selection_is_not_timed_out(  # type: ignore[no-untyped-def]
    pg, monkeypatch
) -> None:
    """CodeRabbit 4498473601: the timeout is re-checked under the row lock; a session that
    went into OUTAGE (or got a later deadline) after the scan is left alone."""
    from sqlalchemy import text

    from vektra_assess.worker import sweeper as sweeper_mod

    await _reset(pg)
    worker = create_async_engine(pg.url("worker"))
    try:
        async with world(pg) as w:
            s = await seed_session(w, worker, "OUTAGE", assignment="simple")
            await pg.run(
                "super",
                "UPDATE assess_sessions SET deadline_at = now() - interval '61 seconds', "
                "reply_grace_s = 60 WHERE id = :s",
                {"s": s.session_id},
            )
            # the scan saw it ACTIVE a moment ago
            monkeypatch.setattr(
                sweeper_mod,
                "_DUE",
                text(
                    "SELECT id, epoch FROM assess_sessions WHERE id = :sid LIMIT :n"
                ).bindparams(sid=s.session_id),
            )
            assert await sweep_timed_out(worker.connect) == 0
            state = await pg.run(
                "super",
                "SELECT state FROM assess_sessions WHERE id = :s",
                {"s": s.session_id},
            )
            assert state[0][0] == "OUTAGE"
    finally:
        await worker.dispose()
