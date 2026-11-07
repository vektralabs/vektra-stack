"""M4.4: ``POST /sessions/{sid}/retry`` only nudges; it never changes the state (REQ-086)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.sessions import seed_session
from support.world import default_settings, err, world

from integration.test_session_preflight import _reset
from vektra_assess.api.pending import PendingTurnDriver
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


async def down() -> bool:
    raise ConnectionError("provider still down")


async def snapshot(pg, sid):  # type: ignore[no-untyped-def]
    return tuple(
        (
            await pg.run(
                "super",
                "SELECT s.state, s.credited_outage_s, s.deadline_at, s.epoch, "
                "(SELECT count(*) FROM assess_outage_incidents i "
                " WHERE i.session_id = s.id AND i.ended_at IS NULL) "
                "FROM assess_sessions s WHERE s.id = :s",
                {"s": sid},
            )
        )[0]
    )


async def test_retry_with_the_provider_still_down_changes_nothing(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "OUTAGE")
        await pg.run(
            "super",
            "UPDATE assess_sessions SET pending_reply_since = now() WHERE id = :s",
            {"s": s.session_id},
        )
        pending = PendingTurnDriver(w.app)
        w.app.state.assess_pending_driver = pending
        before = await snapshot(pg, s.session_id)
        r = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/retry",
            headers=bearer(s.session_token),
        )
        assert r.status_code == 202, r.text
        assert r.json() == {
            "session_id": str(s.session_id),
            "state": "OUTAGE",
            "reply_pending": True,
        }
        assert pending._wake.is_set(), "the retry nudges the pending-turn driver"
        await outage_tick(
            worker.connect, down, min_credit_s=20
        )  # the probe still fails
        after = await snapshot(pg, s.session_id)
        assert after == before and after[0] == "OUTAGE" and after[1] == 0


async def test_retry_is_rate_limited_per_session(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, default_settings(VEKTRA_ASSESS_RETRY_RPM=2)) as w:
        s = await seed_session(w, worker, "ACTIVE")
        url = f"/api/v1/assess/sessions/{s.session_id}/retry"
        codes = [
            (await w.client.post(url, headers=bearer(s.session_token))).status_code
            for _ in range(3)
        ]
        assert codes == [202, 202, 429]
        r = await w.client.post(url, headers=bearer(s.session_token))
        assert err(r)["code"] == "ERR-ASSESS-012"
        assert err(r)["details"] == {"reason": "retry_rate"}


async def test_retry_on_a_closed_session_is_refused(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "ACTIVE")
        await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/finish",
            headers=bearer(s.session_token),
        )
        r = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/retry",
            headers=bearer(s.session_token),
        )
        assert r.status_code == 409 and err(r)["code"] == "ERR-ASSESS-009", r.text


@pytest.mark.parametrize("route", ["retry", "turns/1/flag"])
async def test_a_foreign_token_does_not_use_up_the_victims_limit(
    pg, worker, route
) -> None:  # type: ignore[no-untyped-def]
    """Review fix 3: the per-session limiters count only requests that passed the gate."""
    settings = default_settings(VEKTRA_ASSESS_RETRY_RPM=1, VEKTRA_ASSESS_FLAG_RPM=1)
    async with world(pg, settings) as w:
        victim = await seed_session(w, worker, "ACTIVE", assignment="rubric_vis")
        other = await seed_session(w, worker, "ACTIVE", assignment="simple")
        url = f"/api/v1/assess/sessions/{victim.session_id}/{route}"
        for _ in range(3):
            r = await w.client.post(url, headers=bearer(other.session_token))
            assert r.status_code == 404, r.text
        r = await w.client.post(url, headers=bearer(victim.session_token))
        assert r.status_code in (201, 202), r.text
