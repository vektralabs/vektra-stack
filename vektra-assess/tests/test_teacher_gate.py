"""The activity-level teacher facts (plan assess-teacher-ops 11a), against the 4.3 / 5.2 recordings.

``MoodleStartSource.teacher_facts`` resolves the cmid INSIDE the hinted course and reads its
editors, graders and the REQ-077 enable facts (close date, team submission, grading method,
Moodle rubric). Expectations come from the seed manifest. The editors request is SYNTHETIC: the
recordings asked ``core_enrol_get_enrolled_users_with_capability`` for ``mod/assign:grade`` only,
so it is served from that recording with the capability renamed (the live read is BLOCKED in
11a, see the plan's Notes).
"""

from __future__ import annotations

import copy
import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from assess_moodle_support import BRANCHES, Replay, make_client, manifest, uid

from vektra_assess.jobs import JobKind
from vektra_assess.worker.runner import JobContext
from vektra_assess.worker.start_source import MoodleStartSource
from vektra_assess.worker.teacher_gate import make_teacher_gate_handler

pytestmark = pytest.mark.parametrize("branch", BRANCHES)

SITE = uuid.uuid4()


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def first(self) -> Any:
        return self._row


class _Conn:
    def __init__(self, release: str, sink: list[Any]) -> None:
        self.release = release
        self.sink = sink

    async def execute(self, stmt: Any, params: Any = None) -> _Result:
        self.sink.append((str(stmt), params))
        return _Result(
            SimpleNamespace(base_url="https://moodle.example.org", release=self.release)
        )

    def begin(self) -> Any:
        @asynccontextmanager
        async def tx():  # type: ignore[no-untyped-def]
            yield self

        return tx()


def _source(branch: str) -> tuple[Replay, MoodleStartSource, dict[str, Any], list[Any]]:
    m = manifest(branch)
    replay = Replay(branch)
    for key in [
        k
        for k in replay.responses
        if "core_enrol_get_enrolled_users_with_capability" in k
        and "mod/assign:grade" in k
    ]:
        body = copy.deepcopy(replay.responses[key])
        for row in body:
            row["capability"] = "moodle/course:manageactivities"
        replay.responses[
            key.replace("mod/assign:grade", "moodle/course:manageactivities")
        ] = body
    sink: list[Any] = []

    @asynccontextmanager
    async def acquire():  # type: ignore[no-untyped-def]
        yield _Conn(m["release"], sink)

    src = MoodleStartSource(make_client(replay), None, acquire)  # type: ignore[arg-type]
    return replay, src, m, sink


def _graders(branch: str, m: dict[str, Any]) -> list[int]:
    return sorted(uid(branch, k) for k in m["graders"])


async def test_assignment_facts(branch: str) -> None:
    replay, src, m, _ = _source(branch)
    course = int(m["courseid"])
    team = await src.teacher_facts(
        SITE, int(m["assigns"]["team"]["cmid"]), course, "assign"
    )
    assert team["resolved"] is True and team["courseid"] == course
    assert team["team_submission"] is True
    assert team["close_at"] == m["assigns"]["team"]["expected"]["cutoffdate"]
    assert team["editors"] == _graders(branch, m)  # synthetic: the graders recording
    assert team["graders"] == _graders(branch, m)
    nocut = await src.teacher_facts(
        SITE, int(m["assigns"]["nocutoff"]["cmid"]), course, "assign"
    )
    assert nocut["close_at"] == 0 and nocut["team_submission"] is False
    guide = await src.teacher_facts(
        SITE, int(m["assigns"]["guide"]["cmid"]), course, "assign"
    )
    assert guide["grading_method"] == "guide" and guide["moodle_rubric"] is False
    rubric = await src.teacher_facts(
        SITE, int(m["assigns"]["rubric_vis"]["cmid"]), course, "assign"
    )
    assert rubric["grading_method"] == "rubric" and rubric["moodle_rubric"] is True
    simple = await src.teacher_facts(
        SITE, int(m["assigns"]["simple"]["cmid"]), course, "assign"
    )
    assert simple["moodle_rubric"] is False
    # ids and flags only: no names, no roles, no addresses
    blob = json.dumps(team)
    assert "@" not in blob and "fullname" not in blob
    assert not replay.unmatched


async def test_quiz_facts(branch: str) -> None:
    _replay, src, m, _ = _source(branch)
    q = m["quizzes"]["types"]
    got = await src.teacher_facts(SITE, int(q["cmid"]), int(m["courseid"]), "quiz")
    assert got["resolved"] is True and got["activity_type"] == "quiz"
    assert got["close_at"] == q["expected"]["timeclose"]
    assert got["team_submission"] is False and got["grading_method"] is None


async def test_a_wrong_type_hint_never_resolves(branch: str) -> None:
    _replay, src, m, _ = _source(branch)
    # a quiz's cmid looked up among the course's assignments: not there
    got = await src.teacher_facts(
        SITE, int(m["quizzes"]["types"]["cmid"]), int(m["courseid"]), "assign"
    )
    assert got["resolved"] is False
    assert "editors" not in got and "graders" not in got


async def test_the_handler_stores_the_facts_with_their_hint(branch: str) -> None:
    _replay, src, m, sink = _source(branch)
    cmid = int(m["assigns"]["team"]["cmid"])

    @asynccontextmanager
    async def acquire():  # type: ignore[no-untyped-def]
        yield _Conn(m["release"], sink)

    handler = make_teacher_gate_handler(src)
    args = {
        "site_id": str(SITE),
        "cmid": cmid,
        "courseid": int(m["courseid"]),
        "activity_type": "assign",
    }
    import logging

    ctx = JobContext(1, JobKind.TEACHER_GATE, args, 1, acquire, logging.getLogger())
    await handler(ctx)
    (stored,) = [p for sql, p in sink if "assess_teacher_gate" in sql]
    facts = json.loads(stored["i"])
    assert facts["hint"] == [int(m["courseid"]), "assign"]
    assert facts["team_submission"] is True
    # malformed jobs are stale no-ops: nothing is written
    sink.clear()
    for bad in (
        {**args, "activity_type": "forum"},
        {**args, "cmid": "x"},
        {k: v for k, v in args.items() if k != "courseid"},
    ):
        await handler(
            JobContext(2, JobKind.TEACHER_GATE, bad, 1, acquire, logging.getLogger())
        )
    assert not [p for sql, p in sink if "assess_teacher_gate" in sql]
