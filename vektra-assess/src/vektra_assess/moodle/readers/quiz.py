"""Quiz readers (plan group C): close inputs, attempts, attempt review, review options.

Spike traps handled here:
- trap 2 (caller-relative nulls): ``graceperiod``, ``overduehandling`` and the quiz-level
  ``review*`` options are absent/null for the service account once the quiz is closed. They are
  ``Unreadable(partial)``, never "zero" or "none";
- trap 4 (review HTML with hidden content): ``ReviewSlot.html`` is a separate field that the
  capture step drops with ``AttemptReview.without_html()``;
- ``mod_quiz_get_overrides`` exists only on Moodle 5.2: on 4.3 the overrides are
  ``Unreadable(function_not_in_service)`` WITHOUT calling the site (REQ-091 becomes the path).
"""

from __future__ import annotations

from typing import Any

from vektra_assess.moodle.client import MoodleWsClient, SiteRef
from vektra_assess.moodle.domain import (
    AttemptReview,
    AttemptState,
    AttemptSummary,
    GracePolicy,
    QuizCloseInputs,
    QuizOverride,
    ReviewSlot,
    ReviewVisibility,
    UserReviewOptions,
)
from vektra_assess.moodle.models import branch_of
from vektra_assess.moodle.readers._support import fetch, reader
from vektra_assess.moodle.result import Readable, Reading, Unreadable, UnreadableReason

_GRADEMETHOD = {1: "highest", 2: "average", 3: "first", 4: "last"}
_AFTER_CLOSE = 0x10  # quiz review bitmask: "after the quiz is closed"
_REVIEW_FIELDS = (
    "reviewattempt",
    "reviewcorrectness",
    "reviewmarks",
    "reviewmaxmarks",
    "reviewspecificfeedback",
    "reviewgeneralfeedback",
    "reviewrightanswer",
    "reviewoverallfeedback",
)


def _grace(q: Any) -> Reading[GracePolicy]:
    if q.graceperiod is None or q.overduehandling is None:
        return Unreadable(UnreadableReason.PARTIAL, "caller_relative_grace")
    return Readable(GracePolicy(int(q.graceperiod), str(q.overduehandling)))


def _after(mask: int) -> bool:
    return bool(mask & _AFTER_CLOSE)


def _review(q: Any) -> Reading[ReviewVisibility]:
    if any(getattr(q, f) is None for f in _REVIEW_FIELDS):
        return Unreadable(UnreadableReason.PARTIAL, "caller_relative_review")
    marks = 2 if _after(q.reviewmarks) else (1 if _after(q.reviewmaxmarks) else 0)
    return Readable(
        ReviewVisibility(
            feedback=_after(q.reviewspecificfeedback),
            generalfeedback=_after(q.reviewgeneralfeedback),
            rightanswer=_after(q.reviewrightanswer),
            overallfeedback=_after(q.reviewoverallfeedback),
            marks=marks,
        )
    )


@reader
async def read_quiz_overrides(
    client: MoodleWsClient, site: SiteRef, quizid: int
) -> Reading[tuple[QuizOverride, ...]]:
    """User and group overrides of ALL participants (5.2 only)."""
    if branch_of(site.release) == "4.3":
        return Unreadable(
            UnreadableReason.FUNCTION_NOT_IN_SERVICE, "no_override_function"
        )
    res = await fetch(client, site, "mod_quiz_get_overrides", {"quizid": quizid})
    if isinstance(res, Unreadable):
        return res
    rows = tuple(
        QuizOverride(
            userid=o.userid,
            groupid=o.groupid,
            timeopen=o.timeopen,
            timeclose=o.timeclose,
            timelimit_s=o.timelimit,
            attempts=o.attempts,
        )
        for o in res.value.overrides
    )
    return Readable(rows, res.evidence)


@reader
async def read_quiz_close_inputs(
    client: MoodleWsClient, site: SiteRef, courseid: int, quizid: int
) -> Reading[QuizCloseInputs]:
    """Quiz settings that decide the close (BR-007) plus the overrides of everybody."""
    res = await fetch(
        client, site, "mod_quiz_get_quizzes_by_courses", {"courseids": [courseid]}
    )
    if isinstance(res, Unreadable):
        return res
    found = [q for q in res.value.quizzes if q.id == quizid]
    if len(found) != 1:
        return Unreadable(UnreadableReason.PARTIAL, "quiz_not_found")
    q = found[0]
    method = _GRADEMETHOD.get(q.grademethod)
    if method is None:
        return Unreadable(UnreadableReason.UNKNOWN_SHAPE, "grademethod")
    return Readable(
        QuizCloseInputs(
            quizid=q.id,
            courseid=q.course,
            name=q.name,
            timeopen=q.timeopen,
            timeclose=q.timeclose,
            timelimit_s=q.timelimit,
            grademethod=method,
            attempts_allowed=q.attempts,
            grace=_grace(q),
            review_after_close=_review(q),
            overrides=await read_quiz_overrides(client, site, quizid),
        ),
        res.evidence,
    )


def _state(value: str) -> AttemptState | None:
    try:
        return AttemptState(value)
    except ValueError:
        return None


@reader
async def read_user_attempts(
    client: MoodleWsClient, site: SiteRef, quizid: int, userid: int
) -> Reading[tuple[AttemptSummary, ...]]:
    """Every attempt of one user, any state (BR-006(b), BR-012)."""
    res = await fetch(
        client,
        site,
        "mod_quiz_get_user_attempts",
        {"quizid": quizid, "userid": userid, "status": "all"},
    )
    if isinstance(res, Unreadable):
        return res
    out: list[AttemptSummary] = []
    for a in res.value.attempts:
        state = _state(a.state)
        if state is None:
            return Unreadable(UnreadableReason.UNKNOWN_SHAPE, "attempt_state")
        out.append(
            AttemptSummary(
                attemptid=a.id,
                attempt_number=a.attempt,
                userid=a.userid,
                state=state,
                preview=bool(a.preview),
                timestart=a.timestart,
                timefinish=a.timefinish,
                sumgrades=None if a.sumgrades is None else float(a.sumgrades),
            )
        )
    return Readable(tuple(sorted(out, key=lambda x: x.attempt_number)), res.evidence)


def _mark(text: str) -> float | None:
    return float(text) if text.strip() else None


@reader
async def read_attempt_review(
    client: MoodleWsClient, site: SiteRef, attemptid: int
) -> Reading[AttemptReview]:
    """Per slot: display number, question type, state (incl. needs-grading), mark, maxmark.

    The rendered HTML is kept in ``ReviewSlot.html`` ONLY so the capture step can decide; it
    always contains the hidden general feedback and right-answer block (trap 4), so nothing
    derived from it may reach a model: call ``without_html()`` first.
    """
    res = await fetch(
        client, site, "mod_quiz_get_attempt_review", {"attemptid": attemptid}
    )
    if isinstance(res, Unreadable):
        return res
    doc = res.value
    state = _state(doc.attempt.state)
    if state is None:
        return Unreadable(UnreadableReason.UNKNOWN_SHAPE, "attempt_state")
    slots = tuple(
        ReviewSlot(
            slot=s.slot,
            display_number=s.questionnumber,
            qtype=s.type,
            state=s.state,
            status=s.status,
            mark=_mark(s.mark),
            maxmark=float(s.maxmark),
            needs_grading=s.state == "needsgrading",
            html=s.html,
        )
        for s in doc.questions
    )
    sumgrades = doc.attempt.sumgrades
    return Readable(
        AttemptReview(
            attemptid=doc.attempt.id,
            quizid=doc.attempt.quiz,
            userid=doc.attempt.userid,
            state=state,
            sumgrades=None if sumgrades is None else float(sumgrades),
            grade=None if doc.grade is None else float(doc.grade),
            slots=slots,
        ),
        res.evidence,
    )


def _visibility(options: list[Any]) -> ReviewVisibility | None:
    by_name = {o.name: o.value for o in options}
    needed = ("feedback", "generalfeedback", "rightanswer", "overallfeedback", "marks")
    if set(by_name) != set(needed) or len(by_name) != len(options):
        return None
    return ReviewVisibility(
        feedback=by_name["feedback"] > 0,
        generalfeedback=by_name["generalfeedback"] > 0,
        rightanswer=by_name["rightanswer"] > 0,
        overallfeedback=by_name["overallfeedback"] > 0,
        marks=by_name["marks"],
    )


@reader
async def read_user_review_options(
    client: MoodleWsClient, site: SiteRef, quizid: int, userid: int
) -> Reading[UserReviewOptions]:
    """Per-student, after-close-correct review options: the policy input of BR-011.

    Never use the quiz-level ``review*`` fields of ``quizzes_by_courses`` for this (they are
    caller-relative and null after close).
    """
    res = await fetch(
        client,
        site,
        "mod_quiz_get_combined_review_options",
        {"quizid": quizid, "userid": userid},
    )
    if isinstance(res, Unreadable):
        return res
    all_opts = _visibility(res.value.alloptions)
    some_opts = _visibility(res.value.someoptions)
    if all_opts is None or some_opts is None:
        return Unreadable(UnreadableReason.UNKNOWN_SHAPE, "review_option_names")
    return Readable(UserReviewOptions(all_opts, some_opts), res.evidence)
