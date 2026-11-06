"""M4.5: the ``service_failure`` close (REQ-086), on a real database.

Cumulative outage above C, or a third consecutive failed call, closes the session
``service_failure`` through the plan-2 close function: transcript job AND teacher notification job
in the same transaction as the close, epoch bumped, ``closed_service_failure`` message code.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakeStreamRunner
from support.sessions import seed_session
from support.world import err, world

from integration.test_session_preflight import _reset
from vektra_assess.api.events import broker_of
from vektra_assess.api.pending import PendingTurnDriver
from vektra_assess.llm.types import ProviderCallRecord, ProviderOutageError
from vektra_assess.worker.outage_probe import outage_tick

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


def failing() -> ProviderOutageError:
    now = datetime.now(UTC)
    return ProviderOutageError(ProviderCallRecord(now, None, now, "error", "APIError"))


async def ok_probe() -> bool:
    return True


async def down() -> bool:
    return False


async def wait_idle(w) -> None:  # type: ignore[no-untyped-def]
    driver = getattr(w.app.state, "assess_turn_driver", None)
    if driver is not None:
        for task in list(driver.tasks.values()):
            await asyncio.wait_for(task, 15)


async def closed_row(pg, sid):  # type: ignore[no-untyped-def]
    return tuple(
        (
            await pg.run(
                "super",
                "SELECT s.state, s.close_reason, s.epoch, "
                "(SELECT count(*) FROM assess_jobs j WHERE j.kind = 'transcript_generate' "
                " AND j.args->>'session_id' = s.id::text), "
                "(SELECT count(*) FROM assess_jobs j WHERE j.kind = 'teacher_notify' "
                " AND j.args->>'session_id' = s.id::text "
                " AND j.args->>'reason' = 'service_failure'), "
                "(SELECT count(*) FROM assess_outage_incidents i "
                " WHERE i.session_id = s.id AND i.ended_at IS NULL) "
                "FROM assess_sessions s WHERE s.id = :s",
                {"s": sid},
            )
        )[0]
    )


async def test_three_consecutive_failed_calls_close_service_failure(pg, worker) -> None:  # type: ignore[no-untyped-def]
    runner = FakeStreamRunner(fail=failing())
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        sid = s.session_id
        broker = broker_of(_Req(w.app))  # type: ignore[arg-type]
        broker.observe(sid, "ACTIVE", {})
        r = await w.client.post(
            f"/api/v1/assess/sessions/{sid}/turns",
            json={"text": "ciao"},
            headers=bearer(s.session_token),
        )
        assert r.status_code == 202
        await wait_idle(w)  # failed call 1 -> OUTAGE
        pending = PendingTurnDriver(w.app, min_age_s=0)
        for expected in ("OUTAGE", "CLOSED"):  # failed calls 2 and 3
            await outage_tick(worker.connect, ok_probe, min_credit_s=20)
            assert await pending.run_once() == [sid]
            await wait_idle(w)
            st = await pg.run(
                "super", "SELECT state FROM assess_sessions WHERE id = :s", {"s": sid}
            )
            assert st[0][0] == expected
        assert await closed_row(pg, sid) == ("CLOSED", "service_failure", 2, 1, 1, 0)
        intervals = await pg.run(
            "super",
            "SELECT count(*) FROM assess_outage_incidents WHERE session_id = :s",
            {"s": sid},
        )
        assert intervals[0][0] == 2  # the third failure closed instead of opening one
        audit = await pg.run(
            "super",
            "SELECT data->>'failed_calls' FROM assess_audit_events "
            "WHERE session_id = :s AND event = 'assess_outage_start' ORDER BY id",
            {"s": sid},
        )
        assert [a[0] for a in audit] == ["1", "2"]
        closed = [e for e in broker.after(sid, 0) if e.type == "closed"]
        assert len(closed) == 1
        assert closed[0].data["message_code"] == "closed_service_failure"


async def test_a_successful_reply_resets_the_consecutive_count(pg, worker) -> None:  # type: ignore[no-untyped-def]
    runner = FakeStreamRunner(fail=failing())
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        sid = s.session_id
        url = f"/api/v1/assess/sessions/{sid}/turns"
        await w.client.post(url, json={"text": "a"}, headers=bearer(s.session_token))
        await wait_idle(w)
        runner.fail = None
        await outage_tick(worker.connect, ok_probe, min_credit_s=20)
        assert await PendingTurnDriver(w.app, min_age_s=0).run_once() == [sid]
        await wait_idle(w)  # answered: the count starts again
        runner.fail = failing()
        await w.client.post(url, json={"text": "b"}, headers=bearer(s.session_token))
        await wait_idle(w)
        audit = await pg.run(
            "super",
            "SELECT data->>'failed_calls' FROM assess_audit_events "
            "WHERE session_id = :s AND event = 'assess_outage_start' ORDER BY id",
            {"s": sid},
        )
        assert [a[0] for a in audit] == ["1", "1"]


async def test_cumulative_outage_above_c_closes_through_the_probe_tick(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "OUTAGE", cap_minutes=30)  # C = 900 s
        await pg.run(
            "super",
            "UPDATE assess_outage_incidents SET started_at = now() - interval '901 s' "
            "WHERE session_id = :s",
            {"s": s.session_id},
        )
        got = await outage_tick(worker.connect, down, min_credit_s=20)
        assert got.closed == 1
        assert await closed_row(pg, s.session_id) == (
            "CLOSED",
            "service_failure",
            2,
            1,
            1,
            0,  # the open interval ended with the close
        )


async def test_cumulative_outage_above_c_closes_lazily_on_a_request(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "OUTAGE", cap_minutes=30)
        await pg.run(
            "super",
            "UPDATE assess_outage_incidents SET started_at = now() - interval '901 s' "
            "WHERE session_id = :s",
            {"s": s.session_id},
        )
        r = await w.client.get(
            f"/api/v1/assess/sessions/{s.session_id}", headers=bearer(s.session_token)
        )
        assert r.status_code == 409 and err(r)["code"] == "ERR-ASSESS-009"
        assert (await closed_row(pg, s.session_id))[:5] == (
            "CLOSED",
            "service_failure",
            2,
            1,
            1,
        )


async def test_other_closes_notify_nobody(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "ACTIVE")
        await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/finish",
            headers=bearer(s.session_token),
        )
        assert (await closed_row(pg, s.session_id))[:5] == (
            "CLOSED",
            "finished",
            2,
            1,
            0,
        )
