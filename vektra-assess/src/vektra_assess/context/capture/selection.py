"""Which attempt is discussed (BR-012) and whether it is fully graded (BR-014). Pure functions.

The backend selects, never the client, and only when EVERY attempt of the student is finished
(abandoned attempts are ignored), at least one was submitted, and every considered attempt is
fully graded and released. Anything unreadable is ``NotEligible(UNREADABLE)``: the function never
guesses.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from vektra_assess.context.qtypes import (
    INFORMATION_KINDS,
    NO_MARK_FINAL_STATES,
    is_final,
)
from vektra_shared.lms import (
    AttemptReview,
    AttemptState,
    AttemptSummary,
    GradeMethod,
    Readable,
    Reading,
    Unreadable,
)


class NotEligibleReason(StrEnum):
    UNREADABLE = "unreadable"
    IN_PROGRESS = "in_progress"  # an attempt is still open or overdue
    NO_SUBMISSION = "no_submission"
    NOT_GRADED = "not_graded"
    NOT_RELEASED = "not_released"
    UNSUPPORTED = "unsupported"  # marking guide, unknown grading method


@dataclass(frozen=True)
class NotEligible:
    reason: NotEligibleReason


@dataclass(frozen=True)
class Selected:
    attempt: AttemptSummary
    attempt_number: int  # "#k"
    attempt_count: int  # "di M": the student's attempts, previews excluded


_OPEN = {AttemptState.IN_PROGRESS, AttemptState.OVERDUE}


def is_fully_graded(review: AttemptReview) -> bool:
    """Quiz BR-014: every item with max > 0 has a final mark; info and zero-mark items ignored.
    A state the provider could not place (``UNKNOWN``) is never final (fail closed)."""
    for item in review.items:
        if item.question_kind in INFORMATION_KINDS or item.max_mark <= 0:
            continue
        if item.needs_grading or not is_final(item.state):
            return False
        # an unanswered question is final with no mark ("requires grading" = 0); any other
        # final state without a mark is inconsistent and fails closed
        if item.mark is None and item.state not in NO_MARK_FINAL_STATES:
            return False
    return review.sum_grades is not None


def select_attempt(
    attempts: Reading[tuple[AttemptSummary, ...]],
    grade_method: GradeMethod | str,
    fully_graded: Mapping[int, bool | None],
    *,
    released: bool,
) -> Selected | NotEligible:
    """``fully_graded`` maps attempt id -> ``is_fully_graded`` (None: its review was unreadable)."""
    if isinstance(attempts, Unreadable):
        return NotEligible(NotEligibleReason.UNREADABLE)
    assert isinstance(attempts, Readable)
    if grade_method not in ("highest", "average", "first", "last"):
        return NotEligible(NotEligibleReason.UNSUPPORTED)
    real = [a for a in attempts.value if not a.preview]
    if any(a.state in _OPEN for a in real):
        return NotEligible(NotEligibleReason.IN_PROGRESS)
    finished = sorted(
        (a for a in real if a.state is AttemptState.FINISHED),
        key=lambda a: a.attempt_number,
    )
    if not finished:
        return NotEligible(NotEligibleReason.NO_SUBMISSION)
    for a in finished:
        graded = fully_graded.get(a.attempt_id)
        if graded is None:
            return NotEligible(NotEligibleReason.UNREADABLE)
        if not graded or a.sum_grades is None:
            return NotEligible(NotEligibleReason.NOT_GRADED)
    if not released:
        return NotEligible(NotEligibleReason.NOT_RELEASED)
    chosen = _choose(finished, str(grade_method))
    return Selected(chosen, chosen.attempt_number, len(real))


def _choose(finished: Sequence[AttemptSummary], grade_method: str) -> AttemptSummary:
    if grade_method == "first":
        return finished[0]
    if grade_method in ("last", "average"):
        # average: the grade is a mean, no attempt determines it; discuss the last one
        return finished[-1]
    best = max(a.sum_grades or 0.0 for a in finished)
    # highest: ties go to the EARLIEST attempt (finished is sorted by attempt number)
    return next(a for a in finished if (a.sum_grades or 0.0) == best)


# --- assignments ---------------------------------------------------------------------------------


def assign_fully_graded(
    grading_method: str,
    grade: float | None,
    criterion_ids: Sequence[int] | None,
    filled_levels: Mapping[int, int | None] | None,
) -> bool | NotEligible:
    """Assignment BR-014. Rubric: grade set and every criterion has a level. Simple direct or
    scale: grade set. Marking guide: not supported in v1 (EX-023). Unknown method: unsupported."""
    if grading_method == "guide":
        return NotEligible(NotEligibleReason.UNSUPPORTED)
    if grading_method not in ("none", "simple", "rubric", ""):
        return NotEligible(NotEligibleReason.UNSUPPORTED)
    if grade is None:
        return False
    if grading_method != "rubric":
        return True
    if not criterion_ids or filled_levels is None:
        return False
    return all(filled_levels.get(c) is not None for c in criterion_ids)
