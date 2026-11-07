"""M3.6: reconcile_reply answers a lost reply under the KEK, and only when no driver is live."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakeStreamRunner
from support.sessions import seed_session
from support.world import world

from integration.test_session_preflight import _reset
from vektra_assess.worker.reply_reconcile import queue_reconciles, reconcile_one

pytestmark = pytest.mark.integration

TIMEOUT = 45.0


async def n(pg, sql: str, **params) -> int:  # type: ignore[no-untyped-def]
    return int((await pg.run("super", sql, params))[0][0])


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _age_pending(pg, sid) -> None:  # type: ignore[no-untyped-def]
    await pg.run(
        "super",
        "UPDATE assess_sessions SET pending_reply_since = now() - interval '3 minutes' WHERE id = :s",
        {"s": sid},
    )


async def _marked(pg, sid) -> int:  # type: ignore[no-untyped-def]
    return await n(
        pg,
        "SELECT count(*) FROM assess_turns WHERE session_id = :s AND role = 'bot' AND kind = 'interrupted'",
        s=sid,
    )


async def test_a_failed_persist_is_reconciled_and_the_next_message_is_accepted(
    pg, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit (deferred to 8b, review fix 2): a persist that failed in the API leaves the
    student turn pending; the worker answers it with the marked turn and the session moves on."""
    from vektra_assess.api import turns as turns_mod
    from vektra_assess.api.material import MaterialUnavailable

    await _reset(pg)
    worker = create_async_engine(pg.url("worker"))
    release = asyncio.Event()
    runner = FakeStreamRunner(release=release)
    try:
        async with world(pg, assess_runner=runner) as w:
            s = await seed_session(w, worker, "ACTIVE")
            url = f"/api/v1/assess/sessions/{s.session_id}"
            assert (
                await w.client.post(
                    f"{url}/turns",
                    json={"text": "uno"},
                    headers=bearer(s.session_token),
                )
            ).status_code == 202
            original = turns_mod.load_material

            async def broken(*_a, **_k):  # type: ignore[no-untyped-def]
                raise MaterialUnavailable()

            monkeypatch.setattr(turns_mod, "load_material", broken)
            release.set()
            for task in list(w.app.state.assess_turn_driver.tasks.values()):
                await asyncio.wait_for(task, 15)
            monkeypatch.setattr(turns_mod, "load_material", original)
            assert (
                await n(
                    pg,
                    "SELECT count(*) FROM assess_sessions WHERE id=:s AND pending_reply_since IS NOT NULL",
                    s=s.session_id,
                )
                == 1
            )
            r = await w.client.post(
                f"{url}/turns", json={"text": "due"}, headers=bearer(s.session_token)
            )
            assert r.status_code == 409  # still blocked: the lost reply is pending
            # too young: nothing queued yet
            assert await queue_reconciles(worker.connect, TIMEOUT) == 0
            await _age_pending(pg, s.session_id)
            assert await queue_reconciles(worker.connect, TIMEOUT) == 1
            assert await queue_reconciles(worker.connect, TIMEOUT) == 0  # deduped
            assert (
                await reconcile_one(worker.connect, s.kek, s.session_id, TIMEOUT)
                is True
            )
            assert (
                await reconcile_one(worker.connect, s.kek, s.session_id, TIMEOUT)
                is False
            )
            assert await _marked(pg, s.session_id) == 1
            assert (
                await n(
                    pg,
                    "SELECT count(*) FROM assess_audit_events WHERE session_id=:s AND event='assess_reply_reconciled'",
                    s=s.session_id,
                )
                == 1
            )
            # the session accepts the next message, and the chain verifies through the marked turn
            r = await w.client.post(
                f"{url}/turns", json={"text": "due"}, headers=bearer(s.session_token)
            )
            assert r.status_code == 202 and r.json()["turn_no"] == 5
            for task in list(w.app.state.assess_turn_driver.tasks.values()):
                await asyncio.wait_for(task, 15)
            got = (await w.client.get(url, headers=bearer(s.session_token))).json()
            assert [t["kind"] for t in got["turns"]][2:] == [
                "normal",
                "interrupted",
                "normal",
                "normal",
            ]
    finally:
        await worker.dispose()


async def test_a_live_driver_keeps_the_turn_and_reconcile_leaves_it(pg) -> None:  # type: ignore[no-untyped-def]
    await _reset(pg)
    worker = create_async_engine(pg.url("worker"))
    release = asyncio.Event()
    try:
        async with world(pg, assess_runner=FakeStreamRunner(release=release)) as w:
            s = await seed_session(w, worker, "ACTIVE")
            url = f"/api/v1/assess/sessions/{s.session_id}"
            assert (
                await w.client.post(
                    f"{url}/turns",
                    json={"text": "uno"},
                    headers=bearer(s.session_token),
                )
            ).status_code == 202
            await asyncio.sleep(
                0.3
            )  # the driver holds the claim, stalled in the provider
            await _age_pending(pg, s.session_id)
            assert (
                await reconcile_one(worker.connect, s.kek, s.session_id, TIMEOUT)
                is False
            )
            assert await _marked(pg, s.session_id) == 0
            release.set()
            for task in list(w.app.state.assess_turn_driver.tasks.values()):
                await asyncio.wait_for(task, 15)
            # answered normally; nothing left to reconcile
            assert (
                await reconcile_one(worker.connect, s.kek, s.session_id, TIMEOUT)
                is False
            )
            assert await _marked(pg, s.session_id) == 0
            assert (
                await n(
                    pg,
                    "SELECT count(*) FROM assess_turns WHERE session_id=:s AND role='bot' AND kind='normal'",
                    s=s.session_id,
                )
                == 1
            )
    finally:
        await worker.dispose()


async def test_a_claimed_turn_does_not_hide_the_next_abandoned_one(pg) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit review 5808100754 (reply_reconcile.py 38-42): a live claim excludes the turn
    from the scan, so the batch reaches the later abandoned turns."""
    from sqlalchemy import text

    from integration.conftest import make_session
    from vektra_assess.turn_markers import REPLY_CLAIM_KEY_SQL

    await _reset(pg)
    await pg.run("super", "DELETE FROM assess_jobs")
    claimed, free = await make_session(pg), await make_session(pg)
    for sid, age in ((claimed, 600), (free, 300)):  # the claimed one is the oldest
        await pg.run(
            "super",
            "UPDATE assess_sessions SET pending_reply_since = now() - make_interval(secs => :a) "
            "WHERE id = :s",
            {"s": sid, "a": age},
        )
    holder = create_async_engine(pg.url("api"))
    worker = create_async_engine(pg.url("worker"))
    try:
        async with holder.connect() as conn:
            await conn.execute(
                text(f"SELECT pg_advisory_lock({REPLY_CLAIM_KEY_SQL})"),
                {"sid": str(claimed)},
            )
            await conn.commit()
            assert await queue_reconciles(worker.connect, 45, batch=1) == 1
            queued = await pg.run(
                "super",
                "SELECT args->>'session_id' FROM assess_jobs WHERE kind = 'reconcile_reply'",
            )
            assert [q[0] for q in queued] == [str(free)]
            await conn.execute(text("SELECT pg_advisory_unlock_all()"))
    finally:
        await holder.dispose()
        await worker.dispose()
