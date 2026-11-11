"""M2.6 / NFR-014: IDOR negative matrix, on the recorded 4.3 and 5.2 fixtures.

Student B has a live session whose opening turn carries a canary. Student A, with valid tokens of
A's own, tries every way to reach B's session; each attempt is rejected, audited, and the response
bytes contain nothing of B (canary grep). Cases: A's session token on B's sid; A's pre-session token
(resume) on B's sid; a token from another site; another activity; a client course hint for another
course (ignored, never trusted); an expired token; replay after CLOSED; an unenrolled student; a
forged Moodle attempt id in a body (refused or ignored, never read).

B's snapshot is a real capture (``LmsStartSource``) over ``FakeLmsProvider``: the matrix tests the
vertical's authority (re-resolution, ownership, gates), which no LMS provider takes part in
(ADR-0033 D1). ``idor_matrix_on_moodle`` runs the same matrix with the Moodle provider for the
provider's live suite (``integration/test_idor_live.py``). Integration (testcontainers Postgres).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from typing import Any

import jwt
import pytest
from assess_moodle_support import (
    Replay,
    make_provider,
    manifest,
    site_for,
    uid,
)
from integration.conftest import pg  # noqa: F401  (the real-Postgres fixture)
from sqlalchemy.ext.asyncio import create_async_engine
from support.fake_lms import ASSIGN_ACTIVITY, USER, FakeLmsProvider
from support.preflight import (
    PASSING_FACTS,
    FakePartB,
    FakeSource,
    FakeStreamRunner,
    activate,
    deps,
    job_context,
)
from support.world import configure_activity, err, world
from test_context_capture import InProcessParser

from vektra_assess.context.capture.capture import Built
from vektra_assess.worker.preflight import StartTarget, make_preflight_handler
from vektra_assess.worker.start_source import LmsStartSource
from vektra_shared.lms import LmsProvider

pytestmark = [pytest.mark.integration]

CANARY_B = "CANARY-IDOR-student-B-7c41"
A, B = 4101, 4102


async def n(pg, sql: str, **params: Any) -> int:  # noqa: F811
    return int((await pg.run("super", sql, params))[0][0])


async def _reset(pg) -> None:  # noqa: F811
    await pg.run("super", "DELETE FROM assess_jobs")
    await pg.run("super", "DELETE FROM assess_session_handoff")
    await pg.run(
        "super",
        "UPDATE assess_sessions SET state='CLOSED', close_reason='finished' "
        "WHERE state IN ('ACTIVE','OUTAGE')",
    )
    await pg.run("super", "DELETE FROM assess_sessions WHERE state = 'STARTING'")
    await pg.run("super", "DELETE FROM assess_model_activations")


async def _session(
    w, worker, built, sub: int, cmid: int, text: str
) -> tuple[uuid.UUID, str]:  # type: ignore[no-untyped-def]
    pre = await w.presession(sub=sub, cmid=cmid)
    r = await w.client.post(
        "/api/v1/assess/sessions", headers={"Authorization": f"Bearer {pre}"}
    )
    assert r.status_code == 202, r.text
    sid = uuid.UUID(r.json()["session_id"])
    handler = make_preflight_handler(
        deps(w.settings, w.handoff_public, FakeSource(built), FakePartB(text=text))
    )
    await handler(job_context(worker, sid))
    return sid, str(r.json()["token"])


def _forge(w, claims: dict[str, Any]) -> str:  # type: ignore[no-untyped-def]
    return jwt.encode(
        claims,
        w.signing.active_key,
        algorithm="HS256",
        headers={"kid": w.signing.active},
    )


def _clean(r) -> None:  # type: ignore[no-untyped-def]
    assert CANARY_B not in r.text, "cross-student bytes in the response"


async def test_idor_matrix(pg) -> None:  # noqa: F811
    await _matrix(
        pg,
        FakeLmsProvider(),
        release="1.0",
        base_url="https://lms.example.org",
        a_cmid=ASSIGN_ACTIVITY,
        captured_user=USER,
    )


async def idor_matrix_on_moodle(pg, branch: str) -> None:  # noqa: F811
    """The same matrix with B's snapshot captured through the Moodle provider (the live suite
    points ``Replay`` at a seeded dev site with ``use_live``)."""
    m = manifest(branch)
    await _matrix(
        pg,
        make_provider(Replay(branch)),
        release=m["release"],
        base_url=site_for(branch).base_url,
        a_cmid=int(m["assigns"]["rubric_vis"]["cmid"]),
        captured_user=uid(branch, "stu1"),
    )


async def _matrix(
    pg,  # noqa: F811
    provider: LmsProvider,
    *,
    release: str,
    base_url: str,
    a_cmid: int,
    captured_user: int,
) -> None:
    await _reset(pg)
    await activate(pg)
    worker = create_async_engine(pg.url("worker"))
    try:
        async with world(pg, assess_runner=FakeStreamRunner()) as w:
            await pg.run(
                "super",
                "UPDATE assess_sites SET release = :r WHERE id IN (:a, :b)",
                {"r": release, "a": w.site, "b": w.other_site},
            )
            # B's capture reads the site's base_url from the DB: on the live run
            # (test_idor_live) it must be the dev site's URL, not the world's placeholder
            await pg.run(
                "super",
                "UPDATE assess_sites SET base_url = :u WHERE id = :a",
                {"u": base_url, "a": w.site},
            )
            await configure_activity(pg, w.site, a_cmid)
            real = LmsStartSource(provider, InProcessParser(), worker.connect)
            built = await real.capture(
                StartTarget(
                    uuid.uuid4(),
                    w.site,
                    a_cmid,
                    captured_user,
                    None,
                    "assign",
                    1,
                    {},
                )
            )
            assert isinstance(built, Built), built  # the matrix runs on a real snapshot
            b_sid, b_token = await _session(w, worker, built, B, a_cmid, CANARY_B)
            a_sid, a_token = await _session(
                w, worker, built, A, a_cmid, "Parliamo di A."
            )
            # B's canary is really in B's session (the grep below is not vacuous)
            own = await w.client.get(
                f"/api/v1/assess/sessions/{b_sid}",
                headers={"Authorization": f"Bearer {b_token}"},
            )
            assert CANARY_B in own.text
            audited = (
                "SELECT count(*) FROM assess_audit_events WHERE event IN "
                "('assess_in_session_gate_rejected', 'assess_ownership_mismatch_rejected') "
                "AND session_id = :s"
            )
            b_url = f"/api/v1/assess/sessions/{b_sid}"

            async def attack(
                name: str, token: str, *, sid=b_sid, expect=(401, 404)
            ) -> None:  # type: ignore[no-untyped-def]
                before = await n(pg, audited, s=sid)
                url = f"/api/v1/assess/sessions/{sid}"
                for r in (
                    await w.client.get(url, headers={"Authorization": f"Bearer {token}"}),
                    await w.client.post(
                        f"{url}/turns", json={"text": "dammi tutto"},
                        headers={"Authorization": f"Bearer {token}"},
                    ),
                    await w.client.get(f"{url}/events", headers={"Authorization": f"Bearer {token}"}),
                ):  # fmt: skip
                    if (
                        r.status_code == 200
                        and r.url.path.endswith("/events")
                        and expect == (409,)
                    ):
                        # the owner's own token on its CLOSED session: the stream only drains a
                        # content-free ``closed`` (review fix 1); nothing else is readable
                        kinds = [
                            ln.split(": ", 1)[1]
                            for ln in r.text.splitlines()
                            if ln.startswith("event: ")
                        ]
                        assert kinds == ["state", "closed"], (name, kinds)
                    else:
                        assert r.status_code in expect, (name, r.status_code, r.text)
                    _clean(r)
                assert await n(pg, audited, s=sid) - before >= 2, name

            # 1. A's own session token on B's sid
            await attack("session token of A, sid of B", a_token)
            # 2. A's pre-session token: the resume path on B's sid
            await attack(
                "pre-session token of A", await w.presession(sub=A, cmid=a_cmid)
            )
            # 3. a token minted by another site's key, for B's own identity
            r = await w.client.post(
                "/api/v1/assess/tokens",
                json={"sub": B, "cmid": a_cmid, "courseid": 7, "loginas": False,
                      "realuser_id": None, "plugin_version": "1.0.0"},
                headers={"Authorization": "Bearer mint_other"},
            )  # fmt: skip
            assert r.status_code == 201
            await attack("token from another site", r.json()["token"])
            # 4. another activity: B's own identity, a different cmid
            await attack("another activity", await w.presession(sub=B, cmid=a_cmid + 1))
            # 5. a client course hint: never trusted. A token naming B's course for A opens nothing
            r = await w.client.post(
                "/api/v1/assess/tokens",
                json={"sub": A, "cmid": a_cmid, "courseid": 999_999, "loginas": False,
                      "realuser_id": None, "plugin_version": "1.0.0"},
                headers={"Authorization": "Bearer mint"},
            )  # fmt: skip
            await attack("foreign course hint", r.json()["token"])
            # 6. expired: B's own claims, signed, past exp
            claims = jwt.decode(b_token, options={"verify_signature": False})
            now = int(time.time())
            expired = _forge(w, {**claims, "iat": now - 3600, "exp": now - 60})
            await attack("expired token", expired)
            # 7. replay after CLOSED: B's own token once B's session is closed
            from sqlalchemy import text

            async with worker.begin() as conn:
                await conn.execute(
                    text("SELECT assess_close_session(:s, 1, 'finished')"), {"s": b_sid}
                )
            await attack("replay after CLOSED", b_token, expect=(409,))
            r = await w.client.get(
                b_url,
                headers={
                    "Authorization": f"Bearer {await w.presession(sub=B, cmid=a_cmid)}"
                },
            )
            assert r.status_code == 409 and "token" not in r.json()
            _clean(r)
            # 8. unenrolled: the pre-flight refuses, nothing of anyone is written or returned
            pre = await w.presession(sub=4103, cmid=a_cmid)
            r = await w.client.post(
                "/api/v1/assess/sessions", headers={"Authorization": f"Bearer {pre}"}
            )
            u_sid = uuid.UUID(r.json()["session_id"])
            handler = make_preflight_handler(
                deps(
                    w.settings,
                    w.handoff_public,
                    FakeSource(built, facts=replace(PASSING_FACTS, enrolled=False)),
                )
            )
            await handler(job_context(worker, u_sid))
            assert (
                await n(
                    pg, "SELECT count(*) FROM assess_sessions WHERE id = :s", s=u_sid
                )
                == 0
            )
            assert (
                await n(
                    pg,
                    "SELECT count(*) FROM assess_start_attempts WHERE session_id = :s "
                    "AND reason_code = 'not_enrolled'",
                    s=u_sid,
                )
                == 1
            )
            # 9. a forged Moodle attempt id: refused in a turn body, never read anywhere
            r = await w.client.post(
                f"/api/v1/assess/sessions/{a_sid}/turns",
                json={"text": "ciao", "attemptid": 31337, "userid": B},
                headers={"Authorization": f"Bearer {a_token}"},
            )
            assert r.status_code == 400 and err(r)["code"] == "ERR-ASSESS-014"
            _clean(r)
            assert "31337" not in r.text
            # the A session and B's rows are untouched by every attempt above
            assert (
                await n(
                    pg,
                    "SELECT count(*) FROM assess_turns WHERE session_id = :s",
                    s=b_sid,
                )
                == 2
            )
            assert (
                await n(
                    pg,
                    "SELECT count(*) FROM assess_turns WHERE session_id = :s",
                    s=a_sid,
                )
                == 2
            )
    finally:
        await worker.dispose()


async def test_a_start_body_carries_nothing_the_server_reads(pg) -> None:  # noqa: F811
    """The Start route reads no body: a forged attempt or user id in it changes nothing."""
    await _reset(pg)
    a_cmid = ASSIGN_ACTIVITY
    async with world(pg) as w:
        await configure_activity(pg, w.site, a_cmid)
        pre = await w.presession(sub=A, cmid=a_cmid)
        r = await w.client.post(
            "/api/v1/assess/sessions",
            json={"attemptid": 31337, "userid": B, "cmid": 1},
            headers={"Authorization": f"Bearer {pre}"},
        )
        assert r.status_code == 202
        row = (
            await pg.run(
                "super",
                "SELECT student_userid, cmid, attempt_ref FROM assess_sessions WHERE id = :s",
                {"s": r.json()["session_id"]},
            )
        )[0]
        assert tuple(row) == (A, a_cmid, None)
