"""BR-012 attempt selection on the recorded 4.3 / 5.2 responses, through the Moodle provider, so
its state mapping is checked end to end; expected attempts come from the SEED MANIFEST (what the
seeder created), never from the LMS. The pure cases are the vertical's
(``vektra-assess/tests/test_context_selection.py``).
"""

from __future__ import annotations

from typing import Any

import pytest
from assess_moodle_support import BRANCHES, Replay, manifest, site_readers, uid

from vektra_assess.context.capture.selection import (
    NotEligible,
    NotEligibleReason,
    Selected,
    is_fully_graded,
    select_attempt,
)
from vektra_shared.lms import (
    AttemptState,
    Readable,
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
