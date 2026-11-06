"""M3.7: enrolment freshness (gate step 6) and the re-check tick."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakeSource, FakeStreamRunner
from support.sessions import seed_session
from support.world import err, world

from integration.test_session_preflight import _reset, moodle_source
from vektra_assess.worker.enrolment import queue_rechecks, recheck_one

pytestmark = pytest.mark.integration


async def n(pg, sql: str, **params) -> int:  # type: ignore[no-untyped-def]
    return int((await pg.run("super", sql, params))[0][0])


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


async def _turn(w, s):  # type: ignore[no-untyped-def]
    return await w.client.post(
        f"/api/v1/assess/sessions/{s.session_id}/turns",
        json={"text": "ciao"},
        headers=bearer(s.session_token),
    )


async def test_the_commit_writes_the_first_check_and_a_fresh_one_admits_a_turn(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, assess_runner=FakeStreamRunner()) as w:
        s = await seed_session(w, worker, "ACTIVE")
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_enrolment_checks WHERE session_id=:s AND result='enrolled'",
                s=s.session_id,
            )
            == 1
        )
        assert (await _turn(w, s)).status_code == 202


async def test_a_stopped_worker_rejects_turns_without_closing(pg, worker) -> None:  # type: ignore[no-untyped-def]
    runner = FakeStreamRunner()
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        # the last re-check is 151 s old: the worker has been down past the freshness window
        await pg.run(
            "super",
            "UPDATE assess_enrolment_checks SET checked_at = now() - interval '151 seconds' WHERE session_id = :s",
            {"s": s.session_id},
        )
        before = await pg.run(
            "super",
            "SELECT state, seq, epoch FROM assess_sessions WHERE id=:s",
            {"s": s.session_id},
        )
        r = await _turn(w, s)
        assert r.status_code == 503 and "enrolment_freshness" in err(r)["remediation"]
        assert runner.turns == []
        assert [
            tuple(x)
            for x in await pg.run(
                "super",
                "SELECT state, seq, epoch FROM assess_sessions WHERE id=:s",
                {"s": s.session_id},
            )
        ] == [tuple(x) for x in before]
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE session_id=:s AND reason_code='enrolment'",
                s=s.session_id,
            )
            == 1
        )
        # reading the session needs it too (CodeRabbit 4498473572): no decrypted history and
        # no resume token for a student whose enrolment is not freshly confirmed
        r = await w.client.get(
            f"/api/v1/assess/sessions/{s.session_id}", headers=bearer(s.session_token)
        )
        assert r.status_code == 503 and "enrolment_freshness" in err(r)["remediation"]
        assert "turns" not in r.text


async def test_the_recheck_against_the_recordings_keeps_an_enrolled_student(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, assess_runner=FakeStreamRunner()) as w:
        s = await seed_session(w, worker, "ACTIVE")
        assert await queue_rechecks(worker.connect) == 1
        assert await queue_rechecks(worker.connect) == 0  # deduped while pending
        assert (
            await recheck_one(worker.connect, moodle_source(worker), s.session_id)
            == "enrolled"
        )
        assert (await _turn(w, s)).status_code == 202


async def test_an_unenrolled_student_is_closed_access_revoked(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "ACTIVE")
        assert (
            await recheck_one(worker.connect, FakeSource(enrolled=False), s.session_id)
            == "not_enrolled"
        )
        row = (
            await pg.run(
                "super",
                "SELECT state, close_reason, epoch, (SELECT count(*) FROM assess_jobs WHERE kind='transcript_generate' AND args->>'session_id' = :t) FROM assess_sessions WHERE id = :s",
                {"s": s.session_id, "t": str(s.session_id)},
            )
        )[0]
        assert tuple(row) == ("CLOSED", "access_revoked", 2, 1)


async def test_an_unreadable_enrolment_rejects_turns_and_closes_nothing(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, assess_runner=FakeStreamRunner()) as w:
        s = await seed_session(w, worker, "ACTIVE")
        assert (
            await recheck_one(worker.connect, FakeSource(enrolled=None), s.session_id)
            == "unreadable"
        )
        assert (await _turn(w, s)).status_code == 503
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_sessions WHERE id=:s AND state='ACTIVE'",
                s=s.session_id,
            )
            == 1
        )


async def test_every_live_session_is_rechecked_within_a_few_ticks(pg) -> None:  # type: ignore[no-untyped-def]
    """Review fix 9: the re-check queue takes the sessions whose last check is oldest (never
    checked first), so a batch limit below the number of live sessions still reaches all."""
    from sqlalchemy.ext.asyncio import create_async_engine as _engine

    from integration.conftest import make_session
    from integration.test_session_preflight import _reset as _clean
    from vektra_assess.worker.enrolment import queue_rechecks

    await _clean(pg)
    await pg.run("super", "DELETE FROM assess_jobs")
    sids = [await make_session(pg) for _ in range(5)]
    engine = _engine(pg.url("worker"))
    try:
        for _ in range(3):  # three ticks of two
            await queue_rechecks(engine.connect, batch=2)
            queued = await pg.run(
                "super",
                "SELECT (args->>'session_id')::uuid FROM assess_jobs "
                "WHERE kind = 'enrolment_recheck'",
            )
            for (sid,) in queued:  # the handler's effect: one check row, the job done
                await pg.run(
                    "super",
                    "INSERT INTO assess_enrolment_checks (session_id, result) "
                    "VALUES (:s, 'enrolled')",
                    {"s": sid},
                )
            await pg.run("super", "DELETE FROM assess_jobs")
        checked = await pg.run(
            "super",
            "SELECT count(DISTINCT session_id) FROM assess_enrolment_checks "
            "WHERE session_id = ANY(:s)",
            {"s": sids},
        )
        assert checked[0][0] == 5
    finally:
        await engine.dispose()


async def test_sessions_with_a_pending_recheck_do_not_take_the_batch(pg) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4498466303: when the oldest sessions' jobs are still pending, the next tick
    queues the other sessions instead of selecting (and deduplicating) the same ones."""
    from sqlalchemy.ext.asyncio import create_async_engine as _engine

    from integration.conftest import make_session
    from integration.test_session_preflight import _reset as _clean
    from vektra_assess.worker.enrolment import queue_rechecks

    await _clean(pg)
    await pg.run("super", "DELETE FROM assess_jobs")
    for _ in range(4):
        await make_session(pg)
    engine = _engine(pg.url("worker"))
    try:
        assert await queue_rechecks(engine.connect, batch=2) == 2
        assert (
            await queue_rechecks(engine.connect, batch=2) == 2
        )  # the jobs are not run
        queued = await pg.run(
            "super",
            "SELECT count(DISTINCT args->>'session_id') FROM assess_jobs "
            "WHERE kind = 'enrolment_recheck'",
        )
        assert queued[0][0] == 4
    finally:
        await engine.dispose()
