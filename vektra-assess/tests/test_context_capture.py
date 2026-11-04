"""capture_quiz / capture_assign on the recorded 4.3 and 5.2 responses (plan assess-context C).

The parser is the sidecar's own dispatch (``child.run``) called in-process, so file and HTML
statuses are the real ones without a socket. Expectations come from the seed manifest.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import pytest
from assess_moodle_support import (
    BRANCHES,
    FIXTURES,
    Replay,
    make_client,
    manifest,
    site_for,
    uid,
)

from vektra_assess.context.capture.capture import (
    AssignTarget,
    Built,
    Capabilities,
    CaptureLimits,
    CaptureNotEligible,
    QuizTarget,
    TeacherConfigView,
    _files,
    capture_assign,
    capture_quiz,
)
from vektra_assess.context.capture.readers import MoodleCaptureReaders
from vektra_assess.context.capture.selection import NotEligibleReason
from vektra_assess.context.snapshot import GradeKind, canonical_bytes
from vektra_assess.moodle.readers import read_submission_status
from vektra_assess.moodle.result import Readable
from vektra_assess.parser_protocol import FileStatus
from vektra_assess.parser_sidecar.child import run
from vektra_shared.testing_recorders import find_canaries

OFF = TeacherConfigView(show_solution=False, rubric_source="none")


class InProcessParser:
    """The sidecar's dispatch without the socket (same statuses, same text)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def parse_file(self, data: bytes, ext_hint: str = "") -> Any:
        self.calls.append("parse")
        return run("parse", data, ext_hint, max_pages=50, max_chars=200_000)

    async def sanitize_html(self, html: bytes) -> Any:
        self.calls.append("sanitize_html")
        return run("sanitize_html", html, "html", max_pages=1, max_chars=200_000)

    async def extract_review_block(self, html: bytes, block: str) -> Any:
        self.calls.append(f"review_{block}")
        return run(
            f"review_{'qtext' if block == 'qtext' else 'response'}",
            html,
            "html",
            max_pages=1,
            max_chars=200_000,
        )


@pytest.fixture(params=BRANCHES)
def branch(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _env(branch: str) -> tuple[Replay, MoodleCaptureReaders, dict[str, Any]]:
    replay = Replay(branch)
    return (
        replay,
        MoodleCaptureReaders(make_client(replay), site_for(branch)),
        manifest(branch),
    )


def _all_canaries(m: dict[str, Any]) -> list[str]:
    return list(m["canaries"])


async def _quiz(
    branch: str, name: str, user: str = "stu1", teacher: TeacherConfigView = OFF
) -> Any:
    replay, readers, m = _env(branch)
    q = m["quizzes"][name]
    target = QuizTarget(
        courseid=m["courseid"], quizid=q["id"], cmid=q["cmid"], userid=uid(branch, user)
    )
    parser = InProcessParser()
    out = await capture_quiz(
        target,
        readers,
        parser,
        teacher,
        Capabilities(),
        moodle_release=m["release"],
        now=1_800_000_000,
    )
    assert not replay.unmatched, replay.unmatched
    return out, m, parser


async def test_quiz_with_everything_hidden_keeps_no_hidden_material(
    branch: str,
) -> None:
    out, m, _ = await _quiz(branch, "review_mixed")
    assert isinstance(out, Built), out
    blob = canonical_bytes(out.snapshot)
    hits = {c: n for c, n in find_canaries(blob, _all_canaries(m)).items() if n}
    assert hits == {}
    assert out.snapshot.exclusion.html_structured_only is True
    assert "solution_text" in out.snapshot.exclusion.excluded_paths
    # the exclusion event carries the path-set hash, never a value
    (ev,) = [e for e in out.audit if e.event == "assess_context_exclusion_applied"]
    assert ev.data["excluded_field_set_hash"] == out.snapshot.exclusion.field_set_hash()


async def test_quiz_items_carry_question_and_answer_for_enabled_types(
    branch: str,
) -> None:
    out, m, parser = await _quiz(branch, "types")
    assert isinstance(out, Built), out
    by_type = {i.qtype: i for i in out.snapshot.quiz_items}
    for qtype in ("truefalse", "multichoice", "shortanswer", "numerical"):
        item = by_type[qtype]
        assert item.question_text and "VISIBLE-QTEXT" in item.question_text, qtype
        assert item.response_text, qtype
        assert not item.degraded, qtype
    for qtype in ("multianswer", "ddwtos", "match", "gapselect"):
        assert by_type[qtype].question_text is None and by_type[qtype].degraded, qtype
    assert "description" not in by_type  # information items are not discussion items
    assert out.snapshot.degraded_count == sum(
        i.degraded for i in out.snapshot.quiz_items
    )
    blob = canonical_bytes(out.snapshot)
    assert find_canaries(
        blob, [c for c in _all_canaries(m) if "GENFB" in c]
    ) == dict.fromkeys([c for c in _all_canaries(m) if "GENFB" in c], 0)
    assert b"The correct answer" not in blob
    assert "sanitize_html" not in parser.calls  # the whole page is never sent


async def test_highest_selects_the_earliest_tie_and_freezes_k_of_m(branch: str) -> None:
    out, m, _ = await _quiz(branch, "gm_highest")
    assert isinstance(out, Built)
    ties = m["quizzes"]["gm_highest"]["expected"]["highest_ties_attempt_numbers"][
        "stu1"
    ]
    assert (out.snapshot.header.attempt_number, out.snapshot.header.attempt_count) == (
        min(ties),
        4,
    )
    assert out.snapshot.overall.grade == "10"


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("review_none", NotEligibleReason.NOT_RELEASED),
        ("inprogress", NotEligibleReason.IN_PROGRESS),
    ],
)
async def test_quiz_not_eligible_builds_nothing(
    branch: str, name: str, reason: NotEligibleReason
) -> None:
    out, _, _ = await _quiz(branch, name)
    assert out == CaptureNotEligible(reason)


async def test_toggle_on_includes_the_solution_and_off_never_fetches_it(
    branch: str,
) -> None:
    on = TeacherConfigView(
        show_solution=True,
        rubric_source="pasted",
        solution_text="CNRY-solution-xyz",
        pasted_rubric="CNRY-pasted-abc",
    )
    out, _, _ = await _quiz(branch, "review_mixed", teacher=on)
    assert isinstance(out, Built)
    assert (
        out.snapshot.solution_text == "CNRY-solution-xyz"
        and out.snapshot.pasted_rubric == "CNRY-pasted-abc"
    )
    assert "solution_text" not in out.snapshot.exclusion.excluded_paths
    with pytest.raises(ValueError, match="only when show_solution"):
        TeacherConfigView(
            show_solution=False, rubric_source="none", solution_text="leak"
        )


# --- assignments ---------------------------------------------------------------------------------


async def _assign(branch: str, name: str, user: str = "stu1") -> Any:
    replay, readers, m = _env(branch)
    a = m["assigns"][name]
    target = AssignTarget(
        courseid=m["courseid"],
        assignid=a["id"],
        cmid=a["cmid"],
        userid=uid(branch, user),
    )
    out = await capture_assign(
        target,
        readers,
        InProcessParser(),
        OFF,
        Capabilities(),
        moodle_release=m["release"],
        now=1_800_000_000,
    )
    assert not replay.unmatched, replay.unmatched
    return out, m


async def test_rubric_levels_points_and_remarks_without_descriptors(
    branch: str,
) -> None:
    for name in ("rubric_vis", "rubric_hidden"):
        out, m = await _assign(branch, name)
        assert isinstance(out, Built), (name, out)
        exp = m["assigns"][name]["grades"][0]
        crit = out.snapshot.criteria
        assert len(crit) == len(exp["rubric_levels"])
        assert sum(float(c.points) for c in crit) == exp["rubric_total"]
        assert all(c.description is None and c.chosen_level_text is None for c in crit)
        blob = canonical_bytes(out.snapshot)
        rubric_canaries = [c for c in _all_canaries(m) if "RUBRIC" in c]
        assert sum(find_canaries(blob, rubric_canaries).values()) == 0, (
            name
        )  # OQ-025: always excluded
        assert out.snapshot.exclusion.rubric_descriptors_excluded
        assert f"VISIBLE-ONLINETEXT-{name}" in (
            out.snapshot.submission.onlinetext or ""
        )


async def test_scale_grade_is_the_label(branch: str) -> None:
    out, m = await _assign(branch, "scale")
    assert isinstance(out, Built), out
    ov = out.snapshot.overall
    assert (
        ov.grade_kind is GradeKind.SCALE and ov.grade is None and ov.grade_max is None
    )
    assert ov.scale_label == m["assigns"]["scale"]["grades"][0]["scale_label"]


async def test_simple_grade_and_visible_feedback(branch: str) -> None:
    out, m = await _assign(branch, "simple")
    assert isinstance(out, Built), out
    exp = m["assigns"]["simple"]["grades"][0]
    assert float(out.snapshot.overall.grade or "nan") == exp["grade"]
    assert exp["feedback"] in (out.snapshot.overall.overall_feedback or "")


@pytest.mark.parametrize(
    ("name", "user"), [("hiddengrade", "stu1"), ("hiddenuntil", "stu1"), ("wf", "stu2")]
)
async def test_unreleased_grade_is_not_eligible_and_carries_nothing(
    branch: str, name: str, user: str
) -> None:
    out, _ = await _assign(branch, name, user)
    assert out == CaptureNotEligible(NotEligibleReason.NOT_RELEASED)


async def test_marking_guide_is_unsupported(branch: str) -> None:
    out, _ = await _assign(branch, "guide")
    assert out == CaptureNotEligible(NotEligibleReason.UNSUPPORTED)


async def test_released_workflow_grade_is_captured(branch: str) -> None:
    out, _ = await _assign(branch, "wf", "stu1")
    assert isinstance(out, Built), out
    assert "CANARY-UNRELEASEDFB" not in canonical_bytes(out.snapshot).decode()


# --- file intake (REQ-101) -----------------------------------------------------------------------


async def _intake(
    branch: str, user: str, limits: CaptureLimits = CaptureLimits()
) -> Any:
    replay, readers, m = _env(branch)
    client = make_client(replay)
    site = site_for(branch)
    a = m["assigns"]["files"]
    status = await read_submission_status(client, site, a["id"], uid(branch, user))
    assert isinstance(status, Readable)
    for f in status.value.files:
        local = FIXTURES / branch / "files" / f"{user}__{f.filename}"
        replay.files[urlsplit(f.fileurl).path] = local.read_bytes()
    readers = MoodleCaptureReaders(client, site)
    audit: list[Any] = []
    target = AssignTarget(
        courseid=m["courseid"],
        assignid=a["id"],
        cmid=a["cmid"],
        userid=uid(branch, user),
    )
    out = await _files(target, status.value, readers, InProcessParser(), limits, audit)
    return out, audit, m


async def test_file_statuses_follow_req_101(branch: str) -> None:
    out, _, m = await _intake(branch, "stu1")
    by_name = {f.filename: f for f in out}
    assert by_name["report_text.pdf"].status is FileStatus.READ
    assert by_name["report_scan.pdf"].status is FileStatus.NO_TEXT
    assert by_name["report.docx"].status is FileStatus.READ
    assert by_name["report.odt"].status is FileStatus.READ
    assert by_name["slides.pptx"].status is FileStatus.UNSUPPORTED_FORMAT
    assert by_name["slides.pptx"].text == "" and by_name["report_scan.pdf"].text == ""
    for f in m["assigns"]["files"]["submissions"]["stu1"]["files"]:
        canary, entry = f["content_canary"], by_name[f["filename"]]
        if not canary:
            continue
        if entry.status is FileStatus.READ:
            assert canary in entry.text, f["filename"]  # read text is in the snapshot
        else:  # unread files contribute nothing (REQ-101: never discussed)
            assert all(canary not in e.text for e in out), f["filename"]


async def test_the_eleventh_file_is_over_the_maximum(branch: str) -> None:
    out, _, _ = await _intake(branch, "stu2")
    assert len(out) == 11
    assert [f.status for f in out[10:]] == [FileStatus.OVER_MAX_FILES]
    assert all(f.status is FileStatus.READ for f in out[:10])


async def test_total_text_budget_cuts_to_partial(branch: str) -> None:
    out, _, _ = await _intake(branch, "stu2", CaptureLimits(total_text_chars=30))
    assert sum(len(f.text) for f in out) <= 30
    statuses = [f.status for f in out]
    # the file that crosses the cap keeps what fits; later ones get nothing and say so
    assert FileStatus.READ_PARTIAL in statuses or FileStatus.TOO_LARGE in statuses
    assert statuses[-1] is FileStatus.OVER_MAX_FILES
    assert all(s is not FileStatus.NO_TEXT for s in statuses)


async def test_oversized_files_are_not_downloaded(branch: str) -> None:
    out, _, _ = await _intake(branch, "stu1", CaptureLimits(max_file_bytes=10))
    assert {f.status for f in out} == {FileStatus.TOO_LARGE}
