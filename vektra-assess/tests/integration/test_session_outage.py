"""M4.1: a provider outage moves the session ACTIVE -> OUTAGE (REQ-086), on a real database.

The interval starts at the failed call's own start (the server-side ``ProviderCallRecord``), the
pending student turn is kept (no marked turn), the timer is frozen (the deadline does not move
until the outage ends) and the input is locked; ``outage_start`` is audited and published.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakeStreamRunner
from support.sessions import seed_session
from support.world import err, world

from integration.test_session_preflight import _reset
from vektra_assess.api.events import broker_of
from vektra_assess.llm.types import ProviderCallRecord, ProviderOutageError

pytestmark = pytest.mark.integration


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def outage(started_ago_s: float = 0.0) -> ProviderOutageError:
    now = datetime.now(UTC)
    start = now - timedelta(seconds=started_ago_s)
    return ProviderOutageError(
        ProviderCallRecord(start, None, now, "first_token_timeout")
    )


async def wait_idle(w) -> None:  # type: ignore[no-untyped-def]
    driver = getattr(w.app.state, "assess_turn_driver", None)
    if driver is not None:
        for task in list(driver.tasks.values()):
            await asyncio.wait_for(task, 15)


class _Req:
    def __init__(self, app) -> None:  # type: ignore[no-untyped-def]
        self.app = app


async def test_an_outage_moves_active_to_outage_and_keeps_the_pending_turn(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    runner = FakeStreamRunner(fail=outage(started_ago_s=4))
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        sid = s.session_id
        broker = broker_of(_Req(w.app))  # type: ignore[arg-type]
        broker.observe(sid, "ACTIVE", {})  # a connected stream saw ACTIVE
        # committed a minute ago (the interval is clamped to the commit)
        await pg.run(
            "super",
            "UPDATE assess_sessions SET committed_at = committed_at - interval '60 s', "
            "deadline_at = deadline_at - interval '60 s' WHERE id = :s",
            {"s": sid},
        )
        before = (
            await pg.run(
                "super",
                "SELECT deadline_at FROM assess_sessions WHERE id = :s",
                {"s": sid},
            )
        )[0][0]
        url = f"/api/v1/assess/sessions/{sid}"
        r = await w.client.post(
            f"{url}/turns", json={"text": "ciao"}, headers=bearer(s.session_token)
        )
        assert r.status_code == 202, r.text
        await wait_idle(w)

        row = (
            await pg.run(
                "super",
                "SELECT state, deadline_at, pending_reply_since IS NOT NULL, credited_outage_s "
                "FROM assess_sessions WHERE id = :s",
                {"s": sid},
            )
        )[0]
        assert row[0] == "OUTAGE"
        assert row[1] == before, (
            "the timer is frozen: the deadline moves only at the end"
        )
        assert row[2] is True and row[3] == 0, (
            "the pending turn is kept, nothing credited"
        )
        turns = await pg.run(
            "super",
            "SELECT role, kind FROM assess_turns WHERE session_id = :s ORDER BY turn_no",
            {"s": sid},
        )
        assert [tuple(t) for t in turns][-1] == ("student", "normal"), "no marked turn"

        incidents = await pg.run(
            "super",
            "SELECT extract(epoch FROM now() - started_at), ended_at, reason_code "
            "FROM assess_outage_incidents WHERE session_id = :s",
            {"s": sid},
        )
        assert len(incidents) == 1
        ago, ended, reason = incidents[0]
        assert 3.0 <= float(ago) <= 15.0, (
            "the interval starts at the failed call's start"
        )
        assert ended is None and reason == "first_token_timeout"
        audit = await pg.run(
            "super",
            "SELECT reason_code, actor_kind, data FROM assess_audit_events "
            "WHERE session_id = :s AND event = 'assess_outage_start'",
            {"s": sid},
        )
        assert [(a[0], a[1]) for a in audit] == [("first_token_timeout", "api")]
        assert audit[0][2] == {"failed_calls": 1}

        evs = [e.type for e in broker.after(sid, 0)]
        assert evs == ["turn_accepted", "outage_start"], evs

        got = (await w.client.get(url, headers=bearer(s.session_token))).json()
        assert got["state"] == "OUTAGE" and got["reply_pending"] is True
        # input locked: no turn while OUTAGE
        r = await w.client.post(
            f"{url}/turns", json={"text": "ci sei?"}, headers=bearer(s.session_token)
        )
        assert r.status_code == 409 and err(r)["code"] == "ERR-ASSESS-011", r.text
        assert err(r)["details"] == {"state": "OUTAGE"}


async def test_the_interval_never_starts_before_the_commit_or_after_now(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "ACTIVE")
        sid = s.session_id
        await pg.run(
            "api",
            "SELECT assess_enter_outage(:s, 1, now() - interval '3 days', 'error')",
            {"s": sid},
        )
        ok = await pg.run(
            "super",
            "SELECT i.started_at >= s.committed_at AND i.started_at <= now() "
            "FROM assess_outage_incidents i JOIN assess_sessions s ON s.id = i.session_id "
            "WHERE i.session_id = :s",
            {"s": sid},
        )
        assert ok[0][0] is True


async def test_enter_outage_is_idempotent_and_refuses_another_epoch(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "ACTIVE")
        sid = s.session_id
        q = "SELECT assess_enter_outage(:s, :e, now(), 'error')"
        assert (await pg.run("api", q, {"s": sid, "e": 7}))[0][0] == "noop"
        assert (await pg.run("api", q, {"s": sid, "e": 1}))[0][0] == "outage"
        assert (await pg.run("api", q, {"s": sid, "e": 1}))[0][0] == "outage"
        count = await pg.run(
            "super",
            "SELECT count(*) FROM assess_outage_incidents WHERE session_id = :s",
            {"s": sid},
        )
        assert count[0][0] == 1, "one open interval at a time"


async def test_remaining_time_is_frozen_while_in_outage(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4498473565: GET reports the time left at the outage start while the outage
    is open (the timer is paused), not against the advancing clock."""
    async with world(pg) as w:
        s = await seed_session(w, worker, "OUTAGE")
        await pg.run(
            "super",
            "UPDATE assess_outage_incidents SET started_at = now() - interval '60 seconds' "
            "WHERE session_id = :s",
            {"s": s.session_id},
        )
        await pg.run(
            "super",
            "UPDATE assess_sessions SET deadline_at = now() + interval '10 seconds' "
            "WHERE id = :s",
            {"s": s.session_id},
        )
        got = (
            await w.client.get(
                f"/api/v1/assess/sessions/{s.session_id}",
                headers=bearer(s.session_token),
            )
        ).json()
        assert got["state"] == "OUTAGE" and 65 <= got["remaining_s"] <= 71, got[
            "remaining_s"
        ]
