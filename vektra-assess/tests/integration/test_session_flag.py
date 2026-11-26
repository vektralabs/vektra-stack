"""M5.9 / REQ-103, FEAT-034: thumbs up / thumbs down on an assistant reply, on a real database."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.sessions import seed_session
from support.world import default_settings, err, world

from integration.test_session_preflight import _reset

pytestmark = pytest.mark.integration


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


UP = {"rating": "up"}


async def state_of(pg, sid):  # type: ignore[no-untyped-def]
    return tuple(
        (
            await pg.run(
                "super",
                "SELECT (SELECT count(*) FROM assess_turn_flags WHERE session_id = :s), "
                "(SELECT count(*) FROM assess_audit_events WHERE session_id = :s "
                " AND event = 'assess_flag_added'), "
                "(SELECT string_agg(turn_hash, ',' ORDER BY turn_no) FROM assess_turns "
                " WHERE session_id = :s)",
                {"s": sid},
            )
        )[0]
    )


@pytest.mark.parametrize("state", ["ACTIVE", "OUTAGE"])
async def test_one_flag_is_exactly_one_marker_and_the_hashes_stay_equal(
    pg, worker, state
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, state)
        url = f"/api/v1/assess/sessions/{s.session_id}/turns/1/flag"
        _, _, hashes = await state_of(pg, s.session_id)
        r = await w.client.post(url, json=UP, headers=bearer(s.session_token))
        assert r.status_code == 201, r.text
        r = await w.client.post(
            url, json=UP, headers=bearer(s.session_token)
        )  # idempotent
        assert r.status_code == 200 and r.json()["rating"] == "up"
        assert await state_of(pg, s.session_id) == (1, 1, hashes)


async def test_only_an_assistant_turn_can_be_flagged(pg, worker) -> None:  # type: ignore[no-untyped-def]
    from support.preflight import FakeStreamRunner

    release = asyncio.Event()  # the reply never comes: turn 3 stays the student's last
    async with world(pg, assess_runner=FakeStreamRunner(release=release)) as w:
        s = await seed_session(w, worker, "ACTIVE")
        r = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/turns/99/flag",
            json=UP,
            headers=bearer(s.session_token),
        )
        assert r.status_code == 400 and err(r)["details"]["reason"] == "not_a_bot_turn"
        # the student's own turn exists and is not the assistant's
        posted = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/turns",
            json={"text": "la mia risposta"},
            headers=bearer(s.session_token),
        )
        assert posted.status_code == 202 and posted.json()["turn_no"] == 3
        r = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/turns/3/flag",
            json=UP,
            headers=bearer(s.session_token),
        )
        assert r.status_code == 400 and err(r)["details"]["reason"] == "not_a_bot_turn"
        release.set()
        for task in list(w.app.state.assess_turn_driver.tasks.values()):
            await asyncio.wait_for(task, 15)


async def test_a_flag_after_closed_is_rejected(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "ACTIVE")
        await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/finish",
            headers=bearer(s.session_token),
        )
        r = await w.client.post(
            f"/api/v1/assess/sessions/{s.session_id}/turns/1/flag",
            json=UP,
            headers=bearer(s.session_token),
        )
        assert r.status_code == 409 and err(r)["code"] == "ERR-ASSESS-009"
        assert (await state_of(pg, s.session_id))[:2] == (0, 0)


async def test_flags_are_rate_limited_per_session(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, default_settings(VEKTRA_ASSESS_FLAG_RPM=1)) as w:
        s = await seed_session(w, worker, "ACTIVE")
        url = f"/api/v1/assess/sessions/{s.session_id}/turns/1/flag"
        assert (
            await w.client.post(url, json=UP, headers=bearer(s.session_token))
        ).status_code == 201
        r = await w.client.post(url, json=UP, headers=bearer(s.session_token))
        assert r.status_code == 429 and err(r)["details"] == {"reason": "flag_rate"}


async def test_a_flag_survives_a_reload(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit review 5808100754 (flag.py 66-67): the marker is persisted AND served: a
    reload or a resume shows which assistant turn the student flagged."""
    async with world(pg) as w:
        s = await seed_session(w, worker, "ACTIVE")
        url = f"/api/v1/assess/sessions/{s.session_id}"
        assert (
            await w.client.post(
                f"{url}/turns/2/flag", json=UP, headers=bearer(s.session_token)
            )
        ).status_code == 201
        turns = (await w.client.get(url, headers=bearer(s.session_token))).json()[
            "turns"
        ]
        assert [(t["turn_no"], t["rating"], t["flagged"]) for t in turns] == [
            (1, None, False),
            (2, "up", False),
        ]


async def test_a_thumbs_down_needs_its_reason(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """FEAT-034: the flag route records only a thumbs up; no body or a "down" is refused."""
    async with world(pg) as w:
        s = await seed_session(w, worker, "ACTIVE")
        url = f"/api/v1/assess/sessions/{s.session_id}/turns/2/flag"
        r = await w.client.post(
            url, json={"rating": "down"}, headers=bearer(s.session_token)
        )
        assert r.status_code == 400 and err(r)["details"]["reason"] == "reason_required"
        r = await w.client.post(url, headers=bearer(s.session_token))
        assert r.status_code == 400 and err(r)["details"]["reason"] == "body_not_object"
        assert (await state_of(pg, s.session_id))[:2] == (0, 0)


async def test_a_thumbs_down_is_the_reason_turn_linked_to_the_reply(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """FEAT-034: the reason is an ordinary student turn (hash-chained, answered); the reply is
    rated "down" with a link to it, in the same transaction; the audit row stays content-free."""
    from support.preflight import FakeStreamRunner

    runner = FakeStreamRunner()
    async with world(pg, assess_runner=runner) as w:
        s = await seed_session(w, worker, "ACTIVE")
        base = f"/api/v1/assess/sessions/{s.session_id}"
        auth = bearer(s.session_token)
        r = await w.client.post(
            f"{base}/turns",
            json={"text": "Il punteggio massimo era 5.", "contests_turn": 2},
            headers=auth,
        )
        assert r.status_code == 202, r.text
        assert r.json()["turn_no"] == 3
        for task in list(w.app.state.assess_turn_driver.tasks.values()):
            await asyncio.wait_for(task, 15)
        # the model saw the reason with an excerpt of the reply it contests
        assert runner.turns[-1].contested and runner.turns[-1].text.startswith(
            "Il punteggio"
        )
        rows = await pg.run(
            "super",
            "SELECT turn_no, rating, reason_turn_no FROM assess_turn_flags "
            "WHERE session_id = :s",
            {"s": s.session_id},
        )
        assert [tuple(x) for x in rows] == [(2, "down", 3)]
        audit = await pg.run(
            "super",
            "SELECT data FROM assess_audit_events WHERE session_id = :s "
            "AND event = 'assess_flag_added'",
            {"s": s.session_id},
        )
        assert [a[0] for a in audit] == [
            {"turn_no": 2, "rating": "down", "reason_turn_no": 3}
        ]
        turns = (await w.client.get(base, headers=auth)).json()["turns"]
        by_no = {t["turn_no"]: t for t in turns}
        assert (by_no[2]["rating"], by_no[2]["flagged"]) == ("down", True)
        assert by_no[3]["contests_turn"] == 2 and by_no[3]["role"] == "student"
        assert by_no[4]["role"] == "bot"  # the assistant answered the reason
        # a rating is final: neither a thumbs up nor a second reason on the same reply
        r = await w.client.post(f"{base}/turns/2/flag", json=UP, headers=auth)
        assert r.status_code == 409 and err(r)["details"] == {
            "reason": "already_rated",
            "rating": "down",
        }
        r = await w.client.post(
            f"{base}/turns", json={"text": "ancora", "contests_turn": 2}, headers=auth
        )
        assert r.status_code == 409 and err(r)["details"]["reason"] == "already_rated"


async def test_a_reason_must_contest_an_assistant_reply_not_yet_rated(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    from support.preflight import FakeStreamRunner

    async with world(pg, assess_runner=FakeStreamRunner()) as w:
        s = await seed_session(w, worker, "ACTIVE")
        base = f"/api/v1/assess/sessions/{s.session_id}"
        auth = bearer(s.session_token)
        assert (
            await w.client.post(f"{base}/turns/2/flag", json=UP, headers=auth)
        ).status_code == 201
        for target, status, reason in (
            (99, 400, "not_a_bot_turn"),
            (2, 409, "already_rated"),
        ):
            r = await w.client.post(
                f"{base}/turns",
                json={"text": "motivo", "contests_turn": target},
                headers=auth,
            )
            assert r.status_code == status and err(r)["details"]["reason"] == reason
        r = await w.client.post(
            f"{base}/turns", json={"text": "  ", "contests_turn": 1}, headers=auth
        )
        assert r.status_code == 400 and err(r)["details"]["reason"] == "message_empty"
        # nothing was appended and only the thumbs up was recorded
        assert (await state_of(pg, s.session_id))[:2] == (1, 1)
