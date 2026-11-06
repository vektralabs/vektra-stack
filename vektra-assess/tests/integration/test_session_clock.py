"""M3.4: the lazy close of gate step 5, the epoch bump on every close, the closed-reconnect rule
and epoch-checked delivery of reply text (CodeRabbit 4493120906)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakeStreamRunner
from support.sessions import seed_session
from support.world import default_settings, err, world

from integration.test_session_events_sse import TIMING, parse
from integration.test_session_preflight import _reset

pytestmark = pytest.mark.integration


async def n(pg, sql: str, **params) -> int:  # type: ignore[no-untyped-def]
    return int((await pg.run("super", sql, params))[0][0])


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _row(pg, sid):  # type: ignore[no-untyped-def]
    return (
        await pg.run(
            "super",
            "SELECT state, close_reason, epoch, (SELECT count(*) FROM assess_jobs WHERE "
            "kind = 'transcript_generate' AND args->>'session_id' = :t) AS jobs, "
            "(SELECT count(*) FROM assess_session_handoff WHERE session_id = :s) AS handoff "
            "FROM assess_sessions WHERE id = :s",
            {"s": sid, "t": str(sid)},
        )
    )[0]


async def test_a_read_past_the_deadline_closes_timed_out_and_bumps_the_epoch(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "ACTIVE")
        await pg.run(
            "super",
            "UPDATE assess_sessions SET deadline_at = now() - interval '1 second' WHERE id = :s",
            {"s": s.session_id},
        )
        r = await w.client.get(
            f"/api/v1/assess/sessions/{s.session_id}", headers=bearer(s.session_token)
        )
        assert r.status_code == 409 and err(r)["code"] == "ERR-ASSESS-009"
        row = await _row(pg, s.session_id)
        assert (row.state, row.close_reason, row.epoch, row.jobs, row.handoff) == (
            "CLOSED",
            "timed_out",
            2,
            1,
            0,
        )
        # the old (epoch 1) token on the CLOSED row: reported closed, never "revoked" (401)
        r = await w.client.get(
            f"/api/v1/assess/sessions/{s.session_id}", headers=bearer(s.session_token)
        )
        assert r.status_code == 409
        assert (await _row(pg, s.session_id)).jobs == 1  # one close, one transcript job


async def test_a_pending_reply_inside_the_grace_keeps_the_session_open(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    release = asyncio.Event()
    async with world(pg, assess_runner=FakeStreamRunner(release=release)) as w:
        s = await seed_session(w, worker, "ACTIVE")
        url = f"/api/v1/assess/sessions/{s.session_id}"
        assert (
            await w.client.post(
                f"{url}/turns", json={"text": "ultima"}, headers=bearer(s.session_token)
            )
        ).status_code == 202
        await pg.run(
            "super",
            "UPDATE assess_sessions SET deadline_at = now() - interval '5 seconds', reply_grace_s = 60 WHERE id = :s",
            {"s": s.session_id},
        )
        r = await w.client.get(url, headers=bearer(s.session_token))
        assert (
            r.status_code == 200
            and r.json()["state"] == "ACTIVE"
            and r.json()["reply_pending"]
        )
        release.set()
        for task in list(w.app.state.assess_turn_driver.tasks.values()):
            await asyncio.wait_for(task, 15)
        # answered inside the grace; the next request closes it
        assert (
            await w.client.get(url, headers=bearer(s.session_token))
        ).status_code == 409
        row = await _row(pg, s.session_id)
        assert (row.state, row.close_reason) == ("CLOSED", "timed_out")
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_turns WHERE session_id = :s AND role = 'bot' AND kind = 'normal'",
                s=s.session_id,
            )
            == 1
        )


async def test_outage_is_never_timed_out_and_past_c_is_service_failure(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "OUTAGE", cap_minutes=30)  # C = 15 min
        await pg.run(
            "super",
            "UPDATE assess_sessions SET deadline_at = now() - interval '10 minutes' WHERE id = :s",
            {"s": s.session_id},
        )
        await pg.run(
            "super",
            "INSERT INTO assess_outage_incidents (session_id, started_at) VALUES (:s, now() - interval '14 minutes')",
            {"s": s.session_id},
        )
        r = await w.client.get(
            f"/api/v1/assess/sessions/{s.session_id}", headers=bearer(s.session_token)
        )
        assert (
            r.status_code == 200 and r.json()["state"] == "OUTAGE"
        )  # frozen clock: not timed out
        await pg.run(
            "super",
            "UPDATE assess_outage_incidents SET started_at = now() - interval '16 minutes' WHERE session_id = :s",
            {"s": s.session_id},
        )
        r = await w.client.get(
            f"/api/v1/assess/sessions/{s.session_id}", headers=bearer(s.session_token)
        )
        assert r.status_code == 409
        row = await _row(pg, s.session_id)
        assert (row.state, row.close_reason, row.epoch, row.jobs) == (
            "CLOSED",
            "service_failure",
            2,
            1,
        )


async def test_a_starting_session_past_t_is_reported_rolled_back(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, default_settings(VEKTRA_ASSESS_START_T_S=60)) as w:
        s = await seed_session(w, worker, "STARTING")
        await pg.run(
            "super",
            "UPDATE assess_sessions SET created_at = now() - interval '61 seconds' WHERE id = :s",
            {"s": s.session_id},
        )
        r = await w.client.get(
            f"/api/v1/assess/sessions/{s.session_id}", headers=bearer(s.session_token)
        )
        assert r.status_code == 409 and err(r)["details"]["reason"] == "start_timeout"
        assert (
            await n(
                pg, "SELECT count(*) FROM assess_sessions WHERE id = :s", s=s.session_id
            )
            == 1
        )  # the reaper deletes it


async def test_a_reconnect_after_the_close_gets_a_content_free_closed(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """Decision (plan Notes): an older-epoch token of the owner on a CLOSED row is closed, not
    revoked: GET 409, the event stream a content-free ``closed`` and nothing else."""
    from vektra_assess.api.events import broker_of

    async with world(pg, assess_sse_timing=TIMING) as w:
        s = await seed_session(w, worker, "ACTIVE")

        class R:
            app = w.app

        broker_of(R()).publish(
            s.session_id, "reply_delta", {"text": "CANARY-old-text"}, epoch=1
        )  # type: ignore[arg-type]
        async with worker.begin() as conn:
            await conn.execute(
                text("SELECT assess_close_session(:s, 1, 'finished')"),
                {"s": s.session_id},
            )
        assert (await _row(pg, s.session_id)).epoch == 2
        r = await w.client.get(
            f"/api/v1/assess/sessions/{s.session_id}/events",
            headers=bearer(s.session_token),
        )
        assert r.status_code == 200 and "CANARY-old-text" not in r.text
        assert [e["event"] for e in parse(r.text)] == ["state", "closed"]


async def test_reply_text_is_never_delivered_past_a_revocation(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4493120906: a delta reaches a stream only when its epoch is the token's AND the
    row, read just before the delivery, is still live at that epoch."""
    from vektra_assess.api.events import broker_of

    async with world(pg, assess_sse_timing={**TIMING, "poll_s": 0.2}) as w:
        s = await seed_session(w, worker, "ACTIVE")

        class R:
            app = w.app

        broker = broker_of(R())  # type: ignore[arg-type]
        stream = asyncio.create_task(
            w.client.get(
                f"/api/v1/assess/sessions/{s.session_id}/events",
                headers=bearer(s.session_token),
            )
        )
        await asyncio.sleep(0.3)
        # revoked while live (the epoch moves without a close) between two polls
        await pg.run(
            "super",
            "UPDATE assess_sessions SET epoch = 2 WHERE id = :s",
            {"s": s.session_id},
        )
        # the delivery check may reuse a row read up to poll_s old (review fix 6), never older
        await asyncio.sleep(0.3)
        broker.publish(
            s.session_id, "reply_delta", {"text": "CANARY-after-revocation"}, epoch=1
        )
        broker.publish(
            s.session_id, "reply_delta", {"text": "CANARY-other-epoch"}, epoch=2
        )
        await asyncio.sleep(0.5)
        await pg.run(
            "super",
            "UPDATE assess_sessions SET state = 'CLOSED', close_reason = 'finished' WHERE id = :s",
            {"s": s.session_id},
        )
        body = (await asyncio.wait_for(stream, 10)).text
        assert (
            "CANARY-after-revocation" not in body and "CANARY-other-epoch" not in body
        )


async def test_the_lazy_close_check_takes_no_row_lock_when_nothing_is_due(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """Review fix 5: a lock=False route is not blocked by another transaction holding the
    session row: the clock is read without a lock, and locked only when a close is due."""
    async with world(pg) as w:
        s = await seed_session(w, worker, "ACTIVE")
        holder = create_async_engine(pg.url("super"))
        try:
            async with holder.connect() as conn:
                await conn.execute(
                    text("SELECT 1 FROM assess_sessions WHERE id = :s FOR UPDATE"),
                    {"s": s.session_id},
                )
                r = await asyncio.wait_for(
                    w.client.post(
                        f"/api/v1/assess/sessions/{s.session_id}/retry",
                        headers=bearer(s.session_token),
                    ),
                    timeout=5,
                )
                assert r.status_code == 202, r.text
                await conn.rollback()
        finally:
            await holder.dispose()


async def test_reply_deltas_reuse_a_fresh_poll(pg, worker, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Review fix 6: a burst of reply deltas does not re-read the row once per batch; the
    delivery check reuses a read younger than poll_s."""
    from vektra_assess.api import events

    calls = []
    real = events._poll

    async def counted(request, sid):  # type: ignore[no-untyped-def]
        calls.append(sid)
        return await real(request, sid)

    monkeypatch.setattr(events, "_poll", counted)
    async with world(pg, assess_sse_timing={**TIMING, "poll_s": 2.0}) as w:
        s = await seed_session(w, worker, "ACTIVE")

        class R:
            app = w.app

        broker = events.broker_of(R())  # type: ignore[arg-type]
        stream = asyncio.create_task(
            w.client.get(
                f"/api/v1/assess/sessions/{s.session_id}/events",
                headers=bearer(s.session_token),
            )
        )
        await asyncio.sleep(0.3)
        before = len(calls)
        for i in range(20):
            broker.publish(s.session_id, "reply_delta", {"text": f"d{i} "}, epoch=1)
            await asyncio.sleep(0.02)
        during = len(calls) - before
        broker.publish(s.session_id, "reply_done", {"turn_no": 3})
        await pg.run(
            "super",
            "UPDATE assess_sessions SET state = 'CLOSED', close_reason = 'finished' WHERE id = :s",
            {"s": s.session_id},
        )
        body = (await asyncio.wait_for(stream, 10)).text
        assert "d19" in body
        assert during <= 2, during


async def test_a_deadline_passing_after_the_lazy_close_check_serves_no_material(
    pg, worker, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4498473554: the route's own transaction re-checks the clock; a deadline that
    passes between the lazy close and the read is refused, never answered with material."""
    from vektra_assess.api import sessions as sessions_mod

    async def too_early(*_a, **_k):  # type: ignore[no-untyped-def]
        return None  # the lazy close ran just before the deadline

    monkeypatch.setattr(sessions_mod, "enforce_clock", too_early)
    async with world(pg) as w:
        s = await seed_session(w, worker, "ACTIVE")
        await pg.run(
            "super",
            "UPDATE assess_sessions SET deadline_at = now() - interval '61 seconds', "
            "reply_grace_s = 60 WHERE id = :s",
            {"s": s.session_id},
        )
        r = await w.client.get(
            f"/api/v1/assess/sessions/{s.session_id}", headers=bearer(s.session_token)
        )
        assert r.status_code == 409 and err(r)["code"] == "ERR-ASSESS-009", r.text
        assert "turns" not in r.text
