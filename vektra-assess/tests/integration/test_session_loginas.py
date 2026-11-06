"""M2.5 / REQ-084: a token that does not assert ``loginas: false`` is refused everywhere.

The mint side (the positive body assertion) is in ``test_session_mint.py``. Here: a token signed
with the REAL key (so the signature passes) but carrying ``loginas: true`` or no ``loginas`` claim
at all is refused with 403 ``ERR-ASSESS-006`` on Start, ``/state``, the session read and a turn;
each refusal writes one ``assess_loginas_rejected`` audit row, no session row is created and the
record of an existing session is unchanged.
"""

from __future__ import annotations

import time
import uuid

import jwt
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakeStreamRunner
from support.sessions import seed_session
from support.world import configure_activity, err, world

from integration.test_session_preflight import _reset
from vektra_assess.tokens import AUDIENCE

pytestmark = pytest.mark.integration


async def n(pg, sql: str, **params) -> int:  # type: ignore[no-untyped-def]
    return int((await pg.run("super", sql, params))[0][0])


def signed(w, claims: dict) -> str:  # type: ignore[no-untyped-def]
    now = int(time.time())
    body = {
        "aud": AUDIENCE,
        "iss": "vektra-assess",
        "iat": now,
        "exp": now + 60,
        **claims,
    }
    body = {k: v for k, v in body.items() if v is not None}
    return jwt.encode(
        body, w.signing.active_key, algorithm="HS256", headers={"kid": w.signing.active}
    )


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


@pytest.mark.parametrize("loginas", [True, None, 1, "false"])
async def test_a_loginas_presession_token_starts_nothing(pg, worker, loginas) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        await configure_activity(pg, w.site, 777)
        token = signed(
            w,
            {
                "sub": "321",
                "site": str(w.site),
                "cmid": 777,
                "jti": "forged-jti-0001",
                "stage": "pre",
                "loginas": loginas,
            },
        )
        before = await n(
            pg,
            "SELECT count(*) FROM assess_audit_events WHERE event='assess_loginas_rejected' "
            "AND site_id = :s",
            s=w.site,
        )
        for method, path in (
            ("POST", "/api/v1/assess/sessions"),
            ("GET", "/api/v1/assess/state"),
        ):
            r = await w.client.request(
                method, path, headers={"Authorization": f"Bearer {token}"}
            )
            assert r.status_code == 403 and err(r)["code"] == "ERR-ASSESS-006", (
                path,
                r.text,
            )
        assert (
            await n(
                pg, "SELECT count(*) FROM assess_sessions WHERE student_userid = 321"
            )
            == 0
        )
        assert (
            await n(pg, "SELECT count(*) FROM assess_jobs WHERE kind = 'preflight'")
            == 0
        )
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_render_checks WHERE student_userid = 321",
            )
            == 0
        )
        after = await n(
            pg,
            "SELECT count(*) FROM assess_audit_events WHERE event='assess_loginas_rejected' "
            "AND site_id = :s",
            s=w.site,
        )
        assert after - before == 2


async def test_a_loginas_session_token_reads_and_writes_nothing(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, assess_runner=FakeStreamRunner()) as w:
        s = await seed_session(w, worker, "ACTIVE")
        claims = jwt.decode(s.session_token, options={"verify_signature": False})
        claims.update(loginas=True, jti=uuid.uuid4().hex)
        for key in ("aud", "iss", "iat", "exp"):
            claims.pop(key)
        token = signed(w, claims)
        record = "SELECT state, seq, epoch, pending_reply_since FROM assess_sessions WHERE id = :s"
        before = [tuple(r) for r in await pg.run("super", record, {"s": s.session_id})]
        url = f"/api/v1/assess/sessions/{s.session_id}"
        hdr = {"Authorization": f"Bearer {token}"}
        for r in (
            await w.client.get(url, headers=hdr),
            await w.client.post(f"{url}/turns", json={"text": "x"}, headers=hdr),
            await w.client.get(f"{url}/events", headers=hdr),
        ):
            assert r.status_code == 403 and "Sessione" not in r.text
        assert [
            tuple(r) for r in await pg.run("super", record, {"s": s.session_id})
        ] == before
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_turns WHERE session_id = :s",
                s=s.session_id,
            )
            == 2
        )
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE event='assess_loginas_rejected' "
                "AND site_id = :s",
                s=w.site,
            )
            == 3
        )
