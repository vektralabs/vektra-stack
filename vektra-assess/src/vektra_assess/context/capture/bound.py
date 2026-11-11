"""An ``LmsProvider`` bound to one site, with the vertical's own fail-closed wrapper.

``SiteReaders`` satisfies ``CaptureReaders`` and carries the other reads the worker's start
source makes. Every call goes through ``_guarded``: a provider that lets an exception escape
gives ``Unreadable(UNKNOWN_SHAPE, PROVIDER_ERROR)`` here, so a faulty provider fails a
pre-flight closed instead of crashing it (ADR-0033 D2, invariant 1). Only the exception's type
is logged: its text may carry what the provider read.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog

from vektra_shared.lms import (
    PROVIDER_ERROR,
    ActivityKind,
    ActivityRef,
    AssignmentDates,
    AssignmentRelease,
    AssignmentSettings,
    AttemptReview,
    AttemptSummary,
    Enrolment,
    GradeItem,
    GradingDefinition,
    LmsProvider,
    LmsSite,
    QuizSettings,
    Reading,
    ReviewOptions,
    RubricFilling,
    StaffMember,
    StaffRole,
    Submission,
    SubmissionFileRef,
    Unreadable,
    UnreadableReason,
)

log = structlog.get_logger(__name__)


async def _guarded[T](
    member: str, call: Callable[[], Awaitable[Reading[T]]]
) -> Reading[T]:
    try:
        return await call()
    except Exception as exc:
        log.warning(
            "assess_lms_provider_error", member=member, error=type(exc).__name__
        )
        return Unreadable(UnreadableReason.UNKNOWN_SHAPE, PROVIDER_ERROR)


class SiteReaders:
    def __init__(self, provider: LmsProvider, site: LmsSite) -> None:
        self._p = provider
        self._s = site

    @property
    def site(self) -> LmsSite:
        return self._s

    # --- resolution and people (the start source) ----------------------------------------

    async def resolve_activity(
        self,
        *,
        user_id: int,
        activity_id: int,
        kind: ActivityKind,
        course_hint: int | None,
    ) -> Reading[ActivityRef]:
        return await _guarded(
            "resolve_activity",
            lambda: self._p.resolve_activity(
                self._s,
                user_id=user_id,
                activity_id=activity_id,
                kind=kind,
                course_hint=course_hint,
            ),
        )

    async def resolve_in_course(
        self, *, activity_id: int, kind: ActivityKind, course_id: int
    ) -> Reading[ActivityRef]:
        return await _guarded(
            "resolve_in_course",
            lambda: self._p.resolve_in_course(
                self._s, activity_id=activity_id, kind=kind, course_id=course_id
            ),
        )

    async def enrolment(self, course_id: int, user_id: int) -> Reading[Enrolment]:
        return await _guarded(
            "enrolment", lambda: self._p.enrolment(self._s, course_id, user_id)
        )

    async def course_staff(
        self, course_id: int, role: StaffRole
    ) -> Reading[tuple[StaffMember, ...]]:
        return await _guarded(
            "course_staff", lambda: self._p.course_staff(self._s, course_id, role)
        )

    async def assignment_dates(
        self, assignment_id: int, user_id: int
    ) -> Reading[AssignmentDates]:
        return await _guarded(
            "assignment_dates",
            lambda: self._p.assignment_dates(self._s, assignment_id, user_id),
        )

    # --- CaptureReaders --------------------------------------------------------------------

    async def quiz_settings(
        self, course_id: int, quiz_id: int
    ) -> Reading[QuizSettings]:
        return await _guarded(
            "quiz_settings", lambda: self._p.quiz_settings(self._s, course_id, quiz_id)
        )

    async def review_options(
        self, quiz_id: int, user_id: int
    ) -> Reading[ReviewOptions]:
        return await _guarded(
            "review_options", lambda: self._p.review_options(self._s, quiz_id, user_id)
        )

    async def attempts(
        self, quiz_id: int, user_id: int
    ) -> Reading[tuple[AttemptSummary, ...]]:
        return await _guarded(
            "attempts", lambda: self._p.attempts(self._s, quiz_id, user_id)
        )

    async def attempt_review(self, attempt_id: int) -> Reading[AttemptReview]:
        return await _guarded(
            "attempt_review", lambda: self._p.attempt_review(self._s, attempt_id)
        )

    async def grade_item(
        self, course_id: int, user_id: int, activity_id: int
    ) -> Reading[GradeItem]:
        return await _guarded(
            "grade_item",
            lambda: self._p.grade_item(self._s, course_id, user_id, activity_id),
        )

    async def assignment_settings(
        self, course_id: int, assignment_id: int
    ) -> Reading[AssignmentSettings]:
        return await _guarded(
            "assignment_settings",
            lambda: self._p.assignment_settings(self._s, course_id, assignment_id),
        )

    async def assignment_release(
        self, course_id: int, assignment_id: int, user_id: int
    ) -> Reading[AssignmentRelease]:
        return await _guarded(
            "assignment_release",
            lambda: self._p.assignment_release(
                self._s, course_id, assignment_id, user_id
            ),
        )

    async def submission(self, assignment_id: int, user_id: int) -> Reading[Submission]:
        return await _guarded(
            "submission", lambda: self._p.submission(self._s, assignment_id, user_id)
        )

    async def grading_definition(self, activity_id: int) -> Reading[GradingDefinition]:
        return await _guarded(
            "grading_definition",
            lambda: self._p.grading_definition(self._s, activity_id),
        )

    async def rubric_fillings(
        self, definition_id: int
    ) -> Reading[tuple[RubricFilling, ...]]:
        return await _guarded(
            "rubric_fillings", lambda: self._p.rubric_fillings(self._s, definition_id)
        )

    async def submission_file(
        self,
        assignment_id: int,
        ref: SubmissionFileRef,
        owner_user_id: int,
        submission_id: int,
    ) -> Reading[bytes]:
        return await _guarded(
            "submission_file",
            lambda: self._p.submission_file(
                self._s, assignment_id, ref, owner_user_id, submission_id
            ),
        )
