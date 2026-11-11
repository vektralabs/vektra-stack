"""capture_quiz / capture_assign on the recorded 4.3 and 5.2 responses (plan assess-context C),
through the Moodle LMS provider: Vektra's capture and canary sinks over the provider's recorded
reviews, the joint BR-011 guarantee of ADR-0033 D4. The capture logic on neutral values alone is
in ``test_capture_fake.py``.

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
    manifest,
    site_readers,
    uid,
)

from vektra_assess.context.capture.bound import SiteReaders
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
from vektra_assess.context.capture.selection import NotEligibleReason
from vektra_assess.context.snapshot import GradeKind, canonical_bytes
from vektra_assess.moodle.provider import MOODLE_REVIEW_PROFILE
from vektra_assess.parser_protocol import FileStatus, ReviewProfile
from vektra_assess.parser_sidecar.child import run
from vektra_shared.lms import HtmlExtractionProfile, Readable
from vektra_shared.testing_recorders import find_canaries

OFF = TeacherConfigView(show_solution=False, rubric_source="none")


class InProcessParser:
    """The sidecar's dispatch without the socket (same statuses, same text)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.profiles: list[str] = []

    async def parse_file(self, data: bytes, ext_hint: str = "") -> Any:
        self.calls.append("parse")
        return run("parse", data, ext_hint, max_pages=50, max_chars=200_000)

    async def sanitize_html(self, html: bytes) -> Any:
        self.calls.append("sanitize_html")
        return run("sanitize_html", html, "html", max_pages=1, max_chars=200_000)

    async def extract_review_item(
        self, html: bytes, profile: HtmlExtractionProfile
    ) -> tuple[str | None, str | None, bool]:
        """The real client's parsing of the sidecar's reply, without the socket: the
        provider's profile goes over the wire form the sidecar validates (ADR-0033 D4)."""
        import json

        self.calls.append("review_item")
        self.profiles.append(profile.profile_version)
        res = run(
            "review_item",
            html,
            "html",
            max_pages=1,
            max_chars=200_000,
            profile=wire_profile(profile),
        )
        doc = json.loads(res.text)
        return doc["question_text"], doc["response"], doc["truncated"]


def wire_profile(profile: HtmlExtractionProfile) -> ReviewProfile:
    """What ``ParserClient`` sends for ``profile`` (and the sidecar re-validates)."""
    return ReviewProfile.build(
        profile.keep, profile.drop_classes, profile.profile_version
    )


@pytest.fixture(params=BRANCHES)
def branch(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _env(branch: str) -> tuple[Replay, SiteReaders, dict[str, Any]]:
    """The recordings behind the Moodle provider, bound to the branch's site: Vektra's capture
    and canary sinks over the provider's recorded reviews (the joint guarantee, ADR-0033 D4)."""
    replay = Replay(branch)
    return replay, site_readers(replay), manifest(branch)


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
    # the provider's neutral kinds (ADR-0033 D2), not Moodle's qtype names
    for qtype in ("true_false", "multiple_choice", "short_answer", "numerical"):
        item = by_type[qtype]
        assert item.question_text and "VISIBLE-QTEXT" in item.question_text, qtype
        assert item.response_text, qtype
        assert not item.degraded, qtype
    for qtype in ("embedded_answers", "other", "matching", "gap_select"):
        assert by_type[qtype].question_text is None and by_type[qtype].degraded, qtype
    assert "information" not in by_type  # information items are not discussion items
    assert out.snapshot.degraded_count == sum(
        i.degraded for i in out.snapshot.quiz_items
    )
    blob = canonical_bytes(out.snapshot)
    assert find_canaries(
        blob, [c for c in _all_canaries(m) if "GENFB" in c]
    ) == dict.fromkeys([c for c in _all_canaries(m) if "GENFB" in c], 0)
    assert b"The correct answer" not in blob
    assert "sanitize_html" not in parser.calls  # the whole page is never sent
    # one sidecar call per extracted question, never two
    extracted = [
        i
        for i in out.snapshot.quiz_items
        if i.qtype
        in ("true_false", "multiple_choice", "short_answer", "numerical", "essay")
    ]
    assert parser.calls.count("review_item") == len(extracted)


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
    a = m["assigns"]["files"]
    status = await readers.submission(a["id"], uid(branch, user))
    assert isinstance(status, Readable)
    for f in status.value.files:
        local = FIXTURES / branch / "files" / f"{user}__{f.filename}"
        replay.files[urlsplit(f.locator).path] = local.read_bytes()
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


async def test_a_foreign_file_is_rejected_and_audited(branch: str) -> None:
    """REQ-101 (a): the reader re-checks ownership against the selected submission at download
    time; a file that is not in it (here: the listing changes between list and fetch) is refused."""
    replay, readers, m = _env(branch)
    a = m["assigns"]["files"]
    status = await readers.submission(a["id"], uid(branch, "stu1"))
    assert isinstance(status, Readable)

    def swap(wsfunction: str, body: Any) -> Any:
        if wsfunction == "mod_assign_get_submission_status":
            for plugin in body["lastattempt"]["submission"]["plugins"]:
                for area in plugin.get("fileareas", []):
                    for f in area.get("files", []):
                        f["filename"] = "someone_else_" + f["filename"]
        return body

    replay.mutate = swap
    audit: list[Any] = []
    target = AssignTarget(m["courseid"], a["id"], a["cmid"], uid(branch, "stu1"))
    out = await _files(
        target,
        status.value,
        readers,
        InProcessParser(),
        CaptureLimits(),
        audit,
    )
    assert {f.status for f in out} == {FileStatus.CORRUPT}
    assert {e.event for e in audit} == {"assess_file_rejected"}
    assert {e.reason_code for e in audit} == {"file_not_in_submission"}
    assert replay.file_requests == []  # nothing was downloaded
    assert all(f.text == "" for f in out)


async def test_file_bytes_never_touch_the_disk(
    branch: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-101 (f): bytes go from the WS response to the sidecar call and nowhere else."""
    import os
    import tempfile

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    tempfile.tempdir = None
    try:
        before = set(os.listdir(tmp_path))
        out, _, _ = await _intake(branch, "stu1")
        assert any(f.status is FileStatus.READ for f in out)
        assert set(os.listdir(tmp_path)) == before
    finally:
        tempfile.tempdir = None


@pytest.mark.parametrize(
    ("snippet", "must_keep"),
    [
        (
            "Calcola \\(\\int_0^1 x^2\\,dx\\) e \\[\\frac{a}{b}\\]",
            ["\\(\\int_0^1 x^2\\,dx\\)", "\\[\\frac{a}{b}\\]"],
        ),
        (
            '<math><semantics><mi>x</mi><annotation encoding="application/x-tex">x^{2}+1</annotation></semantics></math>',
            ["x^{2}+1"],
        ),
        ("<pre>def f(x):\n    return x * 2</pre>", ["def f(x):", "    return x * 2"]),
        (
            "<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>",
            ["| a | b |", "| 1 | 2 |"],
        ),
    ],
)
async def test_tex_mathml_code_and_tables_survive_into_the_context(
    snippet: str, must_keep: list[str]
) -> None:
    """REQ-101 AC4: the source is preserved verbatim in the assembled context."""
    from test_context_bundle import _frozen
    from test_context_snapshot import make

    from vektra_assess.context.assemble import assemble_context
    from vektra_assess.context.payload import bundle_to_model_payload
    from vektra_assess.context.snapshot import QuizItem

    html = f"<div class='que'><div class='qtext'>{snippet}</div><fieldset class='ablock'>[x] A</fieldset></div>"
    qtext, _response, _cut = await InProcessParser().extract_review_item(
        html.encode(), MOODLE_REVIEW_PROFILE
    )
    assert qtext
    item = QuizItem(
        label="1",
        qtype="essay",
        discussable=True,
        state="gradedright",
        status=None,
        mark="1",
        max_mark="1",
        question_text=qtext,
    )
    s = make().model_copy(update={"quiz_items": (item,)})
    payload = bundle_to_model_payload(assemble_context(s, _frozen(s)))[0].content
    for piece in must_keep:
        assert piece in payload, (piece, payload)


# --- regressions from the plan's code review ------------------------------------------------------


async def test_a_rubric_edited_after_grading_fails_closed(branch: str) -> None:
    """The filling points at a level the definition no longer has: NotEligible, never a crash."""
    replay, readers, m = _env(branch)
    a = m["assigns"]["rubric_vis"]

    def drop_levels(wsfunction: str, body: Any) -> Any:
        if wsfunction == "core_grading_get_definitions":
            for area in body.get("areas", []):
                for d in area.get("definitions", []):
                    for c in (d.get("rubric") or {}).get("rubric_criteria", []):
                        for lv in c.get("levels", []):
                            lv["id"] = (
                                lv["id"] + 100_000
                            )  # renumbered: no filling matches
        return body

    replay.mutate = drop_levels
    out = await capture_assign(
        AssignTarget(m["courseid"], a["id"], a["cmid"], uid(branch, "stu1")),
        readers, InProcessParser(), OFF, Capabilities(), moodle_release=m["release"],
    )  # fmt: skip
    assert out == CaptureNotEligible(NotEligibleReason.UNREADABLE)


async def test_only_the_selected_attempt_is_read_twice_and_reads_stop_early(
    branch: str,
) -> None:
    replay, readers, m = _env(branch)
    q = m["quizzes"]["gm_highest"]
    out = await capture_quiz(
        QuizTarget(m["courseid"], q["id"], q["cmid"], uid(branch, "stu1")),
        readers, InProcessParser(), OFF, Capabilities(), moodle_release=m["release"], now=1_800_000_000,
    )  # fmt: skip
    assert isinstance(out, Built)
    reviews = [p for f, p in replay.calls if f == "mod_quiz_get_attempt_review"]
    assert (
        len(reviews) == len(q["attempts"]) + 1
    )  # every attempt once, the chosen one again
    replay2, readers2, _m = _env(branch)
    e = m["quizzes"]["essay"]
    await capture_quiz(
        QuizTarget(m["courseid"], e["id"], e["cmid"], uid(branch, "stu2")),
        readers2, InProcessParser(), OFF, Capabilities(), moodle_release=m["release"],
    )  # fmt: skip
    assert [f for f, _ in replay2.calls].count(
        "mod_quiz_get_attempt_review"
    ) == 1  # stops at the ungraded one


async def test_review_options_closed_follows_the_quiz_close_date(branch: str) -> None:
    replay, readers, m = _env(branch)
    q = m["quizzes"]["gm_highest"]
    target = QuizTarget(m["courseid"], q["id"], q["cmid"], uid(branch, "stu1"))
    close = await readers.quiz_settings(m["courseid"], q["id"])
    assert isinstance(close, Readable)
    timeclose = close.value.time_close
    for now in (1, 2_000_000_000):
        out = await capture_quiz(
            target,
            readers,
            InProcessParser(),
            OFF,
            Capabilities(),
            moodle_release=m["release"],
            now=now,
        )
        assert isinstance(out, Built)
        assert out.snapshot.header.review_options_closed is (
            timeclose != 0 and now >= timeclose
        )

    def no_options(wsfunction: str, body: Any) -> Any:
        if wsfunction == "mod_quiz_get_combined_review_options":
            body["alloptions"] = body["alloptions"][:2]  # an unknown shape: unreadable
        return body

    replay.mutate = no_options
    out = await capture_quiz(
        target,
        readers,
        InProcessParser(),
        OFF,
        Capabilities(),
        moodle_release=m["release"],
        now=2_000_000_000,
    )
    assert isinstance(out, Built) and out.snapshot.header.review_options_closed is False


async def test_online_text_counts_against_the_total_budget_and_a_cut_names_no_page(
    branch: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vektra_assess.context.capture import capture as cap

    out, _, _ = await _intake(branch, "stu1", CaptureLimits(total_text_chars=10))
    partial = [f for f in out if f.status is FileStatus.READ_PARTIAL]
    assert partial and all(f.upto_page is None for f in partial)

    seen: dict[str, int] = {}
    real = cap._files

    async def spy(*args: Any, budget: int | None = None, **kw: Any) -> Any:
        seen["budget"] = budget if budget is not None else -1
        return await real(*args, budget=budget, **kw)

    monkeypatch.setattr(cap, "_files", spy)
    _replay, readers, m = _env(branch)
    a = m["assigns"]["simple"]
    limits = CaptureLimits(total_text_chars=1000)
    built = await capture_assign(
        AssignTarget(m["courseid"], a["id"], a["cmid"], uid(branch, "stu1")),
        readers, InProcessParser(), OFF, Capabilities(), moodle_release=m["release"], limits=limits,
    )  # fmt: skip
    assert isinstance(built, Built) and built.snapshot.submission is not None
    used = len(built.snapshot.submission.onlinetext or "")
    assert used > 0 and seen["budget"] == 1000 - used


async def test_online_text_never_exceeds_the_total_budget(branch: str) -> None:
    _replay, readers, m = _env(branch)
    a = m["assigns"]["simple"]
    limits = CaptureLimits(total_text_chars=5, onlinetext_chars=50_000)
    built = await capture_assign(
        AssignTarget(m["courseid"], a["id"], a["cmid"], uid(branch, "stu1")),
        readers, InProcessParser(), OFF, Capabilities(), moodle_release=m["release"], limits=limits,
    )  # fmt: skip
    assert isinstance(built, Built) and built.snapshot.submission is not None
    sub = built.snapshot.submission
    assert len(sub.onlinetext or "") == 5 and sub.truncated


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (FileStatus.CORRUPT, True),
        (FileStatus.TOO_LARGE, True),
        (FileStatus.NO_TEXT, None),
    ],
)
async def test_an_unreadable_online_text_is_flagged_not_dropped(
    branch: str, status: FileStatus, expected: bool | None
) -> None:
    from vektra_assess.parser_protocol import Extracted

    class Broken(InProcessParser):
        async def sanitize_html(self, html: bytes) -> Any:
            return Extracted(status, code="x")

    _replay, readers, m = _env(branch)
    a = m["assigns"]["simple"]
    built = await capture_assign(
        AssignTarget(m["courseid"], a["id"], a["cmid"], uid(branch, "stu1")),
        readers, Broken(), OFF, Capabilities(), moodle_release=m["release"],
    )  # fmt: skip
    assert isinstance(built, Built)
    sub = built.snapshot.submission
    if expected is None:
        assert sub is None  # blank once sanitized: nothing to report
    else:
        assert sub is not None and sub.onlinetext is None and sub.unreadable


class _SmallCapParser(InProcessParser):
    """The real sidecar extraction with a small char cap: the recorded items come back cut."""

    async def extract_review_item(
        self, html: bytes, profile: HtmlExtractionProfile
    ) -> tuple[str | None, str | None, bool]:
        import json

        self.calls.append("review_item")
        res = run(
            "review_item",
            html,
            "html",
            max_pages=1,
            max_chars=90,
            profile=wire_profile(profile),
        )
        doc = json.loads(res.text)
        return doc["question_text"], doc["response"], doc["truncated"]


class _NoQuestionParser(InProcessParser):
    """The question block cannot be isolated, a long response is extracted and cut."""

    async def extract_review_item(
        self, html: bytes, profile: HtmlExtractionProfile
    ) -> tuple[str | None, str | None, bool]:
        self.calls.append("review_item")
        return None, "risposta lunga " * 20, True


async def _quiz_with(branch: str, parser: InProcessParser) -> Any:
    replay, readers, m = _env(branch)
    q = m["quizzes"]["types"]
    target = QuizTarget(
        courseid=m["courseid"],
        quizid=q["id"],
        cmid=q["cmid"],
        userid=uid(branch, "stu1"),
    )
    out = await capture_quiz(
        target, readers, parser, OFF, Capabilities(),
        moodle_release=m["release"], now=1_800_000_000,
    )  # fmt: skip
    assert not replay.unmatched, replay.unmatched
    assert isinstance(out, Built), out
    return out


def _reloaded_texts(out: Any) -> tuple[Any, str, str]:
    """The snapshot as stored and read back, with its Part A and model payload."""
    from vektra_assess.context.assemble import assemble_context
    from vektra_assess.context.part_a import SessionFacts, render_part_a
    from vektra_assess.context.payload import bundle_to_model_payload
    from vektra_assess.context.snapshot import content_hash, from_canonical_bytes

    snap = from_canonical_bytes(canonical_bytes(out.snapshot))
    assert content_hash(snap) == content_hash(out.snapshot)
    bundle = assemble_context(snap, out.frozen_policy)
    return (
        snap,
        render_part_a(bundle, SessionFacts(1, 30)).text,
        bundle_to_model_payload(bundle)[0].content,
    )


async def test_a_long_non_degraded_item_stays_truncated_after_reload(
    branch: str,
) -> None:
    out = await _quiz_with(branch, _SmallCapParser())
    snap, part_a, payload = _reloaded_texts(out)
    cut = [i for i in snap.quiz_items if i.truncated and not i.degraded]
    assert cut, "the small cap must cut at least one extracted item"
    for item in cut:
        line = next(ln for ln in part_a.splitlines() if f"Domanda {item.label}:" in ln)
        assert "troncato" in line  # Part A warning
    assert "testo della domanda o della risposta troncato" in payload  # payload warning


async def test_a_missing_question_does_not_clear_a_cut_response(branch: str) -> None:
    out = await _quiz_with(branch, _NoQuestionParser())
    snap, part_a, payload = _reloaded_texts(out)
    extracted = [i for i in snap.quiz_items if i.response_text]
    assert extracted
    for item in extracted:
        assert item.degraded and item.truncated
        line = next(ln for ln in part_a.splitlines() if f"Domanda {item.label}:" in ln)
        assert "vede solo la tua risposta" in line and "risposta troncata" in line
    assert "risposta dello studente disponibile ma troncata" in payload
