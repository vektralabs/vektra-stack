"""``MoodleLmsProvider``: the Moodle web-service client as an ``LmsProvider`` (ADR-0033 D1, D2).

An adapter over the existing readers (``moodle/readers/*``), probes (``moodle/probes.py``) and
service-account items (``moodle/setup_check.py``). It normalises Moodle's wire vocabulary into
the neutral types of ``vektra_shared.lms``, in one place:

- ``overduehandling`` -> ``OverdueMode``; ``grademethod`` -> ``GradeMethod``;
- the attempt states -> ``AttemptState``;
- the question states (``question_state`` in Moodle core) -> ``QuestionState``, and ``qtype`` ->
  ``QuestionKind``; anything unmapped is ``UNKNOWN`` / ``OTHER`` (fail closed);
- review HTML travels with ``MOODLE_REVIEW_PROFILE``, the CSS classes that mark Moodle's
  question text and response and the ones that carry what Moodle hides (BR-011, D4);
- the re-resolution refusal is ``NOT_IN_USER_COURSES``.

It stays inside ``vektra_assess.moodle`` in this phase; plan 2a-4 moves it to
``adapters/vektra-lms-moodle/``. The vertical reaches it only through the entry point
``vektra.lms_providers: moodle`` and the Protocol.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

import structlog

from vektra_assess.config import VektraAssessSettings
from vektra_assess.moodle import domain as m
from vektra_assess.moodle import readers as r
from vektra_assess.moodle.client import MoodleWsClient, SiteRef
from vektra_assess.moodle.probes import run_probes
from vektra_assess.moodle.readers.resolve import resolve_in_course
from vektra_assess.moodle.setup_check import (
    SERVICE_ACCOUNT_ITEMS,
    CheckContext,
    run_service_account_items,
)
from vektra_shared.lms import (
    NOT_IN_USER_COURSES,
    ActivityKind,
    ActivityRef,
    AssignmentDates,
    AssignmentRelease,
    AssignmentSettings,
    AttemptReview,
    AttemptState,
    AttemptSummary,
    CapabilityResult,
    Enrolment,
    Feedback,
    GracePolicy,
    GradeItem,
    GradeMethod,
    GradingDefinition,
    HtmlExtractionProfile,
    LmsSite,
    LmsSiteInfo,
    MarkingGuideSummary,
    OverdueMode,
    QuestionKind,
    QuestionState,
    QuizOverride,
    QuizSettings,
    Readable,
    Reading,
    ReviewHtml,
    ReviewItem,
    ReviewOptions,
    ReviewVisibility,
    RubricCriterion,
    RubricCriterionFilling,
    RubricDefinition,
    RubricFilling,
    RubricLevel,
    SetupItem,
    StaffMember,
    StaffRole,
    StartupProblem,
    Submission,
    SubmissionFileRef,
    Unreadable,
    UnreadableReason,
)

log = structlog.get_logger(__name__)

NAME = "moodle"

#: the review-HTML extraction profile of Moodle's quiz renderer (ADR-0033 D4). ``keep`` names
#: the block that carries the question text and the student's response; ``drop_classes`` is
#: everything inside them that can carry the right answer, feedback (specific, general, per
#: choice, per cloze gap), a grader comment or a mark. Verified per question type on the
#: recorded 4.3 and 5.2 reviews (``tests/test_review_block_extraction.py``).
MOODLE_REVIEW_PROFILE = HtmlExtractionProfile(
    keep={"question_text": "qtext", "response": "ablock"},
    drop_classes=frozenset(
        {
            "outcome",
            "feedback",
            "specificfeedback",
            "generalfeedback",
            "rightanswer",
            "feedbackspan",
            "feedbacktrigger",
            "im-feedback",
            "comment",
            "history",
            "grade",
            "correctness",
            "validationerror",
            # correctness icons: FA <i class="icon ..."> or pix <img class="icon
            # questioncorrectnessicon" alt="Correct">, whose alt text would otherwise become
            # "Correct" in the response
            "icon",
            "questioncorrectnessicon",
        }
    ),
    profile_version="moodle-1",
)

#: Moodle question types -> Vektra's kinds. Verified on 4.3 and 5.2 by the OQ-025 spike and
#: the recordings (``mod_quiz_get_attempt_review__types-*``); any other type (``ddwtos``,
#: ``ddmarker``, ``calculated``...) is ``OTHER``: mark-and-feedback level, flagged.
QUESTION_KINDS: Mapping[str, QuestionKind] = {
    "multichoice": QuestionKind.MULTIPLE_CHOICE,
    "truefalse": QuestionKind.TRUE_FALSE,
    "shortanswer": QuestionKind.SHORT_ANSWER,
    "numerical": QuestionKind.NUMERICAL,
    "essay": QuestionKind.ESSAY,
    "match": QuestionKind.MATCHING,
    "multianswer": QuestionKind.EMBEDDED_ANSWERS,
    "gapselect": QuestionKind.GAP_SELECT,
    "description": QuestionKind.INFORMATION,
}

#: ``question_state`` values with a FINAL mark. "todo", "invalid", "complete" and
#: "needsgrading" are not final; neither is an unknown state (fail closed).
FINAL_STATES: frozenset[str] = frozenset(
    {
        "gaveup",
        "gradedwrong",
        "gradedpartial",
        "gradedright",
        "mangaveup",
        "mangrwrong",
        "mangrpartial",
        "mangrright",
        "manfinished",
        "finished",
    }
)
#: final states in which Moodle shows no mark (the question was not answered)
NO_MARK_FINAL_STATES: frozenset[str] = frozenset({"gaveup", "mangaveup"})
PENDING_STATES: frozenset[str] = frozenset(
    {"todo", "invalid", "complete", "needsgrading"}
)

_OVERDUE: Mapping[str, OverdueMode] = {
    "autosubmit": OverdueMode.AUTOSUBMIT,
    "graceperiod": OverdueMode.GRACE,
    "autoabandon": OverdueMode.ABANDON,
}
_ATTEMPT: Mapping[m.AttemptState, AttemptState] = {
    m.AttemptState.IN_PROGRESS: AttemptState.IN_PROGRESS,
    m.AttemptState.OVERDUE: AttemptState.OVERDUE,
    m.AttemptState.FINISHED: AttemptState.FINISHED,
    m.AttemptState.ABANDONED: AttemptState.ABANDONED,
}
_GRADE_TYPES = ("point", "scale", "none")
_NOT_IN_STUDENT_COURSES = "activity_not_in_student_courses"


def question_state(state: str | None) -> QuestionState:
    """Moodle's ``question_state`` as Vektra's ``QuestionState``; unknown is ``UNKNOWN``."""
    if state in NO_MARK_FINAL_STATES:
        return QuestionState.FINAL_NO_MARK
    if state in FINAL_STATES:
        return QuestionState.FINAL_MARKED
    if state in PENDING_STATES:
        return QuestionState.PENDING
    return QuestionState.UNKNOWN


def question_kind(qtype: str) -> QuestionKind:
    return QUESTION_KINDS.get(qtype, QuestionKind.OTHER)


class _Shape(ValueError):
    """A reader value that has no neutral form: the reading becomes ``unknown_shape``."""


def _convert[S, T](reading: Reading[S], fn: Callable[[S], T]) -> Reading[T]:
    """Map a Readable through ``fn``; a value that does not map is Unreadable, never a raise."""
    if isinstance(reading, Unreadable):
        return reading
    try:
        return Readable(fn(reading.value), reading.evidence)
    except _Shape as exc:
        return Unreadable(UnreadableReason.UNKNOWN_SHAPE, str(exc))
    except Exception as exc:
        log.warning("moodle_provider_map_error", error=type(exc).__name__)
        return Unreadable(UnreadableReason.UNKNOWN_SHAPE, "reader_error")


def _ref(site: LmsSite) -> SiteRef:
    return SiteRef(site.site_id, site.base_url, site.release)


# --- conversions ---------------------------------------------------------------------------------


def _visibility(v: m.ReviewVisibility) -> ReviewVisibility:
    return ReviewVisibility(
        show_feedback=v.feedback,
        show_general_feedback=v.generalfeedback,
        show_right_answer=v.rightanswer,
        show_overall_feedback=v.overallfeedback,
        marks=v.marks,
    )


def _grace(g: m.GracePolicy) -> GracePolicy:
    mode = _OVERDUE.get(g.overduehandling)
    if mode is None:
        raise _Shape("overduehandling")
    return GracePolicy(g.graceperiod_s, mode)


def _quiz(q: m.QuizCloseInputs) -> QuizSettings:
    try:
        method = GradeMethod(q.grademethod)
    except ValueError:
        raise _Shape("grademethod") from None
    return QuizSettings(
        quiz_id=q.quizid,
        course_id=q.courseid,
        name=q.name,
        time_open=q.timeopen,
        time_close=q.timeclose,
        time_limit_s=q.timelimit_s,
        grade_method=method,
        attempts_allowed=q.attempts_allowed,
        grace=_convert(q.grace, _grace),
        review_after_close=_convert(q.review_after_close, _visibility),
        overrides=_convert(
            q.overrides,
            lambda os: tuple(
                QuizOverride(
                    user_id=o.userid,
                    group_id=o.groupid,
                    time_open=o.timeopen,
                    time_close=o.timeclose,
                    time_limit_s=o.timelimit_s,
                    attempts=o.attempts,
                )
                for o in os
            ),
        ),
    )


def _attempt(a: m.AttemptSummary) -> AttemptSummary:
    return AttemptSummary(
        attempt_id=a.attemptid,
        attempt_number=a.attempt_number,
        user_id=a.userid,
        state=_ATTEMPT[a.state],
        preview=a.preview,
        time_start=a.timestart,
        time_finish=a.timefinish,
        sum_grades=a.sumgrades,
    )


def _review(a: m.AttemptReview) -> AttemptReview:
    return AttemptReview(
        attempt_id=a.attemptid,
        quiz_id=a.quizid,
        user_id=a.userid,
        state=_ATTEMPT[a.state],
        sum_grades=a.sumgrades,
        grade=a.grade,
        items=tuple(
            ReviewItem(
                slot=s.slot,
                display_number=s.display_number,
                question_kind=question_kind(s.qtype),
                state=question_state(s.state),
                status_label=s.status,
                mark=s.mark,
                max_mark=s.maxmark,
                needs_grading=s.needs_grading,
                html=None
                if s.html is None
                else ReviewHtml(s.html, MOODLE_REVIEW_PROFILE),
            )
            for s in a.slots
        ),
    )


def _file(f: m.SubmissionFileRef) -> SubmissionFileRef:
    return SubmissionFileRef(
        filename=f.filename,
        filepath=f.filepath,
        size=f.size,
        mimetype=f.mimetype,
        locator=f.fileurl,
        time_modified=f.timemodified,
    )


def _submission(s: m.SubmissionStatus) -> Submission:
    fb = s.feedback
    return Submission(
        assignment_id=s.assignid,
        user_id=s.userid,
        submission_id=s.submission_id,
        team_submission=s.team_submission,
        state=s.state,
        time_modified=s.timemodified,
        online_text=s.onlinetext,
        files=tuple(_file(f) for f in s.files),
        grading_status=s.grading_status,
        graded=s.graded,
        extension_due_date=s.extensionduedate,
        feedback=None
        if fb is None
        else Feedback(
            grade_id=fb.grade_id,
            grade=fb.grade,
            grade_for_display=fb.grade_for_display,
            graded_date=fb.graded_date,
            comments=fb.comments,
        ),
    )


def _settings(a: m.AssignSettings) -> AssignmentSettings:
    if a.grade_type not in _GRADE_TYPES:
        raise _Shape("grade_type")
    grade_type: Literal["point", "scale", "none"] = a.grade_type  # type: ignore[assignment]
    return AssignmentSettings(
        assignment_id=a.assignid,
        activity_id=a.cmid,
        course_id=a.courseid,
        name=a.name,
        due_date=a.duedate,
        cutoff_date=a.cutoffdate,
        grading_due_date=a.gradingduedate,
        allow_submissions_from=a.allowsubmissionsfromdate,
        team_submission=a.teamsubmission,
        marking_workflow=a.markingworkflow,
        grade_type=grade_type,
        scale_id=a.scale_id,
        grade_max=a.grade_max,
    )


def _dates(a: m.AssignCloseInputs) -> AssignmentDates:
    return AssignmentDates(
        assignment_id=a.assignid,
        user_id=a.userid,
        due_date=a.duedate,
        cutoff_date=a.cutoffdate,
        allow_submissions_from=a.allowsubmissionsfromdate,
        extension_due_date=a.extensionduedate,
        granted_extension=a.granted_extension,
    )


def _grade_item(g: m.GradeReleaseState) -> GradeItem:
    if g.itemmodule not in ("quiz", "assign"):
        raise _Shape("itemmodule")
    kind: ActivityKind = "quiz" if g.itemmodule == "quiz" else "assign"
    return GradeItem(
        activity_id=g.cmid,
        kind=kind,
        hidden=g.hidden,
        hidden_by_date=g.hidden_by_date,
        locked=g.locked,
        grade=g.grade,
        grade_formatted=g.grade_formatted,
        grade_max=g.grade_max,
        range_formatted=g.range_formatted,
        feedback=g.feedback,
    )


def _release(a: m.AssignReleaseState) -> AssignmentRelease:
    return AssignmentRelease(
        assignment_id=a.assignid,
        user_id=a.userid,
        grade_item=_grade_item(a.grade_item),
        grading_status=a.grading_status,
        marking_workflow=a.marking_workflow,
        released=a.released,
    )


def _grading(g: m.GradingView) -> GradingDefinition:
    rubric = g.rubric
    guide = g.guide
    return GradingDefinition(
        activity_id=g.cmid,
        method=g.method,
        rubric=None
        if rubric is None
        else RubricDefinition(
            definition_id=rubric.definitionid,
            name=rubric.name,
            criteria=tuple(
                RubricCriterion(
                    criterion_id=c.criterionid,
                    sort_order=c.sortorder,
                    description=c.description,
                    levels=tuple(
                        RubricLevel(lv.levelid, lv.score, lv.definition)
                        for lv in c.levels
                    ),
                )
                for c in rubric.criteria
            ),
        ),
        guide=None
        if guide is None
        else MarkingGuideSummary(
            guide.definitionid, guide.criteria_count, guide.max_total
        ),
    )


def _fillings(fs: tuple[m.RubricFilling, ...]) -> tuple[RubricFilling, ...]:
    return tuple(
        RubricFilling(
            instance_id=f.instanceid,
            item_id=f.item_id,
            status=f.status,
            criteria=tuple(
                RubricCriterionFilling(c.criterionid, c.levelid, c.remark)
                for c in f.criteria
            ),
        )
        for f in fs
    )


def _staff(g: m.GraderList) -> tuple[StaffMember, ...]:
    return tuple(StaffMember(u.userid, u.fullname, u.email, u.roles) for u in g.graders)


def _activity(a: r.ActivityRef) -> ActivityRef:
    return ActivityRef(a.courseid, a.instanceid, a.course_shortname)


# --- the provider --------------------------------------------------------------------------------


class MoodleLmsProvider:
    """The ``LmsProvider`` over one ``MoodleWsClient``: one per worker, so one rate cap, one
    breaker registry and one connection pool serve every caller (review finding 3)."""

    def __init__(
        self,
        client: MoodleWsClient,
        settings: VektraAssessSettings,
        *,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._clock = clock
        # the full site_info of the last setup-check read, per site: the service-account items
        # need the function list and the account flags, which the neutral value does not carry
        self._site_infos: dict[str, Reading[m.SiteInfo]] = {}

    @property
    def client(self) -> MoodleWsClient:
        return self._client

    @property
    def name(self) -> str:
        return NAME

    @property
    def setup_items(self) -> tuple[str, ...]:
        return SERVICE_ACCOUNT_ITEMS

    # --- lifecycle ----------------------------------------------------------------------------

    def startup_problems(self, env: str) -> tuple[StartupProblem, ...]:
        if self._settings.moodle_allow_http and env != "development":
            return (
                StartupProblem(
                    setting="VEKTRA_ASSESS_MOODLE_ALLOW_HTTP",
                    detail=(
                        "VEKTRA_ASSESS_MOODLE_ALLOW_HTTP=true is only honoured with "
                        "VEKTRA_ENV=development (this process runs with "
                        f"{self._settings.env_described})"
                    ),
                    remediation="Unset VEKTRA_ASSESS_MOODLE_ALLOW_HTTP; use https to Moodle.",
                ),
            )
        return ()

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- site and service account -------------------------------------------------------------

    async def site_info(self, site: LmsSite) -> Reading[LmsSiteInfo]:
        info = await r.read_site_info(self._client, _ref(site))
        self._site_infos[site.site_id] = info
        return _convert(info, lambda i: LmsSiteInfo(release=i.release, branch=i.branch))

    async def _full_info(
        self, site: LmsSite, info: Reading[LmsSiteInfo]
    ) -> Reading[m.SiteInfo]:
        if isinstance(info, Unreadable):
            return info
        full = self._site_infos.get(site.site_id)
        if isinstance(full, Readable) and full.value.release == info.value.release:
            return full
        return await r.read_site_info(self._client, _ref(site))

    async def service_account_check(
        self,
        site: LmsSite,
        info: Reading[LmsSiteInfo],
        manifest: Mapping[str, Any] | None,
    ) -> tuple[SetupItem, ...]:
        full = await self._full_info(site, info)
        ref = _ref(site)
        if isinstance(full, Readable):
            ref = r.with_release(ref, full.value)
        ctx = CheckContext(self._client, ref, self._settings, manifest, full)
        return await run_service_account_items(ctx)

    async def probe_capabilities(
        self, site: LmsSite, manifest: Mapping[str, Any] | None, flags: Sequence[str]
    ) -> tuple[CapabilityResult, ...]:
        now = None if self._clock is None else self._clock()
        return await run_probes(
            self._client, _ref(site), manifest, now=now, flags=tuple(flags)
        )

    # --- resolution ---------------------------------------------------------------------------

    async def resolve_activity(
        self,
        site: LmsSite,
        *,
        user_id: int,
        activity_id: int,
        kind: ActivityKind,
        course_hint: int | None,
    ) -> Reading[ActivityRef]:
        res = await r.resolve_activity(
            self._client,
            _ref(site),
            userid=user_id,
            cmid=activity_id,
            kind=kind,
            courseid_hint=course_hint,
        )
        if isinstance(res, Unreadable) and res.detail_code == _NOT_IN_STUDENT_COURSES:
            return Unreadable(res.reason, NOT_IN_USER_COURSES)
        return _convert(res, _activity)

    async def resolve_in_course(
        self, site: LmsSite, *, activity_id: int, kind: ActivityKind, course_id: int
    ) -> Reading[ActivityRef]:
        res = await resolve_in_course(
            self._client, _ref(site), cmid=activity_id, kind=kind, courseid=course_id
        )
        return _convert(res, _activity)

    # --- people -------------------------------------------------------------------------------

    async def enrolment(
        self, site: LmsSite, course_id: int, user_id: int
    ) -> Reading[Enrolment]:
        res = await r.read_enrolment(self._client, _ref(site), course_id, user_id)
        return _convert(
            res,
            lambda e: Enrolment(e.courseid, e.userid, e.active, e.is_student, e.roles),
        )

    async def course_staff(
        self, site: LmsSite, course_id: int, role: StaffRole
    ) -> Reading[tuple[StaffMember, ...]]:
        reader = {
            StaffRole.GRADER: r.read_graders,
            StaffRole.EDITOR: r.read_editors,
            StaffRole.OVERRIDE_MANAGER: r.read_override_managers,
        }[role]
        return _convert(await reader(self._client, _ref(site), course_id), _staff)

    # --- quiz ---------------------------------------------------------------------------------

    async def quiz_settings(
        self, site: LmsSite, course_id: int, quiz_id: int
    ) -> Reading[QuizSettings]:
        res = await r.read_quiz_close_inputs(
            self._client, _ref(site), course_id, quiz_id
        )
        return _convert(res, _quiz)

    async def review_options(
        self, site: LmsSite, quiz_id: int, user_id: int
    ) -> Reading[ReviewOptions]:
        res = await r.read_user_review_options(
            self._client, _ref(site), quiz_id, user_id
        )
        return _convert(
            res,
            lambda o: ReviewOptions(
                _visibility(o.all_attempts), _visibility(o.some_attempts)
            ),
        )

    async def attempts(
        self, site: LmsSite, quiz_id: int, user_id: int
    ) -> Reading[tuple[AttemptSummary, ...]]:
        res = await r.read_user_attempts(self._client, _ref(site), quiz_id, user_id)
        return _convert(res, lambda xs: tuple(_attempt(a) for a in xs))

    async def attempt_review(
        self, site: LmsSite, attempt_id: int
    ) -> Reading[AttemptReview]:
        res = await r.read_attempt_review(self._client, _ref(site), attempt_id)
        return _convert(res, _review)

    # --- assignment ---------------------------------------------------------------------------

    async def assignment_settings(
        self, site: LmsSite, course_id: int, assignment_id: int
    ) -> Reading[AssignmentSettings]:
        res = await r.read_assign_settings(
            self._client, _ref(site), course_id, assignment_id
        )
        return _convert(res, _settings)

    async def assignment_dates(
        self, site: LmsSite, assignment_id: int, user_id: int
    ) -> Reading[AssignmentDates]:
        res = await r.read_assign_close_inputs(
            self._client, _ref(site), assignment_id, user_id
        )
        return _convert(res, _dates)

    async def submission(
        self, site: LmsSite, assignment_id: int, user_id: int
    ) -> Reading[Submission]:
        res = await r.read_submission_status(
            self._client, _ref(site), assignment_id, user_id
        )
        return _convert(res, _submission)

    async def submission_file(
        self,
        site: LmsSite,
        assignment_id: int,
        ref: SubmissionFileRef,
        owner_user_id: int,
        submission_id: int,
    ) -> Reading[bytes]:
        moodle_ref = m.SubmissionFileRef(
            filename=ref.filename,
            filepath=ref.filepath,
            size=ref.size,
            mimetype=ref.mimetype,
            fileurl=ref.locator,
            timemodified=ref.time_modified,
        )
        return await r.fetch_submission_file(
            self._client,
            _ref(site),
            assignment_id,
            moodle_ref,
            owner_user_id,
            submission_id,
        )

    async def assignment_release(
        self, site: LmsSite, course_id: int, assignment_id: int, user_id: int
    ) -> Reading[AssignmentRelease]:
        res = await r.read_assign_release(
            self._client, _ref(site), course_id, assignment_id, user_id
        )
        return _convert(res, _release)

    # --- grading ------------------------------------------------------------------------------

    async def grade_item(
        self, site: LmsSite, course_id: int, user_id: int, activity_id: int
    ) -> Reading[GradeItem]:
        res = await r.read_grade_release(
            self._client, _ref(site), course_id, user_id, activity_id
        )
        return _convert(res, _grade_item)

    async def grading_definition(
        self, site: LmsSite, activity_id: int
    ) -> Reading[GradingDefinition]:
        res = await r.read_grading_view(self._client, _ref(site), activity_id)
        return _convert(res, _grading)

    async def rubric_fillings(
        self, site: LmsSite, definition_id: int
    ) -> Reading[tuple[RubricFilling, ...]]:
        res = await r.read_rubric_fillings(self._client, _ref(site), definition_id)
        return _convert(res, _fillings)


def build_provider() -> MoodleLmsProvider:
    """The entry point ``vektra.lms_providers: moodle``: the provider reads its own settings
    and its token keyring (``assess_ws_tokens``, re-read at call time). Nothing connects here."""
    from vektra_assess.moodle.tokens import secret_token_provider

    settings = VektraAssessSettings()
    client = MoodleWsClient(
        settings, token_provider=secret_token_provider(settings.secrets_dir)
    )
    return MoodleLmsProvider(client, settings, clock=lambda: int(time.time()))


__all__ = [
    "MOODLE_REVIEW_PROFILE",
    "NAME",
    "MoodleLmsProvider",
    "build_provider",
    "question_kind",
    "question_state",
]
