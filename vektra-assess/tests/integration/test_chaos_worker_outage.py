"""Chaos: the worker down for 10 minutes, on an injected clock (plan 13a, key lifecycle).

Nothing sleeps ten real minutes. The database clock is ``now()``, so time is moved by rewinding
every timestamp the lifecycle compares to it (deadline, commit, enrolment check, heartbeat):
``advance(pg, s)`` is ``s`` seconds of wall time for every rule that reads those rows. No worker
code runs while the worker is "down". Three live sessions of one site:

- ``talking``: the student keeps sending turns;
- ``lazy``: its deadline passes and the student's tab asks again (the API's lazy close);
- ``abandoned``: its deadline passes and nobody asks.

What the rows show, measured, not assumed:

1. inside the enrolment freshness window (150 s) turns keep working without the worker;
2. past it a turn is REFUSED (503 ``enrolment_freshness``), state unchanged, audited. This is the
   designed fail-closed of gate step 6 (NFR-014, M3.7): the plan's "turns continue for 10 min"
   holds only for the first ``VEKTRA_ASSESS_ENROLMENT_FRESHNESS_S`` seconds of an outage;
3. closes are deferred: ``lazy`` is closed by the API itself (state, epoch, hand-off, one
   ``transcript_generate`` job) but its transcript is not generated, the job stays ``ready``;
   ``abandoned`` stays ACTIVE past its deadline, with no job at all;
4. the worker returns (its own functions: the enrolment re-check, the sweeper, the job claim with
   the production transcript handler and the fenced complete): ``abandoned`` is closed
   ``timed_out``, both transcripts are generated exactly once, every job is ``done``, the
   ``talking`` session takes turns again, and the settled invariants hold.

The negative control stops before step 4 and shows that the settled checker flags the deferred
close, so the passing run above is not a checker that cannot fail.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakeSource, FakeStreamRunner, kek_ring
from support.sessions import seed_session
from support.world import err, world

from integration.test_session_preflight import _reset
from vektra_assess.jobs import JobKind
from vektra_assess.keys import AssessKeyring
from vektra_assess.transcript import store
from vektra_assess.worker import sql
from vektra_assess.worker.enrolment import make_recheck_handler, queue_rechecks
from vektra_assess.worker.runner import JobContext
from vektra_assess.worker.sweeper import sweep_timed_out
from vektra_assess.worker.transcript_gen import GenerationDeps, make_generate_handler

sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "tests" / "chaos" / "assess")
)
from invariants import assert_invariants, check

pytestmark = pytest.mark.integration

OUTAGE_S = 600  # ten minutes of worker outage
WORKER_ID = "chaos-returned"


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


def bearer(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


async def advance(pg: Any, seconds: int) -> None:
    """The injected clock: ``seconds`` pass for every rule that compares these rows to now()."""
    iv = {"s": seconds}
    for sql_ in (
        "UPDATE assess_sessions SET deadline_at = deadline_at - make_interval(secs => :s), "
        "committed_at = committed_at - make_interval(secs => :s), "
        "created_at = created_at - make_interval(secs => :s) WHERE state <> 'CLOSED'",
        "UPDATE assess_enrolment_checks SET checked_at = checked_at - make_interval(secs => :s)",
        "UPDATE assess_heartbeat SET beat_at = beat_at - make_interval(secs => :s)",
    ):
        await pg.run("super", sql_, iv)


async def one(pg: Any, sql_: str, **params: Any) -> Any:
    return (await pg.run("super", sql_, params))[0][0]


async def session_rows(pg: Any, sid: Any) -> tuple[Any, ...]:
    """state, close_reason, epoch, hand-off rows, transcript jobs, transcripts, turns."""
    return tuple(
        (
            await pg.run(
                "super",
                "SELECT s.state, s.close_reason, s.epoch, "
                "(SELECT count(*) FROM assess_session_handoff h WHERE h.session_id = s.id), "
                "(SELECT count(*) FROM assess_jobs j WHERE j.kind = 'transcript_generate' "
                " AND j.args->>'session_id' = s.id::text), "
                "(SELECT count(*) FROM assess_transcripts t WHERE t.session_id = s.id), "
                "(SELECT count(*) FROM assess_turns t WHERE t.session_id = s.id) "
                "FROM assess_sessions s WHERE s.id = :s",
                {"s": sid},
            )
        )[0]
    )


async def jobs_by_kind_state(pg: Any) -> dict[tuple[str, str], int]:
    return {
        (r[0], r[1]): int(r[2])
        for r in await pg.run(
            "super", "SELECT kind, state, count(*) FROM assess_jobs GROUP BY 1, 2"
        )
    }


async def wait_idle(w: Any) -> None:
    driver = getattr(w.app.state, "assess_turn_driver", None)
    if driver is not None:
        for task in list(driver.tasks.values()):
            await asyncio.wait_for(task, 15)


async def turn(
    w: Any, s: Any, text_: str = "Ho scelto quel criterio perche'..."
) -> Any:
    return await w.client.post(
        f"/api/v1/assess/sessions/{s.session_id}/turns",
        json={"text": text_},
        headers=bearer(s.session_token),
    )


def _asyncpg_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def drain(pg: Any, engine: Any, handlers: dict[JobKind, Any]) -> list[str]:
    """The returned worker's job loop for ``handlers``: the production claim (lease, fence),
    the handler, the fenced complete. Returns the kinds it ran, in order."""
    conn = await asyncpg.connect(_asyncpg_dsn(pg.url("worker")))
    ran: list[str] = []
    try:
        while True:
            claimed = await sql.claim(conn, list(handlers), WORKER_ID, 60.0, 50)
            if not claimed:
                return ran
            for job in claimed:
                ctx = JobContext(
                    job_id=job.id,
                    kind=job.kind,
                    args=job.args,
                    attempt=job.attempt,
                    acquire=engine.connect,
                    log=logging.getLogger("chaos"),
                )
                await handlers[job.kind](ctx)
                assert await sql.complete(conn, job.id, WORKER_ID, job.attempt)
                ran.append(job.kind.value)
    finally:
        await conn.close()


async def _outage(
    w: Any, worker: Any, runner: FakeStreamRunner
) -> tuple[Any, Any, Any]:
    """Seed the three sessions, take the worker down and run the 10 minutes of outage.

    Returns ``(talking, lazy, abandoned)`` with every outage-time row delta asserted."""
    pg = w.pg
    kek = kek_ring()  # one worker KEK for the three sessions, as in a deployment
    talking = await seed_session(w, worker, "ACTIVE", assignment="rubric_vis", kek=kek)
    lazy = await seed_session(w, worker, "ACTIVE", assignment="simple", kek=kek)
    abandoned = await seed_session(w, worker, "ACTIVE", assignment="scale", kek=kek)
    for s in (
        lazy,
        abandoned,
    ):  # due 5 minutes into the outage, well past the reply grace
        await pg.run(
            "super",
            "UPDATE assess_sessions SET deadline_at = now() + interval '300 seconds', "
            "reply_grace_s = 60 WHERE id = :s",
            {"s": s.session_id},
        )
    # the seeding ran each pre-flight in-process, not through its job: those rows are settled
    # here so that "no job left" below counts only what the outage produced
    await pg.run(
        "super", "UPDATE assess_jobs SET state = 'done' WHERE kind = 'preflight'"
    )
    jobs_before = await jobs_by_kind_state(pg)
    assert all(kind != "transcript_generate" for kind, _ in jobs_before)
    base = {
        s.session_id: await session_rows(pg, s.session_id)
        for s in (talking, lazy, abandoned)
    }
    assert {r[:6] for r in base.values()} == {("ACTIVE", None, 1, 1, 0, 0)}
    turns0 = base[talking.session_id][6]
    assert turns0 >= 1

    # minute 2: inside the freshness window, the API alone answers the turn
    await advance(pg, 120)
    r = await turn(w, talking)
    assert r.status_code == 202, r.text
    await wait_idle(w)
    assert (await session_rows(pg, talking.session_id))[6] == turns0 + 2
    assert len(runner.turns) == 1

    # minute 10: the last enrolment check is 600 s old, the turn is refused fail-closed
    await advance(pg, OUTAGE_S - 120)
    refused_before = await one(
        pg,
        "SELECT count(*) FROM assess_audit_events WHERE session_id = :s "
        "AND reason_code = 'enrolment'",
        s=talking.session_id,
    )
    r = await turn(w, talking)
    assert r.status_code == 503 and "enrolment_freshness" in err(r)["remediation"], (
        r.text
    )
    assert len(runner.turns) == 1  # no model call
    assert await session_rows(pg, talking.session_id) == (
        "ACTIVE",
        None,
        1,
        1,
        0,
        0,
        turns0 + 2,
    )
    assert (
        await one(
            pg,
            "SELECT count(*) FROM assess_audit_events WHERE session_id = :s "
            "AND reason_code = 'enrolment'",
            s=talking.session_id,
        )
        == refused_before + 1
    )

    # the lazy close still runs on the API path: closed, one job, nobody generates it
    r = await w.client.get(
        f"/api/v1/assess/sessions/{lazy.session_id}", headers=bearer(lazy.session_token)
    )
    assert r.status_code == 409, r.text
    lazy_turns = base[lazy.session_id][6]
    assert await session_rows(pg, lazy.session_id) == (
        "CLOSED",
        "timed_out",
        2,
        0,
        1,
        0,
        lazy_turns,
    )
    # nobody asked about this one: the close is deferred, no job exists for it
    assert await session_rows(pg, abandoned.session_id) == base[abandoned.session_id]
    jobs = await jobs_by_kind_state(pg)
    assert jobs.get(("transcript_generate", "ready")) == 1
    assert jobs.get(("transcript_generate", "done"), 0) == 0
    return talking, lazy, abandoned


async def test_worker_down_ten_minutes_closes_deferred_then_reconciled(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    runner = FakeStreamRunner()
    tkek = AssessKeyring(store.KEYRING, "t1", {"t1": os.urandom(32)})
    async with world(pg, assess_runner=runner) as w:
        w.app.state.assess_keys[store.KEYRING] = tkek
        talking, lazy, abandoned = await _outage(w, worker, runner)

        # the worker returns: heartbeat, enrolment re-check, sweeper, then the job loop
        await pg.run("super", "UPDATE assess_heartbeat SET beat_at = now()")
        assert await queue_rechecks(worker.connect) == 2  # talking + abandoned are live
        assert (
            await sweep_timed_out(worker.connect) == 1
        )  # abandoned, the deferred close
        assert await session_rows(pg, abandoned.session_id) == (
            "CLOSED",
            "timed_out",
            2,
            0,
            1,
            0,
            (await session_rows(pg, abandoned.session_id))[6],
        )
        ran = await drain(
            pg,
            worker,
            {
                JobKind.ENROLMENT_RECHECK: make_recheck_handler(
                    FakeSource(enrolled=True)
                ),
                JobKind.TRANSCRIPT_GENERATE: make_generate_handler(
                    GenerationDeps(kek=talking.kek, transcript_kek=tkek)
                ),
            },
        )
        assert sorted(ran) == [
            "enrolment_recheck",
            "enrolment_recheck",
            "transcript_generate",
            "transcript_generate",
        ], ran
        for s in (lazy, abandoned):
            assert (await session_rows(pg, s.session_id))[:6] == (
                "CLOSED",
                "timed_out",
                2,
                0,
                1,
                1,
            )
        jobs = await jobs_by_kind_state(pg)
        assert jobs.get(("transcript_generate", "done")) == 2
        assert not [k for k in jobs if k[1] in ("ready", "leased", "dead")], jobs

        # turns again, now that the enrolment is freshly confirmed
        before = (await session_rows(pg, talking.session_id))[6]
        r = await turn(w, talking, "E per il secondo criterio...")
        assert r.status_code == 202, r.text
        await wait_idle(w)
        assert (await session_rows(pg, talking.session_id))[:7] == (
            "ACTIVE",
            None,
            1,
            1,
            0,
            0,
            before + 2,
        )
        assert len(runner.turns) == 2

        # a second pass of the returned worker does nothing more
        assert await sweep_timed_out(worker.connect) == 0
        assert (
            await drain(
                pg,
                worker,
                {
                    JobKind.TRANSCRIPT_GENERATE: make_generate_handler(
                        GenerationDeps(kek=talking.kek, transcript_kek=tkek)
                    )
                },
            )
            == []
        )
        await assert_invariants(
            pg,
            "worker-down-10min:reconciled",
            expect_sessions=3,
            sites=[w.site],
            settled=True,
        )


async def test_control_without_the_returning_worker_the_settled_check_fails(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """Negative control: the same outage, the worker never comes back. The lazily closed session
    has no transcript, and the settled checker must say so (scenario ``negative:``)."""
    runner = FakeStreamRunner()
    async with world(pg, assess_runner=runner) as w:
        _talking, lazy, abandoned = await _outage(w, worker, runner)
        report = await check(
            pg,
            "negative:worker-down-unreconciled",
            expect_sessions=3,
            sites=[w.site],
            settled=True,
        )
        assert not report.ok
        assert str(lazy.session_id) in report.violations["transcript_per_closed"]
        # the abandoned session is still ACTIVE past its deadline: the sweeper never ran
        assert (await session_rows(pg, abandoned.session_id))[0] == "ACTIVE"
