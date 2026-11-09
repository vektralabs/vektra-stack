"""M3.9 / REQ-069: resume after a reload, two tabs, and what is left after the close."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakeStreamRunner
from support.sessions import seed_session
from support.world import err, world

from integration.test_session_preflight import _reset

pytestmark = pytest.mark.integration


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_reload_two_tabs_and_after_the_close(pg) -> None:  # type: ignore[no-untyped-def]
    await _reset(pg)
    worker = create_async_engine(pg.url("worker"))
    release = asyncio.Event()
    try:
        async with world(pg, assess_runner=FakeStreamRunner(release=release)) as w:
            s = await seed_session(w, worker, "ACTIVE")
            url = f"/api/v1/assess/sessions/{s.session_id}"
            deadline_before = (
                await pg.run(
                    "super",
                    "SELECT deadline_at FROM assess_sessions WHERE id=:s",
                    {"s": s.session_id},
                )
            )[0][0]
            # a reload: a fresh pre-session token resumes the SAME session with its full history
            pre = await w.presession(sub=s.sub, cmid=s.cmid)
            tab = (await w.client.get(url, headers=bearer(pre))).json()
            assert tab["session_id"] == str(s.session_id) and len(tab["turns"]) == 2
            now, dl = (
                datetime.fromisoformat(tab["server_now"]),
                datetime.fromisoformat(tab["deadline_at"]),
            )
            assert (
                abs(tab["remaining_s"] - (dl - now).total_seconds()) <= 1
            )  # server time, +-1 s
            # two tabs: one sends, the other's send is refused, no duplicate turn
            assert (
                await w.client.post(
                    f"{url}/turns",
                    json={"text": "uno"},
                    headers=bearer(s.session_token),
                )
            ).status_code == 202
            r = await w.client.post(
                f"{url}/turns", json={"text": "uno"}, headers=bearer(tab["token"])
            )
            assert r.status_code == 409 and err(r)["code"] == "ERR-ASSESS-010"
            assert (
                await pg.run(
                    "super",
                    "SELECT count(*) FROM assess_turns WHERE session_id=:s AND role='student'",
                    {"s": s.session_id},
                )
            )[0][0] == 1
            release.set()
            for task in list(w.app.state.assess_turn_driver.tasks.values()):
                await asyncio.wait_for(task, 15)
            # nothing the client did moved the deadline (own network drop: +0)
            assert (
                await pg.run(
                    "super",
                    "SELECT deadline_at FROM assess_sessions WHERE id=:s",
                    {"s": s.session_id},
                )
            )[0][0] == deadline_before
            # Fine; then a reload gets the closed message and no token, a new Start is refused
            assert (
                await w.client.post(f"{url}/finish", headers=bearer(tab["token"]))
            ).status_code == 200
            r = await w.client.get(
                url, headers=bearer(await w.presession(sub=s.sub, cmid=s.cmid))
            )
            assert r.status_code == 409 and "token" not in r.json()
            start = await w.client.post(
                "/api/v1/assess/sessions",
                headers=bearer(await w.presession(sub=s.sub, cmid=s.cmid)),
            )
            assert (
                start.status_code == 409
                and err(start)["details"]["reason"] == "already_discussed"
            )
            # a reset grant (plan 11) creates session #2
            await pg.run(
                "super",
                # the complete row the reset route writes (revision 0038; the reason is opaque here)
                "INSERT INTO assess_reset_grants (site_id, cmid, student_userid, granted_by, "
                "session_no, reason_ct, reason_kid) "
                "VALUES (:s, :c, :u, '41', 1, '\\x00', 't1')",
                {"s": w.site, "c": s.cmid, "u": s.sub},
            )
            start = await w.client.post(
                "/api/v1/assess/sessions",
                headers=bearer(await w.presession(sub=s.sub, cmid=s.cmid)),
            )
            assert start.status_code == 202 and start.json()["session_no"] == 2
    finally:
        await worker.dispose()
