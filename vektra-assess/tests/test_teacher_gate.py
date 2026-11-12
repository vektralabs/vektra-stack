"""The activity-level teacher facts (plan assess-teacher-ops 11a), on ``FakeLmsProvider``.

``LmsStartSource.teacher_facts`` resolves the cmid INSIDE the hinted course and reads its
editors, graders and the REQ-077 enable facts (close date, team submission, grading method,
LMS rubric) and, for a quiz, the override facts of the REQ-076 gate panel (11b). Neutral values
only (ADR-0033): the Moodle provider's own mapping of these reads is tested on its recordings.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

from support.fake_lms import (
    ASSIGN_ACTIVITY,
    ASSIGNMENT,
    COURSE,
    QUIZ,
    QUIZ_ACTIVITY,
    FakeLmsProvider,
    default_world,
)

from vektra_assess.jobs import JobKind
from vektra_assess.worker.runner import JobContext
from vektra_assess.worker.start_source import LmsStartSource
from vektra_assess.worker.teacher_gate import make_teacher_gate_handler
from vektra_shared.lms import (
    ActivityRef,
    GracePolicy,
    GradingDefinition,
    MarkingGuideSummary,
    OverdueMode,
    QuizOverride,
    Readable,
    StaffMember,
    StaffRole,
    Unreadable,
    UnreadableReason,
)

SITE = uuid.uuid4()
GRADERS = (70, 71)
EDITORS = (72,)
VIEWERS = (80, 81)
TEAM_ACTIVITY, TEAM = 14, 24
GUIDE_ACTIVITY, GUIDE = 15, 25
SIMPLE_ACTIVITY, SIMPLE = 16, 26
STUDENT_B = 8


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def first(self) -> Any:
        return self._row

    def scalar(self) -> Any:
        return "2026-11-08T00:00:00+00:00"  # the clock read before the LMS reads


class _Conn:
    def __init__(self, sink: list[Any]) -> None:
        self.sink = sink

    async def execute(self, stmt: Any, params: Any = None) -> _Result:
        self.sink.append((str(stmt), params))
        return _Result(
            SimpleNamespace(base_url="https://lms.example.org", release="1.0")
        )

    async def rollback(self) -> None:
        return None

    def begin(self) -> Any:
        @asynccontextmanager
        async def tx():  # type: ignore[no-untyped-def]
            yield self

        return tx()


def _staff(ids: tuple[int, ...]) -> tuple[StaffMember, ...]:
    return tuple(StaffMember(u, f"Staff {u}", None, ("teacher",)) for u in ids)


def _world() -> Any:
    w = default_world()
    w.staff[(COURSE, StaffRole.GRADER)] = _staff(GRADERS)
    w.staff[(COURSE, StaffRole.EDITOR)] = _staff(EDITORS)
    w.staff[(COURSE, StaffRole.OVERRIDE_MANAGER)] = _staff(VIEWERS)
    base = w.assignments[(COURSE, ASSIGNMENT)]
    for activity, instance, changes, grading in (
        (TEAM_ACTIVITY, TEAM, {"team_submission": True, "cutoff_date": 1_700_200_000}, None),
        (GUIDE_ACTIVITY, GUIDE, {}, GradingDefinition(GUIDE_ACTIVITY, "guide", None,
                                                    MarkingGuideSummary(9, 2, 10.0))),
        (SIMPLE_ACTIVITY, SIMPLE, {}, GradingDefinition(SIMPLE_ACTIVITY, "simple", None, None)),
    ):  # fmt: skip
        w.activities[("assign", activity)] = ActivityRef(COURSE, instance, "C3")
        w.assignments[(COURSE, instance)] = dataclasses.replace(
            base, assignment_id=instance, activity_id=activity, **changes
        )
        if grading is not None:
            w.gradings[activity] = grading
    return w


def _source(
    world: Any | None = None,
) -> tuple[FakeLmsProvider, LmsStartSource, list[Any]]:
    fake = FakeLmsProvider(world or _world())
    sink: list[Any] = []

    @asynccontextmanager
    async def acquire():  # type: ignore[no-untyped-def]
        yield _Conn(sink)

    return fake, LmsStartSource(fake, None, acquire), sink  # type: ignore[arg-type]


async def test_assignment_facts() -> None:
    fake, src, _ = _source()
    team = await src.teacher_facts(SITE, TEAM_ACTIVITY, COURSE, "assign")
    assert team["resolved"] is True and team["courseid"] == COURSE
    assert team["team_submission"] is True and team["close_at"] == 1_700_200_000
    assert team["editors"] == list(EDITORS)
    # 11b: the graders (the list, reset, terminate, extra time, opening fallback) are read
    assert team["graders"] == list(GRADERS)
    # an assignment's participant closes are never listable (OQ-025): no holder, no viewers
    assert team["override_holder"] is None and team["override_viewers"] is None
    assert team["grading_method"] is None  # no grading definition for it: unreadable
    nocut = await src.teacher_facts(SITE, ASSIGN_ACTIVITY, COURSE, "assign")
    assert nocut["close_at"] == 0 and nocut["team_submission"] is False
    assert nocut["grading_method"] == "rubric" and nocut["moodle_rubric"] is True
    guide = await src.teacher_facts(SITE, GUIDE_ACTIVITY, COURSE, "assign")
    assert guide["grading_method"] == "guide" and guide["moodle_rubric"] is False
    simple = await src.teacher_facts(SITE, SIMPLE_ACTIVITY, COURSE, "assign")
    assert simple["moodle_rubric"] is False
    # ids and flags only: no names, no roles, no addresses
    blob = json.dumps(team)
    assert "@" not in blob and "Staff" not in blob and "teacher" not in blob
    # resolved inside the hinted course only, never through a student's course list
    assert fake.count("resolve_activity") == 0
    assert all(
        c.args["course_id"] == COURSE
        for c in fake.calls
        if c.member == "resolve_in_course"
    )


async def test_quiz_facts() -> None:
    _fake, src, _ = _source()
    got = await src.teacher_facts(SITE, QUIZ_ACTIVITY, COURSE, "quiz")
    assert got["resolved"] is True and got["activity_type"] == "quiz"
    assert got["close_at"] == 1_700_003_600
    assert got["team_submission"] is False and got["grading_method"] is None
    assert got["graders"] == list(GRADERS)
    assert got["override_viewers"] == list(VIEWERS)
    assert not set(got["override_viewers"]) & set(got["graders"])
    assert got["override_holder"] is None  # no override later than the quiz's own close


def _with_overrides(overrides: Any) -> Any:
    w = _world()
    q = w.quizzes[(COURSE, QUIZ)]
    w.quizzes[(COURSE, QUIZ)] = dataclasses.replace(q, overrides=overrides)
    return w


async def test_the_override_holder_is_read_only_where_overrides_are() -> None:
    """REQ-076 gate panel: where the overrides are readable, the user override that sets the
    latest close names its holder (it beats a group override); where they are not, nobody is
    named."""
    close = 1_700_003_600
    readable = Readable(
        (
            QuizOverride(None, 3, None, close + 500, None, None),
            QuizOverride(STUDENT_B, None, None, close + 900, None, None),
        )
    )
    _f, src, _ = _source(_with_overrides(readable))
    got = await src.teacher_facts(SITE, QUIZ_ACTIVITY, COURSE, "quiz")
    assert got["override_holder"] == STUDENT_B
    _f, src, _ = _source(
        _with_overrides(Unreadable(UnreadableReason.FUNCTION_NOT_IN_SERVICE))
    )
    got = await src.teacher_facts(SITE, QUIZ_ACTIVITY, COURSE, "quiz")
    assert got["override_holder"] is None
    # ids and flags only: never an override's reason, a name or a date of another student
    assert set(got) == {
        "resolved", "courseid", "activity_type", "editors", "graders", "close_at",
        "team_submission", "grading_method", "moodle_rubric", "override_holder",
        "override_viewers", "automatic_close_at",
    }  # fmt: skip


async def test_a_wrong_type_hint_never_resolves() -> None:
    fake, src, _ = _source()
    # a quiz's cmid looked up among the course's assignments: not there
    got = await src.teacher_facts(SITE, QUIZ_ACTIVITY, COURSE, "assign")
    assert got["resolved"] is False and got["reason"]
    assert "editors" not in got and "graders" not in got
    fake.assert_never_read("course_staff", "assignment_settings", "grading_definition")


async def test_an_unreadable_staff_list_is_none_never_empty() -> None:
    fake, src, _ = _source()
    fake.fail("course_staff", Unreadable(UnreadableReason.TIMEOUT))
    got = await src.teacher_facts(SITE, QUIZ_ACTIVITY, COURSE, "quiz")
    assert got["editors"] is None and got["graders"] is None
    assert got["override_viewers"] is None


async def test_the_handler_stores_the_facts_with_their_hint() -> None:
    _fake, src, sink = _source()

    @asynccontextmanager
    async def acquire():  # type: ignore[no-untyped-def]
        yield _Conn(sink)

    handler = make_teacher_gate_handler(src)
    args = {
        "site_id": str(SITE),
        "cmid": TEAM_ACTIVITY,
        "courseid": COURSE,
        "activity_type": "assign",
    }
    ctx = JobContext(1, JobKind.TEACHER_GATE, args, 1, acquire, logging.getLogger())
    await handler(ctx)
    (stored,) = [p for sql, p in sink if "assess_teacher_gate" in sql]
    facts = json.loads(stored["i"])
    assert facts["hint"] == [COURSE, "assign"]
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


def test_the_automatic_close_is_the_start_gates_formula() -> None:
    """CodeRabbit 4506916932: the teacher pages show the opening Start waits for: the latest of
    the quiz close and every override, plus grace; None unless all of it is readable."""
    from vektra_assess.worker.start_source import _automatic_close

    base = default_world().quizzes[(COURSE, QUIZ)]

    def quiz(overrides: Any, grace: Any, close: int = 1000) -> Any:
        return dataclasses.replace(
            base, time_close=close, overrides=overrides, grace=grace
        )

    def ov(close: int, user: int | None = 5) -> QuizOverride:
        return QuizOverride(user, None if user else 3, None, close, None, None)

    grace = Readable(GracePolicy(60, OverdueMode.GRACE))
    no_grace = Readable(GracePolicy(600, OverdueMode.AUTOSUBMIT))
    gone = Unreadable(UnreadableReason.PARTIAL)
    assert _automatic_close(quiz(Readable((ov(1500), ov(1200, None))), grace)) == 1560
    assert _automatic_close(quiz(Readable(()), no_grace)) == 1000
    assert _automatic_close(quiz(gone, grace)) is None  # overrides not readable
    assert _automatic_close(quiz(Readable(()), gone)) is None  # closed quiz: partial
    assert _automatic_close(quiz(Readable(()), grace, close=0)) is None  # BR-007
