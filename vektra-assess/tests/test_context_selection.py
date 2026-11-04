"""BR-012 attempt selection and BR-014 "fully graded", on the recorded 4.3 / 5.2 responses.

Expected attempts come from the SEED MANIFEST (what the seeder created), never from Moodle.
"""

from __future__ import annotations

from typing import Any

import pytest
from assess_moodle_support import BRANCHES, uid
from test_moodle_readers import env, ok

from vektra_assess.context.capture.selection import (
    NotEligible,
    NotEligibleReason,
    Selected,
    assign_fully_graded,
    is_fully_graded,
    select_attempt,
)
from vektra_assess.moodle.domain import (
    AttemptReview,
    AttemptState,
    AttemptSummary,
    ReviewSlot,
)
from vektra_assess.moodle.readers import read_attempt_review, read_user_attempts
from vektra_assess.moodle.result import Readable, Unreadable, UnreadableReason


@pytest.fixture(params=BRANCHES)
def branch(request: pytest.FixtureRequest) -> str:
    return str(request.param)


async def _select(branch: str, quiz: str, user: str, *, released: bool = True) -> Any:
    replay, client, site, m = env(branch)
    q = m["quizzes"][quiz]
    attempts = await read_user_attempts(client, site, q["id"], uid(branch, user))
    graded: dict[int, bool | None] = {}
    for a in ok(attempts):
        if a.state is AttemptState.FINISHED:
            review = await read_attempt_review(client, site, a.attemptid)
            graded[a.attemptid] = (
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
        attemptid=100 + n,
        attempt_number=n,
        userid=1,
        state=state,
        preview=kw.get("preview", False),
        timestart=0,
        timefinish=1,
        sumgrades=s,
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


def _slot(
    qtype: str,
    state: str | None,
    mark: float | None,
    maxmark: float,
    needs: bool = False,
) -> ReviewSlot:
    return ReviewSlot(
        slot=1,
        display_number="1",
        qtype=qtype,
        state=state,
        status=None,
        mark=mark,
        maxmark=maxmark,
        needs_grading=needs,
        html=None,
    )


@pytest.mark.parametrize(
    ("slot", "graded"),
    [
        (_slot("multichoice", "gradedright", 1, 1), True),
        (_slot("description", None, None, 0), True),
        (_slot("truefalse", None, None, 0), True),  # zero-mark: ignored
        (_slot("essay", "needsgrading", None, 5, needs=True), False),
        (_slot("essay", "complete", None, 5), False),
        (_slot("multichoice", "weird_new_state", 1, 1), False),  # unknown: fail closed
        (_slot("multichoice", None, 1, 1), False),
        (_slot("match", "gaveup", None, 1), True),  # not answered: final, no mark
        (_slot("multichoice", "gradedright", None, 1), False),  # graded without a mark
    ],
)
def test_is_fully_graded(slot: ReviewSlot, graded: bool) -> None:
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
        (_slot("multichoice", "gradedright", 1, 1),),
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
