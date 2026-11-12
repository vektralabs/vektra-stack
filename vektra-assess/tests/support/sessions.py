"""Seed sessions in any lifecycle state through the REAL code paths (plan 8 M1.10, plan 9 e2e).

``seed_session`` mints and starts through the production API routes, then runs the production
pre-flight handler in-process with ``LmsStartSource`` over the fake LMS provider's scenario world
(``support.fake_lms.scenario_world``, ADR-0033 / plan 2a-2b.1) and the fake Part B runner, so an
ACTIVE session has a real snapshot, real keys, real opening turns and a real hand-off row. OUTAGE and CLOSED are reached from ACTIVE with the plan-2 rules (the hand-off row
follows the state through its FK; ``assess_close_session`` closes), never by inserting a row in a
state the code could not have produced. ELIGIBLE is the absence of an open row for the student and
the activity, with the activity configured.

Every helper returns ids and tokens only; tests read content back through the API.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from support.fake_lms import (
    SCENARIO_RELEASE,
    scenario_kind,
    scenario_provider,
    scenario_target,
)
from support.parser import InProcessParser
from support.preflight import FakePartB, activate, deps, job_context, kek_ring
from support.world import World, configure_activity
from vektra_assess.worker.preflight import make_preflight_handler
from vektra_assess.worker.start_source import LmsStartSource

State = Literal["ELIGIBLE", "STARTING", "ACTIVE", "OUTAGE", "CLOSED"]

#: the base URL the scenario site is registered with
BASE_URL = "https://lms.example.org"


def fixture_target(name: str = "rubric_vis", user: str = "stu1") -> tuple[int, int]:
    """(student user id, cmid) of a scenario activity (by default a fully graded,
    released, rubric-graded assignment)."""
    return scenario_target(name, user)


def lms_source(
    worker: AsyncEngine, provider: Any | None = None, **kw: Any
) -> LmsStartSource:
    """The production start source over the fake provider (a new scenario world unless given)."""
    return LmsStartSource(
        provider if provider is not None else scenario_provider(),
        InProcessParser(),
        worker.connect,
        **kw,
    )


async def site_release(
    w: World, release: str = SCENARIO_RELEASE, base_url: str = BASE_URL
) -> None:
    """The site's release and base URL, as a setup check would have recorded them."""
    await w.pg.run(
        "super",
        "UPDATE assess_sites SET release = :r, base_url = :u WHERE id = :s",
        {"r": release, "u": base_url, "s": w.site},
    )


@dataclass(frozen=True)
class Seeded:
    session_id: uuid.UUID | None
    sub: int
    cmid: int
    session_token: str | None
    kek: Any = None  # the KEK the session key was wrapped under (worker-side tests)


async def seed_session(
    w: World,
    worker: AsyncEngine,
    state: State = "ACTIVE",
    *,
    assignment: str = "rubric_vis",
    user: str = "stu1",
    cap_minutes: int = 30,
    runner: Any | None = None,
    close_reason: str = "finished",
    kek: Any | None = None,
    provider: Any | None = None,
    target: tuple[int, int] | None = None,
    release: str = SCENARIO_RELEASE,
    base_url: str = BASE_URL,
) -> Seeded:
    """``assignment`` names a scenario activity (an assignment, or the quiz ``essay``);
    ``provider`` lets a test script or inspect the fake LMS the pre-flight reads, or pass any
    other ``LmsProvider`` (a live suite: then ``target``, ``release`` and ``base_url`` name
    the live site's student and assignment)."""
    sub, cmid = target if target is not None else fixture_target(assignment, user)
    await site_release(w, release, base_url)
    await configure_activity(
        w.pg, w.site, cmid, kind=scenario_kind(assignment), cap_minutes=cap_minutes
    )
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
    source = lms_source(worker, provider)
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
