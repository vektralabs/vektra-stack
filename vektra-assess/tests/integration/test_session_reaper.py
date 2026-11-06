"""M1.6: the STARTING reaper on a real database, with a pre-flight stalled on its LLM call."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakePartB, activate, deps, job_context
from support.world import configure_activity, default_settings, world

from integration.test_session_preflight import (
    CMID,
    _counts,
    _reset,
    _rolled_back,
    _start,
    moodle_source,
)
from vektra_assess.jobs import JobKind
from vektra_assess.worker.preflight import make_preflight_handler
from vektra_assess.worker.reaper import reap_starting

pytestmark = pytest.mark.integration


async def _lease(pg, sid: uuid.UUID, live: bool) -> None:  # type: ignore[no-untyped-def]
    until = "now() + interval '90 seconds'" if live else "now() - interval '1 second'"
    await pg.run(
        "super",
        f"UPDATE assess_jobs SET state = 'leased', lease_until = {until}, attempts = 1 "
        "WHERE kind = 'preflight' AND args->>'session_id' = :s",
        {"s": str(sid)},
    )


async def _age(pg, sid: uuid.UUID, seconds: int) -> None:  # type: ignore[no-untyped-def]
    await pg.run(
        "super",
        f"UPDATE assess_sessions SET created_at = now() - interval '{seconds} seconds' "
        "WHERE id = :s",
        {"s": sid},
    )


async def test_reaper_waits_for_a_live_lease_then_rolls_back_a_stalled_start(
    pg,
) -> None:  # type: ignore[no-untyped-def]
    await _reset(pg)
    await activate(pg)
    worker = create_async_engine(pg.url("worker"))
    settings = default_settings(VEKTRA_ASSESS_START_T_S=300)
    try:
        async with world(pg, settings) as w:
            await configure_activity(pg, w.site, CMID)
            sid = await _start(w)
            await _lease(pg, sid, live=True)
            release = asyncio.Event()
            runner = FakePartB(release=release)
            handler = make_preflight_handler(
                deps(settings, w.handoff_public, moodle_source(worker), runner)
            )
            task = asyncio.create_task(handler(job_context(worker, sid)))
            await asyncio.wait_for(runner.started.wait(), 30)  # stalled inside Part B
            # inside T: nothing to reap
            assert await reap_starting(worker.connect, 300) == 0
            # past T but the lease is live: a handler may still be running, left alone
            await _age(pg, sid, 400)
            assert await reap_starting(worker.connect, 300) == 0
            assert (await _counts(pg, sid))["assess_sessions"] == 1
            # the lease expired (the worker died or the handler was cancelled): rolled back
            await _lease(pg, sid, live=False)
            assert await reap_starting(worker.connect, 300) == 1
            await _rolled_back(pg, sid, "start_timeout")
            # the stalled provider answers late: the handler's commit finds no row, writes nothing
            release.set()
            await asyncio.wait_for(task, 30)
            assert await _counts(pg, sid) == dict.fromkeys(
                (
                    "assess_sessions",
                    "assess_snapshots",
                    "assess_session_keys",
                    "assess_turns",
                    "assess_session_handoff",
                ),
                0,
            )
            # a second pass has nothing left to do
            assert await reap_starting(worker.connect, 300) == 0
    finally:
        await worker.dispose()


async def test_a_never_claimed_start_is_reaped_after_t(pg) -> None:  # type: ignore[no-untyped-def]
    await _reset(pg)
    worker = create_async_engine(pg.url("worker"))
    try:
        async with world(pg) as w:
            await configure_activity(pg, w.site, CMID)
            sid = await _start(w)  # the job stays 'ready': no worker ever claimed it
            await _age(pg, sid, 61)
            assert await reap_starting(worker.connect, 60) == 1
            await _rolled_back(pg, sid, "start_timeout")
    finally:
        await worker.dispose()


async def test_a_budget_cancellation_rolls_back_at_once(pg) -> None:  # type: ignore[no-untyped-def]
    """Review fix 10: with T = 120 the runtime's budget cancels a pre-flight stalled in Part B;
    the cancellation goes through the rollback path (start_timeout), no reaper needed."""
    from vektra_assess.worker.policy import load_policy

    await _reset(pg)
    await activate(pg)
    pol = load_policy({"VEKTRA_ASSESS_START_T_S": "120"}).kinds[JobKind.PREFLIGHT]
    assert (pol.budget_s, pol.lease_s) == (105, 210)
    worker = create_async_engine(pg.url("worker"))
    settings = default_settings(VEKTRA_ASSESS_START_T_S=120)
    try:
        async with world(pg, settings) as w:
            await configure_activity(pg, w.site, CMID)
            sid = await _start(w)
            runner = FakePartB(release=asyncio.Event())  # never answers
            handler = make_preflight_handler(
                deps(settings, w.handoff_public, moodle_source(worker), runner)
            )
            # the runtime wraps every handler in asyncio.timeout(budget); a short one here
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(1.0):
                    await handler(job_context(worker, sid))
            assert runner.calls, "the pre-flight reached Part B before the cancellation"
            await _rolled_back(pg, sid, "start_timeout")
    finally:
        await worker.dispose()
