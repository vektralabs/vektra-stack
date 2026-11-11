"""BR-012 attempt selection and BR-014 "fully graded".

The pure cases run on the neutral values of ``vektra_shared.lms`` (what any LMS provider hands
the vertical). The recorded cases run the same selection on the 4.3 / 5.2 responses through the
Moodle provider, so its state mapping is checked end to end; expected attempts come from the SEED
MANIFEST (what the seeder created), never from the LMS.
"""

from __future__ import annotations

from typing import Any

import pytest
from assess_moodle_support import BRANCHES, Replay, manifest, site_readers, uid

from vektra_assess.context.capture.selection import (
    NotEligible,
    NotEligibleReason,
    Selected,
    assign_fully_graded,
    is_fully_graded,
    select_attempt,
)
from vektra_assess.context.qtypes import is_final
from vektra_shared.lms import (
    AttemptReview,
    AttemptState,
    AttemptSummary,
    QuestionKind,
    QuestionState,
    Readable,
    ReviewItem,
    Unreadable,
    UnreadableReason,
)


@pytest.fixture(params=BRANCHES)
def branch(request: pytest.FixtureRequest) -> str:
    return str(request.param)


async def _select(branch: str, quiz: str, user: str, *, released: bool = True) -> Any:
    replay = Replay(branch)
    lms, m = site_readers(replay), manifest(branch)
    q = m["quizzes"][quiz]
    attempts = await lms.attempts(q["id"], uid(branch, user))
    assert isinstance(attempts, Readable), attempts
    graded: dict[int, bool | None] = {}
    for a in attempts.value:
        if a.state is AttemptState.FINISHED:
            review = await lms.attempt_review(a.attempt_id)
            graded[a.attempt_id] = (
                is_fully_graded(review.value) if isinstance(review, Readable) else None
            )
    out = select_attempt(
        attempts, q["expected"]["grademethod"], graded, released=released
    )
    assert not replay.unmatched
    return out, q


async def test_highest_tie_goes_to_the_earliest_attempt(branch: str) -> None:
    out, q = await _select(branch, "gm_highest", "stu1")
    assert isinstance(out, Selected)
    ties = q["expected"]["highest_ties_attempt_numbers"]["stu1"]
    assert len(ties) > 1 and out.attempt_number == min(ties)
    assert out.attempt_count == len(q["attempts"])


@pytest.mark.parametrize(
    ("quiz", "expected"), [("gm_first", 1), ("gm_last", 4), ("gm_average", 4)]
)
async def test_first_last_and_average(branch: str, quiz: str, expected: int) -> None:
    out, _ = await _select(branch, quiz, "stu1")
    assert isinstance(out, Selected) and out.attempt_number == expected


async def test_an_essay_awaiting_grading_is_not_eligible(branch: str) -> None:
    graded, _ = await _select(branch, "essay", "stu1")
    pending, _ = await _select(branch, "essay", "stu2")
    assert isinstance(graded, Selected)  # manually graded (mangrpartial)
    assert pending == NotEligible(NotEligibleReason.NOT_GRADED)


@pytest.mark.parametrize(
    ("quiz", "reason"),
    [
        ("inprogress", NotEligibleReason.IN_PROGRESS),
        ("overdue", NotEligibleReason.IN_PROGRESS),
        ("abandoned", NotEligibleReason.NO_SUBMISSION),
    ],
)
async def test_open_or_abandoned_attempts(
    branch: str, quiz: str, reason: NotEligibleReason
) -> None:
    out, _ = await _select(branch, quiz, "stu1")
    assert out == NotEligible(reason)


async def test_a_description_item_does_not_block_and_unreleased_blocks(
    branch: str,
) -> None:
    out, _ = await _select(branch, "types", "stu1")
    assert isinstance(
        out, Selected
    )  # the attempt holds a description and a zero-mark item
    hidden, _ = await _select(branch, "gm_highest", "stu1", released=False)
    assert hidden == NotEligible(NotEligibleReason.NOT_RELEASED)


# --- pure edge cases ----------------------------------------------------------------------------


def _a(
    n: int,
    state: AttemptState = AttemptState.FINISHED,
    s: float | None = 1.0,
    **kw: Any,
) -> AttemptSummary:
    return AttemptSummary(
        attempt_id=100 + n,
        attempt_number=n,
        user_id=1,
        state=state,
        preview=kw.get("preview", False),
        time_start=0,
        time_finish=1,
        sum_grades=s,
    )


def test_unreadable_attempts_or_review_never_guess() -> None:
    assert select_attempt(
        Unreadable(UnreadableReason.TIMEOUT), "highest", {}, released=True
    ) == (NotEligible(NotEligibleReason.UNREADABLE))
    out = select_attempt(Readable((_a(1),)), "highest", {101: None}, released=True)
    assert out == NotEligible(NotEligibleReason.UNREADABLE)
    assert select_attempt(Readable((_a(1),)), "median", {101: True}, released=True) == (
        NotEligible(NotEligibleReason.UNSUPPORTED)
    )


def test_previews_are_ignored_and_not_counted() -> None:
    attempts = Readable((_a(1, preview=True, state=AttemptState.IN_PROGRESS), _a(2)))
    out = select_attempt(attempts, "last", {102: True}, released=True)
    assert isinstance(out, Selected) and (out.attempt_number, out.attempt_count) == (
        2,
        1,
    )


K, S = QuestionKind, QuestionState


def _slot(
    kind: QuestionKind,
    state: QuestionState,
    mark: float | None,
    maxmark: float,
    needs: bool = False,
) -> ReviewItem:
    return ReviewItem(
        slot=1,
        display_number="1",
        question_kind=kind,
        state=state,
        status_label=None,
        mark=mark,
        max_mark=maxmark,
        needs_grading=needs,
        html=None,
    )


@pytest.mark.parametrize(
    ("slot", "graded"),
    [
        (_slot(K.MULTIPLE_CHOICE, S.FINAL_MARKED, 1, 1), True),
        (_slot(K.INFORMATION, S.UNKNOWN, None, 0), True),
        (_slot(K.TRUE_FALSE, S.UNKNOWN, None, 0), True),  # zero-mark: ignored
        (_slot(K.ESSAY, S.PENDING, None, 5, needs=True), False),
        (_slot(K.ESSAY, S.PENDING, None, 5), False),
        (_slot(K.MULTIPLE_CHOICE, S.UNKNOWN, 1, 1), False),  # unknown: fail closed
        (_slot(K.MATCHING, S.FINAL_NO_MARK, None, 1), True),  # not answered: no mark
        (_slot(K.MULTIPLE_CHOICE, S.FINAL_MARKED, None, 1), False),  # no mark: refused
        (
            _slot(K.OTHER, S.FINAL_MARKED, 1, 1),
            True,
        ),  # an unplaced kind is still graded
    ],
)
def test_is_fully_graded(slot: ReviewItem, graded: bool) -> None:
    review = AttemptReview(1, 1, 1, AttemptState.FINISHED, 1.0, 1.0, (slot,))
    assert is_fully_graded(review) is graded


def test_regrade_in_progress_counts_as_not_graded() -> None:
    review = AttemptReview(
        1,
        1,
        1,
        AttemptState.FINISHED,
        None,
        None,
        (_slot(K.MULTIPLE_CHOICE, S.FINAL_MARKED, 1, 1),),
    )
    assert is_fully_graded(review) is False


@pytest.mark.parametrize("state", list(QuestionState))
def test_an_unknown_provider_state_is_never_final(state: QuestionState) -> None:
    """ADR-0033 D2: a state the provider could not place is never final, so the attempt it
    belongs to is never "fully graded" (fail closed, BR-014)."""
    assert is_final(state) is (
        state in (QuestionState.FINAL_MARKED, QuestionState.FINAL_NO_MARK)
    )
    if state is QuestionState.UNKNOWN:
        review = AttemptReview(
            1, 1, 1, AttemptState.FINISHED, 1.0, 1.0, (_slot(K.ESSAY, state, 1, 1),)
        )
        assert is_fully_graded(review) is False


@pytest.mark.parametrize(
    ("method", "grade", "crit", "filled", "out"),
    [
        ("rubric", 7.0, [1, 2], {1: 3, 2: 1}, True),
        ("rubric", 7.0, [1, 2], {1: 3, 2: None}, False),
        ("rubric", 7.0, [1, 2], {1: 3}, False),
        ("rubric", None, [1], {1: 1}, False),
        ("none", 6.0, None, None, True),
        ("simple", 6.0, None, None, True),
        ("none", None, None, None, False),
        ("guide", 6.0, None, None, NotEligible(NotEligibleReason.UNSUPPORTED)),
        ("checklist", 6.0, None, None, NotEligible(NotEligibleReason.UNSUPPORTED)),
    ],
)
def test_assign_fully_graded(
    method: str, grade: float | None, crit: Any, filled: Any, out: Any
) -> None:
    assert assign_fully_graded(method, grade, crit, filled) == out
