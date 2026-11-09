"""The activity-level teacher facts (plan assess-teacher-ops 11a), against the 4.3 / 5.2 recordings.

``MoodleStartSource.teacher_facts`` resolves the cmid INSIDE the hinted course and reads its
editors, graders and the REQ-077 enable facts (close date, team submission, grading method,
Moodle rubric) and, for a quiz, the override facts of the REQ-076 gate panel (11b). Expectations
come from the seed manifest. The editors and override-managers requests are SYNTHETIC: the
recordings asked ``core_enrol_get_enrolled_users_with_capability`` for ``mod/assign:grade`` only,
so they are served from that recording with the capability renamed (the live reads are BLOCKED,
see the plan's Notes).
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
VIEWER_OFFSET = 10_000


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def first(self) -> Any:
        return self._row

    def scalar(self) -> Any:
        return "2026-11-08T00:00:00+00:00"  # the clock read before the Moodle reads


class _Conn:
    def __init__(self, release: str, sink: list[Any]) -> None:
        self.release = release
        self.sink = sink

    async def execute(self, stmt: Any, params: Any = None) -> _Result:
        self.sink.append((str(stmt), params))
        return _Result(
            SimpleNamespace(base_url="https://moodle.example.org", release=self.release)
        )

    async def rollback(self) -> None:
        return None

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
        for capability in (
            "moodle/course:manageactivities",
            "mod/quiz:manageoverrides",
        ):
            body = copy.deepcopy(replay.responses[key])
            for row in body:
                row["capability"] = capability
                if capability == "mod/quiz:manageoverrides":
                    # other user ids than the graders': a read that returned the graders
                    # instead would not pass for the override viewers
                    for user in row["users"]:
                        user["id"] += VIEWER_OFFSET
            replay.responses[key.replace("mod/assign:grade", capability)] = body
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
    # 11b: the graders (the list, reset, terminate, extra time, opening fallback) are read
    assert team["graders"] == _graders(branch, m)
    # an assignment's participant closes are never listable (OQ-025): no holder, no viewers
    assert team["override_holder"] is None and team["override_viewers"] is None
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
    assert got["graders"] == _graders(branch, m)
    # synthetic: the graders recording, renamed, with shifted ids
    assert got["override_viewers"] == [g + VIEWER_OFFSET for g in _graders(branch, m)]
    assert not set(got["override_viewers"]) & set(got["graders"])
    assert got["override_holder"] is None  # no override later than the quiz's own close


async def test_the_override_holder_is_read_only_where_overrides_are(
    branch: str,
) -> None:
    """REQ-076 gate panel: on 5.2 the user override that sets the latest close names its holder
    (``review_all``: stu2's override beats the G2 group override); on 4.3 the overrides are
    unreadable and nobody is named."""
    _replay, src, m, _ = _source(branch)
    q = m["quizzes"]["review_all"]
    got = await src.teacher_facts(SITE, int(q["cmid"]), int(m["courseid"]), "quiz")
    if m["ws_get_overrides_available"]:
        assert got["override_holder"] == uid(branch, "stu2")
    else:
        assert got["override_holder"] is None
    # ids and flags only: never an override's reason, a name or a date of another student
    assert set(got) == {
        "resolved", "courseid", "activity_type", "editors", "graders", "close_at",
        "team_submission", "grading_method", "moodle_rubric", "override_holder",
        "override_viewers", "automatic_close_at",
    }  # fmt: skip


async def test_a_wrong_type_hint_never_resolves(branch: str) -> None:
    _replay, src, m, _ = _source(branch)
    # a quiz's cmid looked up among the course's assignments: not there
    got = await src.teacher_facts(
        SITE, int(m["quizzes"]["types"]["cmid"]), int(m["courseid"]), "assign"
    )
    assert got["resolved"] is False
    # the one DEFINITIVE unresolved reason the API reports as "not an editor" (review 3)
    assert got["reason"] == "activity_not_in_course"
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


def test_the_automatic_close_is_the_start_gates_formula(branch: str) -> None:
    """CodeRabbit 4506916932: the teacher pages show the opening Start waits for: the latest of
    the quiz close and every override, plus grace; None unless all of it is readable."""
    from vektra_assess.moodle.domain import GracePolicy, QuizOverride
    from vektra_assess.readable import Readable, Unreadable, UnreadableReason
    from vektra_assess.worker.start_source import _automatic_close

    def quiz(overrides: Any, grace: Any, close: int = 1000) -> Any:
        return SimpleNamespace(timeclose=close, overrides=overrides, grace=grace)

    def ov(close: int, user: int | None = 5) -> QuizOverride:
        return QuizOverride(user, None if user else 3, None, close, None, None)

    grace = Readable(GracePolicy(graceperiod_s=60, overduehandling="graceperiod"))
    no_grace = Readable(GracePolicy(graceperiod_s=600, overduehandling="autosubmit"))
    gone = Unreadable(UnreadableReason.PARTIAL)
    assert _automatic_close(quiz(Readable((ov(1500), ov(1200, None))), grace)) == 1560
    assert _automatic_close(quiz(Readable(()), no_grace)) == 1000
    assert _automatic_close(quiz(gone, grace)) is None  # Moodle 4.3: no overrides
    assert _automatic_close(quiz(Readable(()), gone)) is None  # closed quiz: partial
    assert _automatic_close(quiz(Readable(()), grace, close=0)) is None  # BR-007
