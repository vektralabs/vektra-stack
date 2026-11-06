"""M2.4: the fixed gate order, proven by a spy on the database function.

Every statement the API engine sends is recorded (SQLAlchemy ``before_cursor_execute``); a gate
failure at steps 1-4 must reach ``get_session_material`` ZERO times, on every session route that
decrypts (``GET /sessions/{sid}``, ``POST /sessions/{sid}/turns``). A valid request is the positive
control (the spy is not vacuous). Steps 5 (deadline / outage cap) and 6 (enrolment freshness) are
bound by plan M3.4 / M3.7, whose tests extend this table.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import jwt
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakeStreamRunner
from support.sessions import seed_session
from support.world import world

from integration.test_session_preflight import _reset

pytestmark = pytest.mark.integration


@contextmanager
def spy(engine) -> Iterator[list[str]]:  # type: ignore[no-untyped-def]
    calls: list[str] = []

    def record(conn, cursor, statement, params, context, executemany):  # type: ignore[no-untyped-def]
        if "get_session_material" in statement:
            calls.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield calls
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


def forged(w, claims_over: dict, key: bytes | None = None) -> str:  # type: ignore[no-untyped-def]
    """A session token re-signed with the world's key (or another key) and altered claims."""
    return jwt.encode(
        claims_over,
        key if key is not None else w.signing.active_key,
        algorithm="HS256",
        headers={"kid": w.signing.active},
    )


async def _cases(w, s, pg):  # type: ignore[no-untyped-def]
    base = jwt.decode(s.session_token, options={"verify_signature": False})
    other_sid = uuid.uuid4()
    yield "1: bad signature", forged(w, base, os.urandom(32)), s.session_id
    yield (
        "1: pre-session token on a session route",
        await w.presession(sub=s.sub, cmid=s.cmid),
        s.session_id,
    )
    yield "2: token bound to another sid", s.session_token, other_sid
    yield "3: not the owner", forged(w, {**base, "sub": str(s.sub + 1)}), s.session_id
    yield "3: another activity", forged(w, {**base, "cmid": s.cmid + 1}), s.session_id
    yield (
        "3: another site",
        forged(w, {**base, "site": str(w.other_site)}),
        s.session_id,
    )
    yield "3: another session number", forged(w, {**base, "seq": 2}), s.session_id
    yield "3: revoked epoch", forged(w, {**base, "epoch": 0}), s.session_id


@pytest.mark.parametrize("route", ["get", "turn"])
async def test_no_gate_failure_reaches_the_material(pg, worker, route: str) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, assess_runner=FakeStreamRunner()) as w:
        s = await seed_session(w, worker, "ACTIVE")

        async def call(token: str, sid):  # type: ignore[no-untyped-def]
            headers = {"Authorization": f"Bearer {token}"}
            if route == "get":
                return await w.client.get(
                    f"/api/v1/assess/sessions/{sid}", headers=headers
                )
            return await w.client.post(
                f"/api/v1/assess/sessions/{sid}/turns",
                json={"text": "x"},
                headers=headers,
            )

        async for name, token, sid in _cases(w, s, pg):
            if route == "get" and name.startswith("1: pre-session"):
                continue  # on GET a pre-session token is the resume path, by design (M1.7)
            with spy(w.engine) as calls:
                r = await call(token, sid)
            assert r.status_code in (401, 404, 409), (name, r.status_code)
            assert calls == [], (
                f"{name}: get_session_material called {len(calls)} times"
            )
        # 4: state. CLOSED and (for a turn) a non-ACTIVE state stop before the material
        await pg.run(
            "super",
            "UPDATE assess_sessions SET state = 'OUTAGE' WHERE id = :s",
            {"s": s.session_id},
        )
        if route == "turn":
            with spy(w.engine) as calls:
                r = await call(s.session_token, s.session_id)
            assert r.status_code == 409 and calls == [], "4: OUTAGE turn"
        await pg.run(
            "super",
            "UPDATE assess_sessions SET state = 'ACTIVE' WHERE id = :s",
            {"s": s.session_id},
        )
        # positive control: a valid request does reach the material (the spy works)
        with spy(w.engine) as calls:
            r = await call(s.session_token, s.session_id)
        assert r.status_code in (200, 202), r.text
        assert len(calls) >= 1
        driver = getattr(w.app.state, "assess_turn_driver", None)
        if driver is not None:
            for task in list(driver.tasks.values()):
                await task
        from sqlalchemy import text

        async with worker.begin() as conn:
            await conn.execute(
                text("SELECT assess_close_session(:s, 1, 'finished')"),
                {"s": s.session_id},
            )
        with spy(w.engine) as calls:
            r = await call(s.session_token, s.session_id)
        assert r.status_code == 409 and calls == [], "4: CLOSED"


async def test_every_rejection_is_audited_and_changes_nothing(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, assess_runner=FakeStreamRunner()) as w:
        s = await seed_session(w, worker, "ACTIVE")
        before = await pg.run(
            "super",
            "SELECT state, seq, epoch, pending_reply_since FROM assess_sessions WHERE id = :s",
            {"s": s.session_id},
        )
        rejections = (
            "SELECT count(*) FROM assess_audit_events WHERE event IN "
            "('assess_in_session_gate_rejected', 'assess_ownership_mismatch_rejected')"
        )
        audits_before = (await pg.run("super", rejections))[0][0]
        n = 0
        async for _name, token, sid in _cases(w, s, pg):
            await w.client.post(
                f"/api/v1/assess/sessions/{sid}/turns",
                json={"text": "x"},
                headers={"Authorization": f"Bearer {token}"},
            )
            n += 1
        after = await pg.run(
            "super",
            "SELECT state, seq, epoch, pending_reply_since FROM assess_sessions WHERE id = :s",
            {"s": s.session_id},
        )
        assert [tuple(r) for r in after] == [tuple(r) for r in before]
        audits_after = (await pg.run("super", rejections))[0][0]
        assert audits_after - audits_before == n  # exactly one audit row per rejection
