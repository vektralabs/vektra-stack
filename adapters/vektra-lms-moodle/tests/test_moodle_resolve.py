"""Pre-flight cmid resolution over the recorded 4.3 / 5.2 responses (plan 8, REQ-095)."""

from __future__ import annotations

from typing import Any

import pytest
from assess_moodle_support import BRANCHES, Replay, make_client, manifest, site_for, uid

from vektra_lms_moodle.readers.resolve import ActivityRef, resolve_activity
from vektra_lms_moodle.result import Readable, Unreadable

pytestmark = pytest.mark.parametrize("branch", BRANCHES)


async def _resolve(replay: Replay, branch: str, **kw: Any) -> Any:
    return await resolve_activity(make_client(replay), site_for(branch), **kw)


async def test_quiz_and_assignment_resolve_inside_the_students_course(
    branch: str,
) -> None:
    m = manifest(branch)
    replay = Replay(branch)
    quiz = m["quizzes"]["types"]
    res = await _resolve(
        replay, branch, userid=uid(branch, "stu1"), cmid=quiz["cmid"], kind="quiz",
        courseid_hint=m["courseid"],
    )  # fmt: skip
    assert res == Readable(ActivityRef(m["courseid"], quiz["id"]), res.evidence)
    a = m["assigns"]["rubric_vis"]
    res = await _resolve(
        replay, branch, userid=uid(branch, "stu1"), cmid=a["cmid"], kind="assign",
        courseid_hint=None,
    )  # fmt: skip
    assert isinstance(res, Readable) and res.value == ActivityRef(
        m["courseid"], a["id"]
    )
    assert replay.unmatched == []


async def test_a_wrong_hint_is_ignored_and_a_wrong_kind_is_not_found(
    branch: str,
) -> None:
    m = manifest(branch)
    replay = Replay(branch)
    a = m["assigns"]["simple"]
    res = await _resolve(
        replay, branch, userid=uid(branch, "stu1"), cmid=a["cmid"], kind="assign",
        courseid_hint=999_999,  # not one of the student's courses: never queried
    )  # fmt: skip
    assert isinstance(res, Readable) and res.value.courseid == m["courseid"]
    # an assignment cmid asked as a quiz is not found anywhere: Unreadable, never a guess
    res = await _resolve(
        replay, branch, userid=uid(branch, "stu1"), cmid=a["cmid"], kind="quiz",
    )  # fmt: skip
    assert isinstance(res, Unreadable)
    assert res.detail_code == "activity_not_in_student_courses"
    assert all("999999" not in str(params) for _fn, params in replay.calls)


async def test_an_unreadable_course_list_fails_closed(branch: str) -> None:
    m = manifest(branch)
    replay = Replay(branch)
    replay.mutate = lambda fn, body: (
        {"exception": "x", "errorcode": "nopermission"}
        if fn == "core_enrol_get_users_courses"
        else body
    )
    res = await _resolve(
        replay, branch, userid=uid(branch, "stu1"), cmid=m["quizzes"]["types"]["cmid"],
        kind="quiz",
    )  # fmt: skip
    assert isinstance(res, Unreadable)
