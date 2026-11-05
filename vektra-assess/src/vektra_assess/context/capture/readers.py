"""``CaptureReaders`` over the plan 4 Moodle readers, bound to one client and site."""

from __future__ import annotations

from vektra_assess.moodle import readers as r
from vektra_assess.moodle.client import MoodleWsClient, SiteRef
from vektra_assess.moodle.domain import (
    AssignReleaseState,
    AssignSettings,
    AttemptReview,
    AttemptSummary,
    GradeReleaseState,
    GradingView,
    QuizCloseInputs,
    RubricFilling,
    SubmissionFileRef,
    SubmissionStatus,
    UserReviewOptions,
)
from vektra_assess.moodle.result import Reading


class MoodleCaptureReaders:
    def __init__(self, client: MoodleWsClient, site: SiteRef) -> None:
        self._c = client
        self._s = site

    async def quiz_close_inputs(
        self, courseid: int, quizid: int
    ) -> Reading[QuizCloseInputs]:
        return await r.read_quiz_close_inputs(self._c, self._s, courseid, quizid)

    async def user_review_options(
        self, quizid: int, userid: int
    ) -> Reading[UserReviewOptions]:
        return await r.read_user_review_options(self._c, self._s, quizid, userid)

    async def user_attempts(
        self, quizid: int, userid: int
    ) -> Reading[tuple[AttemptSummary, ...]]:
        return await r.read_user_attempts(self._c, self._s, quizid, userid)

    async def attempt_review(self, attemptid: int) -> Reading[AttemptReview]:
        return await r.read_attempt_review(self._c, self._s, attemptid)

    async def grade_release(
        self, courseid: int, userid: int, cmid: int
    ) -> Reading[GradeReleaseState]:
        return await r.read_grade_release(self._c, self._s, courseid, userid, cmid)

    async def assign_settings(
        self, courseid: int, assignid: int
    ) -> Reading[AssignSettings]:
        return await r.read_assign_settings(self._c, self._s, courseid, assignid)

    async def assign_release(
        self, courseid: int, assignid: int, userid: int
    ) -> Reading[AssignReleaseState]:
        return await r.read_assign_release(self._c, self._s, courseid, assignid, userid)

    async def submission_status(
        self, assignid: int, userid: int
    ) -> Reading[SubmissionStatus]:
        return await r.read_submission_status(self._c, self._s, assignid, userid)

    async def grading_view(self, cmid: int) -> Reading[GradingView]:
        return await r.read_grading_view(self._c, self._s, cmid)

    async def rubric_fillings(
        self, definitionid: int
    ) -> Reading[tuple[RubricFilling, ...]]:
        return await r.read_rubric_fillings(self._c, self._s, definitionid)

    async def submission_file(
        self,
        assignid: int,
        ref: SubmissionFileRef,
        owner_userid: int,
        submission_id: int,
    ) -> Reading[bytes]:
        return await r.fetch_submission_file(
            self._c, self._s, assignid, ref, owner_userid, submission_id
        )
