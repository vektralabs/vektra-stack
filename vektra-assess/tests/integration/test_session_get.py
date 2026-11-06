"""M1.7: ``GET /sessions/{sid}`` (owner read and resume) on a real database, API role."""

from __future__ import annotations

import uuid

import jwt
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.sessions import seed_session
from support.world import err, world

pytestmark = pytest.mark.integration


async def n(pg, sql: str, **params) -> int:  # type: ignore[no-untyped-def]
    return int((await pg.run("super", sql, params))[0][0])


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    from integration.test_session_preflight import _reset

    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_owner_reads_state_deadline_server_time_and_turns(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "ACTIVE")
        r = await w.client.get(
            f"/api/v1/assess/sessions/{s.session_id}", headers=bearer(s.session_token)
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state"] == "ACTIVE" and body["session_no"] == 1
        assert body["deadline_at"] and body["server_now"] and not body["reply_pending"]
        assert [t["kind"] for t in body["turns"]] == ["opening", "opening"]
        assert "Sessione #1" in body["turns"][0]["text"]
        assert "token" not in body  # a plain read re-mints nothing
        assert r.headers["cache-control"] == "no-store"


async def test_starting_has_no_turns_and_closed_is_409(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "STARTING")
        r = await w.client.get(
            f"/api/v1/assess/sessions/{s.session_id}", headers=bearer(s.session_token)
        )
        assert r.status_code == 200 and r.json()["state"] == "STARTING"
        assert r.json()["turns"] == []
    from integration.test_session_preflight import _reset

    await _reset(pg)
    async with world(pg) as w:
        c = await seed_session(w, worker, "CLOSED")
        r = await w.client.get(
            f"/api/v1/assess/sessions/{c.session_id}", headers=bearer(c.session_token)
        )
        assert r.status_code == 409 and err(r)["code"] == "ERR-ASSESS-009"
        assert "opening" not in r.text and "Sessione" not in r.text
        # resume of a CLOSED session: 409, no token, the jti stays unconsumed
        pre = await w.presession(sub=c.sub, cmid=c.cmid)
        before = await n(
            pg, "SELECT count(*) FROM assess_token_jti WHERE consumed_at IS NOT NULL"
        )
        r = await w.client.get(
            f"/api/v1/assess/sessions/{c.session_id}", headers=bearer(pre)
        )
        assert r.status_code == 409 and "token" not in r.json()
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_token_jti WHERE consumed_at IS NOT NULL",
            )
            == before
        )


async def test_resume_consumes_the_jti_and_remints_a_token_capped_at_the_deadline(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "ACTIVE", cap_minutes=5)
        pre = await w.presession(sub=s.sub, cmid=s.cmid)
        r = await w.client.get(
            f"/api/v1/assess/sessions/{s.session_id}", headers=bearer(pre)
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["turns"]) == 2
        claims = jwt.decode(body["token"], options={"verify_signature": False})
        assert claims["stage"] == "session" and claims["sid"] == str(s.session_id)
        assert claims["epoch"] == 1  # resume never bumps the epoch: two tabs stay valid
        # 5 min cap + 60 s grace: never the 900 s session TTL
        assert claims["exp"] - claims["iat"] <= 5 * 60 + 60 + 2
        # the jti is consumed: the same pre-session token cannot resume twice
        again = await w.client.get(
            f"/api/v1/assess/sessions/{s.session_id}", headers=bearer(pre)
        )
        assert again.status_code == 401
        # the old and the new session tokens both still read (same epoch)
        for tok in (s.session_token, body["token"]):
            r = await w.client.get(
                f"/api/v1/assess/sessions/{s.session_id}", headers=bearer(tok)
            )
            assert r.status_code == 200


async def test_epoch_bump_revokes_old_tokens(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "ACTIVE")
        await pg.run(
            "super",
            "UPDATE assess_sessions SET epoch = 2 WHERE id = :s",
            {"s": s.session_id},
        )
        r = await w.client.get(
            f"/api/v1/assess/sessions/{s.session_id}", headers=bearer(s.session_token)
        )
        assert r.status_code == 401
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE session_id = :s "
                "AND event = 'assess_in_session_gate_rejected' AND reason_code = 'epoch'",
                s=s.session_id,
            )
            == 1
        )


async def test_another_students_sid_is_404_audited_and_leaks_nothing(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        a = await seed_session(w, worker, "ACTIVE", user="stu1")
        # B's token is a valid session token for B's own session; it never opens A's
        b_sid = uuid.uuid4()
        pre_b = await w.presession(sub=a.sub + 1, cmid=a.cmid)
        r = await w.client.get(
            f"/api/v1/assess/sessions/{a.session_id}", headers=bearer(pre_b)
        )
        assert r.status_code == 404 and err(r)["code"] == "ERR-ASSESS-008"
        assert "Sessione" not in r.text
        r = await w.client.get(
            f"/api/v1/assess/sessions/{b_sid}", headers=bearer(a.session_token)
        )
        assert r.status_code == 404
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_audit_events "
                "WHERE event = 'assess_ownership_mismatch_rejected'",
            )
            >= 2
        )
