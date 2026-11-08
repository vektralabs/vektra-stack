"""Seeding helpers of the teacher-operation tests (plan assess-teacher-ops 11b).

Sessions are reached through the REAL code paths: mint and Start through the production routes,
then the production pre-flight handler. ``live_sessions`` reuses one real capture (stu1's recorded
submission) for several fake students, as the in-process burst does, so an activity can hold N
ACTIVE sessions with real snapshots, keys and hand-off rows. The worker-written facts
(``assess_teacher_gate``, ``assess_gate_inputs``) are seeded as the worker would write them.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from assess_moodle_support import Replay, make_client, manifest, site_for
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from test_context_capture import InProcessParser

from support.preflight import FakePartB, FakeSource, activate, deps, job_context
from support.sessions import BRANCH, fixture_target
from support.world import World, auth, configure_activity
from vektra_assess.keys import AssessKeyring
from vektra_assess.transcript import store
from vektra_assess.worker.preflight import StartTarget, make_preflight_handler
from vektra_assess.worker.start_source import MoodleStartSource

GRADER, NOT_GRADER, COURSE = 41, 42, 7
EDITOR = 31  # the editor of test_teacher_authz.seed_gate's defaults


def transcript_ring(kid: str = "t1") -> AssessKeyring:
    return AssessKeyring(store.KEYRING, kid, {kid: os.urandom(32)})


def with_transcript_key(w: World, ring: AssessKeyring | None = None) -> AssessKeyring:
    ring = ring or transcript_ring()
    w.app.state.assess_keys[store.KEYRING] = ring
    return ring


async def seed_teacher_gate(
    pg: Any,
    site: uuid.UUID,
    cmid: int,
    *,
    age: str = "1 second",
    kind: str = "assign",
    **facts: Any,
) -> None:
    """The worker-written activity facts: GRADER grades, EDITOR edits (override per test)."""
    inputs = {
        "resolved": True,
        "courseid": COURSE,
        "activity_type": kind,
        "editors": [EDITOR],
        "graders": [GRADER],
        "close_at": 1_700_000_000,
        "team_submission": False,
        "grading_method": "rubric" if kind == "assign" else None,
        "moodle_rubric": kind == "assign",
        "override_holder": None,
        "override_viewers": None,
        "hint": [COURSE, kind],
        **facts,
    }
    await pg.run(
        "worker",
        "INSERT INTO assess_teacher_gate (site_id, cmid, inputs, refreshed_at) VALUES "
        f"(:s, :c, CAST(:i AS jsonb), now() - interval '{age}') ON CONFLICT (site_id, cmid) "
        "DO UPDATE SET inputs = EXCLUDED.inputs, refreshed_at = EXCLUDED.refreshed_at",
        {"s": site, "c": cmid, "i": json.dumps(inputs)},
    )


async def seed_render(
    pg: Any,
    site: uuid.UUID,
    student: int,
    cmid: int,
    *,
    moodle_reasons: list[str] | None = None,
    age: str = "1 second",
) -> None:
    """A render evaluation as the worker stores it (``moodle_reasons``: the Moodle half)."""
    inputs: dict[str, Any] = {
        "code": "already_discussed",
        "state": "closed",
        "opens_at": None,
        "session_no_next": 2,
    }
    if moodle_reasons is not None:
        inputs["moodle_reasons"] = moodle_reasons
    await pg.run(
        "worker",
        "INSERT INTO assess_gate_inputs (site_id, student_userid, cmid, inputs, refreshed_at) "
        f"VALUES (:s, :u, :c, CAST(:i AS jsonb), now() - interval '{age}') "
        "ON CONFLICT (site_id, student_userid, cmid) DO UPDATE SET inputs = EXCLUDED.inputs, "
        "refreshed_at = EXCLUDED.refreshed_at",
        {"s": site, "u": student, "c": cmid, "i": json.dumps(inputs)},
    )


async def site_release(w: World) -> None:
    await w.pg.run(
        "super",
        "UPDATE assess_sites SET release = :r, base_url = :u WHERE id = :s",
        {"r": manifest(BRANCH)["release"], "u": site_for(BRANCH).base_url, "s": w.site},
    )


async def start(w: World, student: int, cmid: int) -> tuple[uuid.UUID, str]:
    """Mint and Start through the routes: a STARTING session (pre-flight not run)."""
    token = await w.presession(sub=student, cmid=cmid)
    r = await w.client.post(
        "/api/v1/assess/sessions", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 202, r.text
    return uuid.UUID(r.json()["session_id"]), str(r.json()["token"])


async def live_sessions(
    w: World, worker: AsyncEngine, students: list[int], *, cap_minutes: int = 30
) -> tuple[int, dict[int, tuple[uuid.UUID, str]]]:
    """(cmid, {student: (session id, session token)}): every student ACTIVE on the recorded
    assignment, through Start and the real pre-flight (one real capture reused)."""
    sub, cmid = fixture_target()
    await site_release(w)
    await configure_activity(w.pg, w.site, cmid, cap_minutes=cap_minutes)
    if not (
        await w.pg.run(
            "super",
            "SELECT count(*) FROM assess_model_activations WHERE model = 'fake/part-b'",
        )
    )[0][0]:
        await activate(w.pg)
    real = MoodleStartSource(
        make_client(Replay(BRANCH)), InProcessParser(), worker.connect
    )
    built = await real.capture(
        StartTarget(uuid.uuid4(), w.site, cmid, sub, None, "assign", 1, {})
    )
    handler = make_preflight_handler(
        deps(w.settings, w.handoff_public, FakeSource(built), FakePartB())
    )
    out: dict[int, tuple[uuid.UUID, str]] = {}
    for student in students:
        sid, token = await start(w, student, cmid)
        await handler(job_context(worker, sid))
        state = await w.pg.run(
            "super", "SELECT state FROM assess_sessions WHERE id = :s", {"s": sid}
        )
        assert state[0][0] == "ACTIVE", "seeding: the pre-flight did not commit"
        out[student] = (sid, token)
    return cmid, out


async def close(worker: AsyncEngine, sid: uuid.UUID, reason: str = "finished") -> None:
    async with worker.begin() as conn:
        epoch = (
            await conn.execute(
                text("SELECT epoch FROM assess_sessions WHERE id = :s"), {"s": sid}
            )
        ).scalar()
        ok = (
            await conn.execute(
                text("SELECT assess_close_session(:s, :e, :r)"),
                {"s": sid, "e": epoch, "r": reason},
            )
        ).scalar()
        assert ok is True


async def count(pg: Any, sql: str, **params: Any) -> int:
    return int((await pg.run("super", sql, params))[0][0])


def body(**over: Any) -> dict[str, Any]:
    return {
        "actor_userid": GRADER,
        "courseid": COURSE,
        "activity_type": "assign",
        **over,
    }


def teacher() -> dict[str, str]:
    return auth("teacher")


def list_headers(actor: int = GRADER, key: str = "teacher") -> dict[str, str]:
    return {
        **auth(key),
        "X-Assess-Actor": str(actor),
        "X-Assess-Course": str(COURSE),
        "X-Assess-Activity-Type": "assign",
    }
