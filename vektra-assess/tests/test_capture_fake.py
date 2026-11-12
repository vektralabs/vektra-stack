"""capture_quiz / capture_assign on ``FakeLmsProvider``: the vertical's capture logic on neutral
values alone (ADR-0033 D1, D2). The Moodle provider's recorded reviews are exercised by
``test_context_capture.py`` (the joint BR-011 guarantee of D4).

- a provider whose read raises fails the capture closed, never with an exception (the
  ``SiteReaders`` guard, D2 invariant 1);
- a question state the provider could not place (``UNKNOWN``) is never "fully graded";
- a question kind it could not place (``OTHER``) is degraded and never extracted;
- the sidecar receives the provider's extraction profile with each review HTML (D4);
- the snapshot carries the neutral kind and state, never a provider's wire vocabulary;
- what the LMS hides (right answer, hidden feedback, unreleased grade: canaries planted in the
  fake's ``hidden``) never reaches the snapshot, and the solution is never in it with the toggle
  OFF.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from support.fake_lms import (
    ASSIGN_ACTIVITY,
    ASSIGNMENT,
    ATTEMPT,
    CANARIES,
    COURSE,
    PROFILE,
    QUIZ,
    QUIZ_ACTIVITY,
    RIGHT_ANSWER,
    SITE,
    USER,
    FakeLmsProvider,
    default_world,
)
from support.parser import OFF, InProcessParser

from vektra_assess.context.capture.bound import SiteReaders
from vektra_assess.context.capture.capture import (
    AssignTarget,
    Built,
    Capabilities,
    CaptureNotEligible,
    Failed,
    FailReason,
    QuizTarget,
    TeacherConfigView,
    capture_assign,
    capture_quiz,
)
from vektra_assess.context.capture.selection import NotEligibleReason
from vektra_assess.context.snapshot import canonical_bytes
from vektra_assess.parser_protocol import FileStatus
from vektra_shared.lms import (
    PROVIDER_ERROR,
    QuestionKind,
    QuestionState,
    ReviewOptions,
    ReviewVisibility,
    Unreadable,
    UnreadableReason,
)
from vektra_testkit.recorders import find_canaries

NOW = 1_800_000_000
QUIZ_TARGET = QuizTarget(COURSE, QUIZ, QUIZ_ACTIVITY, USER)
ASSIGN_TARGET = AssignTarget(COURSE, ASSIGNMENT, ASSIGN_ACTIVITY, USER)


def _with_item(**changes: Any) -> Any:
    """The default world with its one reviewed item changed."""
    w = default_world()
    review = w.reviews[ATTEMPT]
    item = dataclasses.replace(review.items[0], **changes)
    w.reviews[ATTEMPT] = dataclasses.replace(review, items=(item,))
    return w


async def _quiz(
    fake: FakeLmsProvider, parser: InProcessParser | None = None
) -> tuple[Any, InProcessParser]:
    parser = parser or InProcessParser()
    out = await capture_quiz(
        QUIZ_TARGET,
        SiteReaders(fake, SITE),
        parser,
        OFF,
        Capabilities(),
        lms_release="1.0",
        now=NOW,
    )
    return out, parser


async def _assign(fake: FakeLmsProvider) -> tuple[Any, InProcessParser]:
    parser = InProcessParser()
    out = await capture_assign(
        ASSIGN_TARGET,
        SiteReaders(fake, SITE),
        parser,
        OFF,
        Capabilities(),
        lms_release="1.0",
        now=NOW,
    )
    return out, parser


async def test_a_quiz_is_captured_on_neutral_values_with_the_providers_profile() -> (
    None
):
    fake = FakeLmsProvider()
    out, parser = await _quiz(fake)
    assert isinstance(out, Built), out
    (item,) = out.snapshot.quiz_items
    # the neutral vocabulary, never a provider's wire names
    assert item.qtype == QuestionKind.MULTIPLE_CHOICE.value
    assert item.state == QuestionState.FINAL_MARKED.value
    assert item.discussable and not item.degraded
    assert item.question_text == "2 + 2?"
    # one sidecar call, carrying the profile the provider shipped with the HTML (ADR-0033 D4)
    assert parser.calls.count("review_item") == 1
    assert parser.profiles == [PROFILE.profile_version]


async def test_the_hidden_material_never_reaches_the_snapshot() -> None:
    fake = FakeLmsProvider()
    canaries = list(CANARIES)
    # the right-answer canary IS in the teacher-visible review HTML: the check is not vacuous
    html = fake.world.reviews[ATTEMPT].items[0].html
    assert html is not None and RIGHT_ANSWER in html.html
    for out, _parser in (await _quiz(fake), await _assign(fake)):
        assert isinstance(out, Built), out
        assert out.snapshot.solution_text is None  # toggle OFF: never in the snapshot
        assert "solution_text" in out.snapshot.exclusion.excluded_paths
        hits = find_canaries(canonical_bytes(out.snapshot), canaries)
        assert not any(hits.values()), hits


def test_the_solution_cannot_even_be_handed_to_capture_with_the_toggle_off() -> None:
    with pytest.raises(ValueError):
        TeacherConfigView(show_solution=False, rubric_source="none", solution_text="x")


async def test_an_unknown_provider_state_is_never_fully_graded() -> None:
    fake = FakeLmsProvider(_with_item(state=QuestionState.UNKNOWN))
    out, _ = await _quiz(fake)
    assert out == CaptureNotEligible(NotEligibleReason.NOT_GRADED)


async def test_a_pending_item_is_not_graded_either() -> None:
    fake = FakeLmsProvider(_with_item(state=QuestionState.PENDING, needs_grading=True))
    out, _ = await _quiz(fake)
    assert out == CaptureNotEligible(NotEligibleReason.NOT_GRADED)


async def test_an_unplaced_kind_is_degraded_and_never_extracted() -> None:
    fake = FakeLmsProvider(_with_item(question_kind=QuestionKind.OTHER))
    out, parser = await _quiz(fake)
    assert isinstance(out, Built), out
    (item,) = out.snapshot.quiz_items
    assert item.qtype == "other" and item.degraded and not item.discussable
    assert item.question_text is None and item.response_text is None
    assert "review_item" not in parser.calls  # the HTML never went to the sidecar
    assert out.snapshot.degraded_count == 1


async def test_an_information_item_is_not_a_discussion_item() -> None:
    fake = FakeLmsProvider(
        _with_item(question_kind=QuestionKind.INFORMATION, max_mark=0.0, mark=None)
    )
    out, _ = await _quiz(fake)
    assert isinstance(out, Built), out
    assert out.snapshot.quiz_items == ()


async def test_hidden_marks_hide_the_state_and_the_status_label() -> None:
    w = default_world()
    hidden = ReviewVisibility(True, True, False, True, 0)
    w.review_options[(QUIZ, USER)] = ReviewOptions(hidden, hidden)
    out, _ = await _quiz(FakeLmsProvider(w))
    assert isinstance(out, Built), out
    (item,) = out.snapshot.quiz_items
    assert item.state is None and item.status is None and item.mark is None


QUIZ_POLICY_READS = ("quiz_settings", "grade_item")
QUIZ_OTHER_READS = ("attempts", "attempt_review")
ASSIGN_POLICY_READS = (
    "assignment_settings",
    "assignment_release",
    "grading_definition",
)


@pytest.mark.parametrize(
    "member", QUIZ_POLICY_READS + QUIZ_OTHER_READS + ("review_options",)
)
async def test_a_raising_quiz_read_fails_closed_never_raises(member: str) -> None:
    fake = FakeLmsProvider().escape(member, RuntimeError("boom with secret"))
    out, _ = await _quiz(fake)
    assert fake.count(member) >= 1  # the faulty member was really called
    if member in QUIZ_POLICY_READS:
        assert out == Failed(FailReason.POLICY_INPUT_UNREADABLE)
    elif member in QUIZ_OTHER_READS:
        assert out == CaptureNotEligible(NotEligibleReason.UNREADABLE)
    else:
        # unreadable review options: the fail-closed visibility set, never a guess
        assert isinstance(out, Built), out
        assert out.snapshot.header.review_options_closed is False


@pytest.mark.parametrize(
    "member", (*ASSIGN_POLICY_READS, "submission", "rubric_fillings", "submission_file")
)
async def test_a_raising_assignment_read_fails_closed_never_raises(member: str) -> None:
    fake = FakeLmsProvider().escape(member, RuntimeError("boom"))
    out, _ = await _assign(fake)
    assert fake.count(member) >= 1
    if member in ASSIGN_POLICY_READS:
        assert out == Failed(FailReason.POLICY_INPUT_UNREADABLE)
    elif member == "submission":
        assert out == Failed(
            FailReason.SUBMISSION_UNREADABLE, UnreadableReason.UNKNOWN_SHAPE.value
        )
    elif member == "rubric_fillings":
        assert out == CaptureNotEligible(NotEligibleReason.UNREADABLE)
    else:
        # a file that cannot be fetched is CORRUPT, the rest of the capture stands
        assert isinstance(out, Built), out
        assert [f.status for f in out.snapshot.files] == [FileStatus.CORRUPT]


async def test_the_guard_maps_an_escape_to_the_provider_error_reading() -> None:
    fake = FakeLmsProvider().escape("quiz_settings", KeyError("x"))
    got = await SiteReaders(fake, SITE).quiz_settings(COURSE, QUIZ)
    assert got == Unreadable(UnreadableReason.UNKNOWN_SHAPE, PROVIDER_ERROR)


async def test_an_assignment_is_captured_on_neutral_values() -> None:
    out, parser = await _assign(FakeLmsProvider())
    assert isinstance(out, Built), out
    (criterion,) = out.snapshot.criteria
    assert (criterion.level_index, criterion.level_count) == (2, 2)
    assert criterion.remark == "clear"
    assert [(f.filename, f.status) for f in out.snapshot.files] == [
        ("essay.txt", FileStatus.READ)
    ]
    assert "parse" in parser.calls
