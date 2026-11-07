"""Typed Moodle readers against the recorded 4.3 and 5.2 responses (plan group C).

Every expectation comes from the SEED MANIFEST (what the seeder put into Moodle), never from
what Moodle answered. A request with no recording fails the test (``Replay.unmatched``).
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import zipfile
import zlib
from pathlib import Path
from typing import Any

import pytest
from assess_moodle_support import (
    BRANCHES,
    FIXTURES,
    Replay,
    make_client,
    manifest,
    now,
    site_for,
    uid,
)
from structlog.testing import capture_logs

from vektra_assess.moodle.client import SiteRef
from vektra_assess.moodle.domain import AttemptState
from vektra_assess.moodle.readers import (
    read_assign_close_inputs,
    read_assign_release,
    read_assign_settings,
    read_attempt_review,
    read_course_participants,
    read_enrolment,
    read_grade_release,
    read_graders,
    read_grading_view,
    read_loginas_events,
    read_module_visibility_for_user,
    read_quiz_close_inputs,
    read_quiz_overrides,
    read_rubric_display_options,
    read_rubric_fillings,
    read_scale_definition,
    read_site_info,
    read_submission_status,
    read_user_attempts,
    read_user_courses,
    read_user_flags,
    read_user_review_options,
    with_release,
)
from vektra_assess.moodle.result import Readable, Unreadable, UnreadableReason

pytestmark = pytest.mark.parametrize("branch", BRANCHES)

# the recorder's own file scan (scripts/ is not a package): the test checks what it checks
_SPEC = importlib.util.spec_from_file_location(
    "assess_record_ws",
    Path(__file__).resolve().parents[2] / "scripts" / "assess_record_ws.py",
)
assert _SPEC and _SPEC.loader
_REC = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_REC)


def env(branch: str) -> tuple[Replay, Any, SiteRef, dict[str, Any]]:
    replay = Replay(branch)
    return replay, make_client(replay), site_for(branch), manifest(branch)


def ok(res: Any) -> Any:
    assert isinstance(res, Readable), res
    return res.value


def seeded(m: dict[str, Any], key: str) -> list[tuple[str, dict[str, Any]]]:
    return [(k, v) for k, v in m[key].items() if v.get("seeded")]


# --- site_info ------------------------------------------------------------------------------


async def test_site_info(branch: str) -> None:
    replay, client, site, m = env(branch)
    info = ok(await read_site_info(client, SiteRef("site-1", site.base_url)))
    assert info.release == m["release"] and info.version == m["version"]
    assert info.branch == branch
    assert info.user_is_site_admin is False
    assert info.downloadfiles is True
    assert "mod_quiz_get_attempt_review" in info.functions
    assert ("mod_quiz_get_overrides" in info.functions) is m[
        "ws_get_overrides_available"
    ]
    assert "userprivateaccesskey" not in repr(info)
    assert with_release(SiteRef("s", site.base_url), info).release == m["release"]
    assert not replay.unmatched


# --- quiz -----------------------------------------------------------------------------------


async def test_quiz_close_inputs_match_the_manifest(branch: str) -> None:
    replay, client, site, m = env(branch)
    for name, q in seeded(m, "quizzes"):
        exp = q["expected"]
        got = ok(await read_quiz_close_inputs(client, site, m["courseid"], q["id"]))
        assert (got.timeopen, got.timeclose, got.timelimit_s) == (
            exp["timeopen"],
            exp["timeclose"],
            exp["timelimit"],
        ), name
        assert got.grademethod == exp["grademethod"], name
        closed = exp["timeclose"] <= now(branch)
        if closed:
            # trap 2: caller-relative, absent after close: Unreadable, never 0
            assert (
                isinstance(got.grace, Unreadable)
                and got.grace.reason is UnreadableReason.PARTIAL
            )
            assert isinstance(got.review_after_close, Unreadable)
        else:
            assert ok(got.grace).graceperiod_s == exp["graceperiod"], name
            assert ok(got.grace).overduehandling == exp["overduehandling"], name
            rev = exp["review_after_close"]
            vis = ok(got.review_after_close)
            assert (
                vis.feedback,
                vis.generalfeedback,
                vis.rightanswer,
                vis.overallfeedback,
            ) == (
                rev["specificfeedback"],
                rev["generalfeedback"],
                rev["rightanswer"],
                rev["overallfeedback"],
            ), name
            assert (vis.marks > 0) is rev["marks"], name
    assert not replay.unmatched


async def test_quiz_overrides(branch: str) -> None:
    replay, client, site, m = env(branch)
    q = m["quizzes"]["review_all"]
    res = await read_quiz_overrides(client, site, q["id"])
    if branch == "4.3":
        # mod_quiz_get_overrides does not exist on 4.3: no call is made at all
        assert isinstance(res, Unreadable)
        assert res.reason is UnreadableReason.FUNCTION_NOT_IN_SERVICE
        assert replay.calls == []
        got = ok(await read_quiz_close_inputs(client, site, m["courseid"], q["id"]))
        assert isinstance(got.overrides, Unreadable)
        return
    rows = {(o.userid, o.groupid): o for o in ok(res)}
    assert len(rows) == len(q["expected"]["overrides"]) == 2
    for exp in q["expected"]["overrides"]:
        user = None if exp["user"] is None else uid(branch, exp["user"])
        group = None if exp["group"] is None else m["groups"][exp["group"]]["id"]
        row = rows[(user, group)]
        assert (row.timeclose, row.timelimit_s) == (exp["timeclose"], exp["timelimit"])
    # a quiz with no override: an empty tuple, not Unreadable
    assert (
        ok(await read_quiz_overrides(client, site, m["quizzes"]["types"]["id"])) == ()
    )
    assert not replay.unmatched


async def test_user_attempts(branch: str) -> None:
    replay, client, site, m = env(branch)
    for name, q in seeded(m, "quizzes"):
        by_user: dict[str, list[dict[str, Any]]] = {}
        for a in q["attempts"]:
            by_user.setdefault(a["user"], []).append(a)
        for user, exp in by_user.items():
            got = ok(await read_user_attempts(client, site, q["id"], uid(branch, user)))
            assert [a.attempt_number for a in got] == [
                e["attempt_number"] for e in exp
            ], name
            assert [a.attemptid for a in got] == [e["id"] for e in exp], name
            assert [a.state.value for a in got] == [e["state"] for e in exp], name
            for a, e in zip(got, exp, strict=True):
                if e["state"] == "finished" and e["expected_sumgrades"] is not None:
                    assert a.sumgrades == pytest.approx(e["expected_sumgrades"]), name
                if e["state"] != "finished":
                    assert a.timefinish == 0 and a.state is not AttemptState.FINISHED
    assert not replay.unmatched


async def test_attempt_review(branch: str) -> None:
    replay, client, site, m = env(branch)
    for name, q in seeded(m, "quizzes"):
        maxmarks = {s["slot"]: s for s in q["slots"]}
        for a in q["attempts"]:
            review = ok(await read_attempt_review(client, site, a["id"]))
            assert (
                review.userid == uid(branch, a["user"]) and review.quizid == q["id"]
            ), name
            assert review.state.value == a["state"], name
            by_slot = {s.slot: s for s in review.slots}
            # every seeded slot is there with its question type and maximum mark
            for slot, spec in maxmarks.items():
                assert by_slot[slot].qtype == spec["qtype"], name
                assert by_slot[slot].maxmark == pytest.approx(spec["maxmark"]), name
            for slot, state in (a["expected_slot_states"] or {}).items():
                got = by_slot[int(slot)]
                if got.state is None:
                    # Moodle exposes no ``state`` for zero-mark items (manifest says
                    # gradedright): the reader must not invent one
                    assert got.maxmark == 0, (name, slot)
                else:
                    assert got.state == state, (name, slot)
            if a["state"] == "finished" and a["expected_sumgrades"] is not None:
                assert review.sumgrades == pytest.approx(a["expected_sumgrades"]), name
    assert not replay.unmatched


async def test_needs_grading_is_per_slot(branch: str) -> None:
    _replay, client, site, m = env(branch)
    q = m["quizzes"]["essay"]
    ungraded = next(a for a in q["attempts"] if a["user"] == "stu2")
    review = ok(await read_attempt_review(client, site, ungraded["id"]))
    essay = next(s for s in review.slots if s.qtype == "essay")
    assert (
        essay.needs_grading is True
        and essay.state == "needsgrading"
        and essay.mark is None
    )
    assert [s.needs_grading for s in review.slots if s.qtype != "essay"] == [False]
    assert review.sumgrades is None  # trap: not fully graded
    graded = next(a for a in q["attempts"] if a["user"] == "stu1")
    other = ok(await read_attempt_review(client, site, graded["id"]))
    assert not any(s.needs_grading for s in other.slots)


async def test_review_html_is_separate_and_droppable(branch: str) -> None:
    """Spike trap 4: the HTML carries hidden general feedback; the capture step drops it."""
    _replay, client, site, m = env(branch)
    q = m["quizzes"]["review_none"]  # student options hide every feedback
    review = ok(await read_attempt_review(client, site, q["attempts"][0]["id"]))
    assert any(s.html and "CANARY-GENFB" in s.html for s in review.slots)  # it IS there
    clean = review.without_html()
    assert all(s.html is None for s in clean.slots)
    assert "CANARY-GENFB" not in repr(clean)
    assert [s.state for s in clean.slots] == [s.state for s in review.slots]


async def test_user_review_options(branch: str) -> None:
    replay, client, site, m = env(branch)
    for name, q in seeded(m, "quizzes"):
        if not q["attempts"]:
            continue
        user = q["attempts"][0]["user"]
        rev = q["expected"]["review_after_close"]
        got = ok(
            await read_user_review_options(client, site, q["id"], uid(branch, user))
        )
        v = got.all_attempts
        assert (v.feedback, v.generalfeedback, v.rightanswer, v.overallfeedback) == (
            rev["specificfeedback"],
            rev["generalfeedback"],
            rev["rightanswer"],
            rev["overallfeedback"],
        ), name
        assert (v.marks > 0) is rev["marks"], name
    assert not replay.unmatched


# --- assignment ------------------------------------------------------------------------------


async def test_assign_settings(branch: str) -> None:
    replay, client, site, m = env(branch)
    for name, a in seeded(m, "assigns"):
        exp = a["expected"]
        got = ok(await read_assign_settings(client, site, m["courseid"], a["id"]))
        assert got.cmid == a["cmid"] and got.name == a["name"], name
        assert (got.duedate, got.cutoffdate) == (exp["duedate"], exp["cutoffdate"]), (
            name
        )
        assert got.teamsubmission is bool(exp["teamsubmission"]), name
        assert got.markingworkflow is bool(exp["markingworkflow"]), name
        assert got.grade_type == exp["grade_type"], name
        if exp["grade_type"] == "scale":
            assert got.scale_id == exp["scale"]["id"]
    # trap 1: the request carries includenotenrolledcourses=1
    assert all(p.get("includenotenrolledcourses") == "1" for fn, p in replay.calls)
    assert not replay.unmatched


async def test_assign_close_inputs_effective_dates(branch: str) -> None:
    replay, client, site, m = env(branch)
    a = m["assigns"]["cutoff"]
    exp = a["expected"]
    # no override, no extension
    s1 = ok(await read_assign_close_inputs(client, site, a["id"], uid(branch, "stu1")))
    assert (s1.duedate, s1.cutoffdate, s1.extensionduedate) == (
        exp["duedate"],
        exp["cutoffdate"],
        None,
    )
    # extension (from the submission status; get_user_flags is not readable for this role)
    s2 = ok(await read_assign_close_inputs(client, site, a["id"], uid(branch, "stu2")))
    assert s2.extensionduedate == exp["reader_by_user"]["stu2"]["extensionduedate"]
    assert s2.granted_extension is True
    # user override: get_participant carries the EFFECTIVE dates
    s4 = ok(await read_assign_close_inputs(client, site, a["id"], uid(branch, "stu4")))
    assert (s4.duedate, s4.cutoffdate) == (
        exp["reader_by_user"]["stu4"]["duedate"],
        exp["reader_by_user"]["stu4"]["cutoffdate"],
    )
    assert s4.duedate != exp["duedate"]
    nocut = m["assigns"]["nocutoff"]
    s = ok(
        await read_assign_close_inputs(client, site, nocut["id"], uid(branch, "stu1"))
    )
    assert s.cutoffdate == 0  # 0 = no cut-off
    assert not replay.unmatched


async def test_user_flags_are_unreadable_for_the_minimal_role(branch: str) -> None:
    """``get_user_flags`` needs mod/assign:grade: Moodle answers with a warning (trap 1)."""
    _replay, client, site, m = env(branch)
    res = await read_user_flags(client, site, m["assigns"]["cutoff"]["id"])
    assert isinstance(res, Unreadable)
    assert res.reason is UnreadableReason.PARTIAL and res.detail_code == "warning_1"


async def test_user_flags_readable_when_moodle_answers(branch: str) -> None:
    replay, client, site, m = env(branch)
    a = m["assigns"]["cutoff"]
    replay.mutate = lambda fn, body: (
        {
            "assignments": [
                {
                    "assignmentid": a["id"],
                    "userflags": [
                        {
                            "id": 1,
                            "userid": uid(branch, "stu2"),
                            "locked": 0,
                            "mailed": 0,
                            "extensionduedate": 1793878417,
                            "workflowstate": "",
                        }
                    ],
                }
            ],
            "warnings": [],
        }
        if fn == "mod_assign_get_user_flags"
        else body
    )
    rows = ok(await read_user_flags(client, site, a["id"]))
    assert [(r.userid, r.extensionduedate) for r in rows] == [
        (uid(branch, "stu2"), 1793878417)
    ]


async def test_submission_status(branch: str) -> None:
    replay, client, site, m = env(branch)
    for name, a in seeded(m, "assigns"):
        for user, sub in (a["submissions"] or {}).items():
            got = ok(
                await read_submission_status(client, site, a["id"], uid(branch, user))
            )
            assert got.userid == uid(branch, user), name
            assert got.state == sub["status"], name
            assert got.onlinetext == sub["onlinetext"], (name, user)
            assert sorted((f.filename, f.mimetype) for f in got.files) == sorted(
                (f["filename"], f["mimetype"]) for f in sub["files"]
            ), (name, user)
            assert got.submission_id is not None
            assert all(f.size > 0 and f.fileurl.endswith(f.filename) for f in got.files)
    assert not replay.unmatched


async def test_submission_status_grading_status_and_feedback(branch: str) -> None:
    _replay, client, site, m = env(branch)
    for name, a in seeded(m, "assigns"):
        for g in a["grades"]:
            got = ok(
                await read_submission_status(
                    client, site, a["id"], uid(branch, g["user"])
                )
            )
            assert got.grading_status == g["gradingstatus"], (name, g["user"])
            if g.get("feedback_is_canary") or a["expected"]["gradeishidden"]:
                # withheld by Moodle: the unreleased feedback never reaches the domain value
                assert "CANARY-UNRELEASEDFB" not in repr(got)
            elif "feedback" in g and g["gradingstatus"] != "inmarking":
                assert got.feedback is not None
                assert g["feedback"] in got.feedback.comments, (name, g["user"])
                assert got.feedback.grade == pytest.approx(g["grade"])


async def test_grade_release_from_the_grade_report(branch: str) -> None:
    replay, client, site, m = env(branch)
    # quizzes: hidden grade item (BR-010 for quizzes)
    for name, q in seeded(m, "quizzes"):
        if not q["attempts"]:
            continue
        user = q["attempts"][0]["user"]
        got = ok(
            await read_grade_release(
                client, site, m["courseid"], uid(branch, user), q["cmid"]
            )
        )
        assert got.hidden is q["expected"]["grade_item_hidden"], name
        if got.hidden:
            assert got.grade is None and got.grade_formatted is None  # never surfaced
        elif user in q["expected"]["final_grades"]:
            assert got.grade == pytest.approx(
                q["expected"]["final_grades"][user], abs=1e-4
            ), name
    # assignments
    for name, a in seeded(m, "assigns"):
        for g in a["grades"]:
            got = ok(
                await read_grade_release(
                    client, site, m["courseid"], uid(branch, g["user"]), a["cmid"]
                )
            )
            hidden = a["expected"]["gradeishidden"]
            assert got.hidden is hidden, name
            if hidden:
                assert got.grade is None and got.feedback is None
            elif a["expected"]["markingworkflow"] and g["gradingstatus"] != "released":
                # the grade report also withholds an in-marking grade (graderaw is null)
                assert got.grade is None, name
            else:
                assert got.grade == pytest.approx(g["grade"]), name
    assert not replay.unmatched


async def test_assign_release_state(branch: str) -> None:
    replay, client, site, m = env(branch)
    expected = {
        ("wf", "stu1"): True,  # workflow state released
        (
            "wf",
            "stu2",
        ): False,  # graded but inmarking (trap 3: get_grades would show 60)
        ("hiddengrade", "stu1"): False,  # grade item hidden
        ("hiddenuntil", "stu1"): False,  # hidden until a future date
        ("simple", "stu1"): True,
        ("rubric_vis", "stu1"): True,
        ("guide", "stu1"): False,  # not graded
    }
    for (name, user), want in expected.items():
        a = m["assigns"][name]
        got = ok(
            await read_assign_release(
                client, site, m["courseid"], a["id"], uid(branch, user)
            )
        )
        assert got.released is want, (name, user)
        assert got.marking_workflow is bool(a["expected"]["markingworkflow"])
    # the wf expectation really is the manifest's: released vs inmarking
    wf = {g["user"]: g["gradingstatus"] for g in m["assigns"]["wf"]["grades"]}
    assert wf == {"stu1": "released", "stu2": "inmarking"}
    # trap 3: release never comes from get_grades or get_user_best_grade
    used = {fn for fn, _ in replay.calls}
    assert (
        "mod_assign_get_grades" not in used
        and "mod_quiz_get_user_best_grade" not in used
    )
    assert not replay.unmatched


# --- grading ---------------------------------------------------------------------------------


async def test_grading_view(branch: str) -> None:
    replay, client, site, m = env(branch)
    for name, a in seeded(m, "assigns"):
        exp = a["expected"]
        got = ok(await read_grading_view(client, site, a["cmid"]))
        assert got.method == exp["grading_method"], name
        if exp["grading_method"] == "rubric":
            r = exp["rubric"]
            assert got.rubric is not None and got.guide is None
            assert got.rubric.definitionid == r["definitionid"]
            assert len(got.rubric.criteria) == r["criteria"]
            assert {len(c.levels) for c in got.rubric.criteria} == {
                r["levels_per_criterion"]
            }
            assert [c.description for c in got.rubric.criteria] == r[
                "canary_criterion_descriptions"
            ]
        elif exp["grading_method"] == "guide":
            g = exp["guide"]
            assert got.guide is not None and got.rubric is None
            assert (got.guide.definitionid, got.guide.criteria_count) == (
                g["definitionid"],
                g["criteria"],
            )
            assert got.guide.max_total == g["max_total"]
            assert "CANARY-GUIDEMARKERS" not in repr(
                got
            )  # marker notes are teacher-only
        else:
            assert got.rubric is None and got.guide is None
    assert not replay.unmatched


async def test_rubric_fillings(branch: str) -> None:
    _replay, client, site, m = env(branch)
    for name in ("rubric_vis", "rubric_hidden"):
        a = m["assigns"][name]
        definition = ok(await read_grading_view(client, site, a["cmid"])).rubric
        fillings = ok(
            await read_rubric_fillings(
                client, site, a["expected"]["rubric"]["definitionid"]
            )
        )
        g = a["grades"][0]
        assert len(fillings) == 1
        status = ok(
            await read_submission_status(client, site, a["id"], uid(branch, g["user"]))
        )
        assert (
            status.feedback is not None
            and fillings[0].item_id == status.feedback.grade_id
        )
        by_criterion = {c.criterionid: c for c in definition.criteria}
        chosen: dict[str, float] = {}
        for f in fillings[0].criteria:
            crit = by_criterion[f.criterionid]
            level = next(lv for lv in crit.levels if lv.levelid == f.levelid)
            chosen[crit.description] = level.score
        assert sorted(chosen.values()) == sorted(g["rubric_levels"].values()), name
        assert sum(chosen.values()) == g["rubric_total"], name
        assert all(f.remark.startswith("VISIBLE-REMARK") for f in fillings[0].criteria)


async def test_rubric_display_options_are_unreadable_by_design(branch: str) -> None:
    replay, client, site, m = env(branch)
    res = await read_rubric_display_options(
        client, site, m["assigns"]["rubric_vis"]["expected"]["rubric"]["definitionid"]
    )
    assert (
        isinstance(res, Unreadable) and res.detail_code == "rubric_display_unreadable"
    )
    assert replay.calls == []  # nothing to call: no function exposes it


async def test_scale_definition(branch: str) -> None:
    _replay, client, site, m = env(branch)
    a = m["assigns"]["scale"]
    items = a["expected"]["scale"]["items"]
    got = ok(
        await read_scale_definition(
            client,
            site,
            m["courseid"],
            uid(branch, "stu1"),
            a["cmid"],
            a["expected"]["scale"]["id"],
        )
    )
    assert (got.first_label, got.last_label, got.size) == (
        items[0],
        items[-1],
        len(items),
    )
    assert (
        isinstance(got.labels, Unreadable)
        and got.labels.detail_code == "scale_items_not_exposed"
    )
    # the student's own grade label comes through the release reader
    rel = ok(
        await read_grade_release(
            client, site, m["courseid"], uid(branch, "stu1"), a["cmid"]
        )
    )
    assert rel.grade_formatted == a["grades"][0]["scale_label"]


# --- enrolment, graders ------------------------------------------------------------------------


async def test_enrolment(branch: str) -> None:
    replay, client, site, m = env(branch)
    students = [k for k, v in m["users"].items() if v["role"] == "student"]
    for user in students:
        courses = ok(await read_user_courses(client, site, uid(branch, user)))
        assert [c.courseid for c in courses] == [m["courseid"]], user
        view = ok(await read_enrolment(client, site, m["courseid"], uid(branch, user)))
        assert view.active and view.is_student and view.roles == ("student",), user
    for teacher in ("grader1", "grader2"):
        view = ok(
            await read_enrolment(client, site, m["courseid"], uid(branch, teacher))
        )
        assert view.active and not view.is_student
    parts = ok(await read_course_participants(client, site, m["courseid"]))
    assert {p.userid for p in parts} == {uid(branch, k) for k in m["users"]}
    assert not replay.unmatched


async def test_enrolment_of_a_suspended_or_unenrolled_user(branch: str) -> None:
    replay, client, site, m = env(branch)
    # suspended / unenrolled: get_users_courses lists ACTIVE enrolments only, so the course is
    # missing from the answer (synthetic: the seed has no suspended enrolment)
    replay.mutate = lambda fn, body: (
        [] if fn == "core_enrol_get_users_courses" else body
    )
    gone = ok(await read_enrolment(client, site, m["courseid"], uid(branch, "stu1")))
    assert gone.active is False and gone.is_student is False


async def test_graders(branch: str) -> None:
    replay, client, site, m = env(branch)
    got = ok(await read_graders(client, site, m["courseid"]))
    assert {g.userid for g in got.graders} == {uid(branch, k) for k in m["graders"]}
    assert uid(branch, m["teacher_without_grade_capability"]) not in {
        g.userid for g in got.graders
    }
    assert {g.roles[0] for g in got.graders} == {"editingteacher", "teacher"}
    # Moodle returns no email to the service account: never invented
    assert all(g.email is None for g in got.graders)
    assert not replay.unmatched


async def test_expected_unreadable_readers(branch: str) -> None:
    replay, _client, site, m = env(branch)
    vis = await read_module_visibility_for_user(
        site, m["courseid"], m["assigns"]["restricted"]["cmid"], uid(branch, "stu3")
    )
    login = await read_loginas_events(site, m["courseid"], uid(branch, "stu1"))
    for res in (vis, login):
        assert isinstance(res, Unreadable)
        assert res.reason is UnreadableReason.FUNCTION_NOT_IN_SERVICE
    assert replay.calls == []


# --- fixtures hygiene ----------------------------------------------------------------------------


def test_every_reader_function_has_a_recording(branch: str) -> None:
    """A missing recording fails; it does not skip."""
    needed = {
        "core_webservice_get_site_info",
        "mod_quiz_get_quizzes_by_courses",
        "mod_quiz_get_user_attempts",
        "mod_quiz_get_attempt_review",
        "mod_quiz_get_combined_review_options",
        "mod_assign_get_assignments",
        "mod_assign_get_user_flags",
        "mod_assign_get_participant",
        "mod_assign_get_submission_status",
        "gradereport_user_get_grade_items",
        "core_grading_get_definitions",
        "core_grading_get_gradingform_instances",
        "core_enrol_get_users_courses",
        "core_enrol_get_enrolled_users",
        "core_enrol_get_enrolled_users_with_capability",
    }
    if branch == "5.2":
        needed.add("mod_quiz_get_overrides")
    have = {
        json.loads(f.read_text())["wsfunction"]
        for f in (FIXTURES / branch).glob("*__*.json")
    }
    assert needed <= have, needed - have


def test_recordings_are_scrubbed(branch: str) -> None:
    blob = "".join(f.read_text() for f in (FIXTURES / branch).glob("*.json"))
    assert "localhost" not in blob and "127.0.0.1" not in blob
    assert '"userprivateaccesskey": "REDACTED"' in blob
    assert "token=" not in blob.replace("token=REDACTED", "")


def _scan(path: Path) -> list[str]:
    hits: list[str] = _REC.file_secret_hits(path.name, path.read_bytes())
    return hits


def test_recorded_files_are_scrubbed(branch: str) -> None:
    """The submission files are written as downloaded: scan raw bytes and zip members too."""
    files = sorted((FIXTURES / branch / "files").iterdir())
    assert files, "no recorded submission files: the scan would be vacuous"
    members = sum(
        len(_REC._file_layers(f.name, f.read_bytes())) - 1
        for f in files
        if f.suffix in {".docx", ".pptx", ".odt"}
    )
    assert members > 0, "no zip member was inspected"
    assert [h for f in files for h in _scan(f)] == []


def _zip(member: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(member, text)
    return buf.getvalue()


def _pdf(
    body: bytes,
    filters: bytes = b"/FlateDecode",
    extra: bytes = b"",
    key: bytes = b"/Filter",
) -> bytes:
    return (
        b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n4 0 obj\n<< /Length "
        + str(len(body)).encode()
        + b" "
        + key
        + b" "
        + filters
        + extra
        + b" >>\nstream\n"
        + body
        + b"\nendstream\nendobj\n%%EOF\n"
    )


def _flate_pdf(text: str) -> bytes:
    return _pdf(zlib.compress(text.encode()))


def _a85_flate_pdf(text: str) -> bytes:
    # /Filter [/ASCII85Decode /FlateDecode]: decode A85 first, then inflate
    body = base64.a85encode(zlib.compress(text.encode())) + b"~>"
    return _pdf(body, b"[/ASCII85Decode /FlateDecode]")


def test_the_file_scan_decodes_a_pdf_filter_chain(branch: str, tmp_path: Path) -> None:
    """CodeRabbit 4499365567 (round 2): an A85+Flate stream used to fail inflate and be skipped."""
    for name, data in {
        "a85_flate": _a85_flate_pdf("BT (key WSTOKEN=abc) Tj ET"),
        "ahx_flate": _pdf(
            zlib.compress(b"BT (wstoken) Tj ET").hex().encode() + b">",
            b"[/AHx /Fl]",
        ),
        "ahx": _pdf(b"BT (wstoken) Tj ET".hex().encode() + b">", b"/ASCIIHexDecode"),
        # PDF name escapes (#xx) in the key and in a filter name are decoded first
        "escaped_key": _pdf(zlib.compress(b"wstoken"), key=b"/F#69lter"),
        "escaped_name": _pdf(zlib.compress(b"wstoken"), b"/Fl#61teDecode"),
        "escaped_chain": _pdf(
            base64.a85encode(zlib.compress(b"wstoken")) + b"~>",
            b"[/ASCII85#44ecode /#46lateDecode]",
        ),
        # CodeRabbit 4499897027: a longer name that starts with /DP or /DecodeParms is not one
        "dp_prefix_names": _pdf(
            zlib.compress(b"wstoken"), extra=b" /DPI 72 /DecodeParmsX 1 /DP-x 2"
        ),
    }.items():
        assert b"wstoken" not in data.lower(), name
        path = tmp_path / f"stu1__{name}.pdf"
        path.write_bytes(data)
        hits = _scan(path)
        assert any(h.endswith(": wstoken") for h in hits), (name, hits)
        assert not any("uninspectable" in h for h in hits), (name, hits)
    clean = tmp_path / "stu1__clean.pdf"
    clean.write_bytes(_a85_flate_pdf("BT (CANARY-PDFTEXT ok) Tj ET"))
    assert _scan(clean) == []


@pytest.mark.parametrize(
    "data",
    [
        _pdf(b"opaque", b"/LZWDecode"),  # unsupported filter
        _pdf(b"opaque", b"[/ASCII85Decode /DCTDecode]"),  # unsupported link in a chain
        _pdf(b"not zlib", b"/FlateDecode"),  # does not decode
        _pdf(b"vvvvv~>", b"/ASCII85Decode"),  # does not decode
        _pdf(  # a predictor rearranges the decoded bytes
            zlib.compress(b"wstoken"),
            b"/FlateDecode",
            b" /DecodeParms << /Predictor 12 >>",
        ),
        b"%PDF-1.4\nstream\rwstoken\rendstream\n",  # a stream the scan cannot delimit
        # CodeRabbit 4499897034: an indirect /Filter (alone or in a chain) cannot be resolved
        _pdf(zlib.compress(b"wstoken"), b"5 0 R"),
        _pdf(
            base64.a85encode(zlib.compress(b"wstoken")) + b"~>",
            b"[/ASCII85Decode 6 0 R]",
        ),
        # an escape that is not #xx, or one that decodes to a delimiter, is ambiguous
        _pdf(zlib.compress(b"wstoken"), key=b"/F#zzlter"),
        _pdf(zlib.compress(b"wstoken"), b"/A85#2FFlateDecode"),
        _pdf(zlib.compress(b"wstoken"), extra=b" /DP<< /Predictor 12 >>"),
    ],
    ids=[
        "unknown",
        "unknown_in_chain",
        "bad_flate",
        "bad_a85",
        "predictor",
        "undelimited",
        "indirect_filter",
        "indirect_in_chain",
        "bad_escape",
        "escaped_delimiter",
        "dp_abbreviation",
    ],
)
def test_the_file_scan_refuses_an_uninspectable_pdf_stream(
    branch: str, data: bytes, tmp_path: Path
) -> None:
    path = tmp_path / "stu1__opaque.pdf"
    path.write_bytes(data)
    assert any("uninspectable" in h for h in _scan(path))


@pytest.mark.parametrize("needle", _REC.FILE_NEEDLES)
@pytest.mark.parametrize("shape", ["txt", "docx", "pdf"])
def test_the_file_scan_fails_on_a_contaminated_file(
    branch: str, needle: str, shape: str, tmp_path: Path
) -> None:
    text = f"VISIBLE text {needle.upper()} more text"  # case-insensitive match
    data = {
        "txt": text.encode(),
        "docx": _zip("word/document.xml", f"<w:t>{text}</w:t>"),  # deflated: not in raw
        "pdf": _flate_pdf(f"BT ({text}) Tj ET"),  # inflated stream only
    }[shape]
    path = tmp_path / f"stu1__synthetic.{shape}"
    path.write_bytes(data)
    if shape != "txt":
        assert needle.upper().encode() not in data, (
            "the needle must be hidden by compression"
        )
    assert any(needle in h for h in _scan(path))


def test_the_file_scan_passes_a_clean_file_and_refuses_an_unreadable_zip(
    branch: str, tmp_path: Path
) -> None:
    clean = tmp_path / "stu1__clean.docx"
    clean.write_bytes(_zip("word/document.xml", "<w:t>CANARY-DOCX ok</w:t>"))
    assert _scan(clean) == []
    broken = tmp_path / "stu1__broken.docx"
    broken.write_bytes(b"PK\x03\x04 not really a zip")
    assert _scan(broken) != []


async def test_readers_log_no_body_param_or_url(branch: str) -> None:
    _replay, client, site, m = env(branch)
    with capture_logs() as logs:
        a = m["assigns"]["files"]
        await read_site_info(client, SiteRef("site-1", site.base_url))
        await read_submission_status(client, site, a["id"], uid(branch, "stu1"))
        await read_attempt_review(
            client, site, m["quizzes"]["review_none"]["attempts"][0]["id"]
        )
        await read_grading_view(client, site, m["assigns"]["rubric_vis"]["cmid"])
        await read_graders(client, site, m["courseid"])
        await read_user_flags(client, site, m["assigns"]["cutoff"]["id"])
    text = json.dumps(logs, default=str)
    assert logs, "the client logs one event per call"
    for canary in m["canaries"]:
        assert canary not in text
    for needle in (
        "0123456789abcdef",
        "VISIBLE-ONLINETEXT",
        "moodle.example.org",
        "pluginfile",
        "userid",
    ):
        assert needle not in text, needle


async def test_feedback_of_an_unreleased_grade_never_reaches_the_domain_value(
    branch: str,
) -> None:
    """BR-010: Moodle may send the feedback block for an in-marking submission to a
    teacher-visibility caller; the reader drops it unless the status says graded/released."""
    replay, client, site, m = env(branch)
    target = None
    for _name, a in seeded(m, "assigns"):
        for g in a["grades"]:
            if "feedback" in g and g["gradingstatus"] not in ("inmarking",):
                target = (a, g)
                break
        if target:
            break
    assert target is not None
    a, g = target

    def to_inmarking(fn: str, doc: Any) -> Any:
        if fn == "mod_assign_get_submission_status":
            doc["lastattempt"]["gradingstatus"] = "inmarking"
        return doc

    replay.mutate = to_inmarking
    got = ok(
        await read_submission_status(client, site, a["id"], uid(branch, g["user"]))
    )
    assert got.grading_status == "inmarking" and got.feedback is None
