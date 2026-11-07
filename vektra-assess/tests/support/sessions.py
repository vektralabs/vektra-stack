"""Seed sessions in any lifecycle state through the REAL code paths (plan 8 M1.10, plan 9 e2e).

``seed_session`` mints and starts through the production API routes, then runs the production
pre-flight handler in-process with the recorded Moodle responses (4.3 by default) and the fake
Part B runner, so an ACTIVE session has a real snapshot, real keys, real opening turns and a real
hand-off row. OUTAGE and CLOSED are reached from ACTIVE with the plan-2 rules (the hand-off row
follows the state through its FK; ``assess_close_session`` closes), never by inserting a row in a
state the code could not have produced. ELIGIBLE is the absence of an open row for the student and
the activity, with the activity configured.

Every helper returns ids and tokens only; tests read content back through the API.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from assess_moodle_support import Replay, make_client, manifest, site_for, uid
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from test_context_capture import InProcessParser

from support.preflight import FakePartB, activate, deps, job_context, kek_ring
from support.world import World, configure_activity
from vektra_assess.worker.preflight import make_preflight_handler
from vektra_assess.worker.start_source import MoodleStartSource

State = Literal["ELIGIBLE", "STARTING", "ACTIVE", "OUTAGE", "CLOSED"]

BRANCH = "4.3"


def fixture_target(
    branch: str = BRANCH, name: str = "rubric_vis", user: str = "stu1"
) -> tuple[int, int]:
    """(student userid, cmid) of a recorded, fully graded assignment."""
    m = manifest(branch)
    return uid(branch, user), int(m["assigns"][name]["cmid"])


@dataclass(frozen=True)
class Seeded:
    session_id: uuid.UUID | None
    sub: int
    cmid: int
    session_token: str | None
    kek: Any = None  # the KEK the session key was wrapped under (worker-side tests)


async def _site_release(w: World, branch: str) -> None:
    # the capture reads base_url from the DB: on a live run (test_req073_live) it must be the
    # dev site's URL, not the world's placeholder; hermetic runs get the recordings' root
    await w.pg.run(
        "super",
        "UPDATE assess_sites SET release = :r, base_url = :u WHERE id = :s",
        {"r": manifest(branch)["release"], "u": site_for(branch).base_url, "s": w.site},
    )


async def seed_session(
    w: World,
    worker: AsyncEngine,
    state: State = "ACTIVE",
    *,
    branch: str = BRANCH,
    assignment: str = "rubric_vis",
    user: str = "stu1",
    cap_minutes: int = 30,
    runner: Any | None = None,
    close_reason: str = "finished",
    kek: Any | None = None,
) -> Seeded:
    sub, cmid = fixture_target(branch, assignment, user)
    await _site_release(w, branch)
    await configure_activity(w.pg, w.site, cmid, cap_minutes=cap_minutes)
    if state == "ELIGIBLE":
        return Seeded(None, sub, cmid, None)
    token = await w.presession(sub=sub, cmid=cmid)
    r = await w.client.post(
        "/api/v1/assess/sessions", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 202, r.text
    sid = uuid.UUID(r.json()["session_id"])
    session_token = str(r.json()["token"])
    if state == "STARTING":
        return Seeded(sid, sub, cmid, session_token)
    if not (
        await w.pg.run(
            "super",
            "SELECT count(*) FROM assess_model_activations WHERE model = 'fake/part-b'",
        )
    )[0][0]:
        await activate(w.pg)
    source = MoodleStartSource(
        make_client(Replay(branch)), InProcessParser(), worker.connect
    )
    kek = kek or kek_ring()
    handler = make_preflight_handler(
        deps(w.settings, w.handoff_public, source, runner or FakePartB(), kek=kek)
    )
    await handler(job_context(worker, sid))
    committed = await w.pg.run(
        "super", "SELECT state FROM assess_sessions WHERE id = :s", {"s": sid}
    )
    assert committed and committed[0][0] == "ACTIVE", (
        "seeding: the pre-flight did not commit"
    )
    if state == "OUTAGE":
        # through the API's own transition (M4.1): an interval row and its audit event
        epoch = (
            await w.pg.run(
                "super", "SELECT epoch FROM assess_sessions WHERE id = :s", {"s": sid}
            )
        )[0][0]
        got = await w.pg.run(
            "api",
            "SELECT assess_enter_outage(:s, :e, now(), 'first_token_timeout')",
            {"s": sid, "e": epoch},
        )
        assert got[0][0] == "outage", got
    elif state == "CLOSED":
        async with worker.begin() as conn:
            ok = (
                await conn.execute(
                    text("SELECT assess_close_session(:s, 1, :r)"),
                    {"s": sid, "r": close_reason},
                )
            ).scalar()
            assert ok is True
    return Seeded(sid, sub, cmid, session_token, kek)
