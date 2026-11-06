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
        # reading the session is still allowed (only turns need the fresh check)
        assert (
            await w.client.get(
                f"/api/v1/assess/sessions/{s.session_id}",
                headers=bearer(s.session_token),
            )
        ).status_code == 200


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
