"""``FakeLmsProvider``: an in-memory ``LmsProvider`` for the vertical's tests (plan 2a-1.8).

Neutral values only, no recorded wire document and no product vocabulary: the vertical's suites
move onto it in 2a-2, so they stop depending on one LMS's recordings.

- **World**: ``FakeLmsWorld`` holds the values the fake returns, keyed by the ids the Protocol
  takes. A key that is not there is ``Unreadable(ACCESS_EXCEPTION, "not_found")``.
- **Scripting**, per member or for all (``"*"``):
  ``fail(member, Unreadable(...))`` answers that Reading; ``transport_error(member, exc)``
  simulates a transport that raised, which the fake turns into
  ``Unreadable(UNKNOWN_SHAPE, PROVIDER_ERROR)`` as a conforming provider must;
  ``escape(member, exc)`` lets the exception out, for the vertical's own wrapper (ADR-0033 D2,
  invariant 1). ``clear()`` drops every script.
- **Recorder**: every call is appended to ``calls`` before any script applies, so a "never read"
  assertion holds even for a member scripted to fail (``assert_never_read``).
- It is a test double: ``ProviderRegistry`` and ``load_lms_provider`` refuse it unless the caller
  opted in.
- ``hidden`` holds the LMS-side content the fake must never return (the conformance kit plants
  its canaries there). ``secret`` is the fake transport's credential; it is never logged.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

from vektra_shared.lms import (
    NOT_IN_USER_COURSES,
    PROVIDER_ERROR,
    SETUP_OK,
    SETUP_UNVERIFIABLE,
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
    OverdueMode,
    QuestionKind,
    QuestionState,
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

log = structlog.get_logger("vektra_lms_fake")

ALL = "*"
NOT_FOUND = Unreadable(UnreadableReason.ACCESS_EXCEPTION, "not_found")


@dataclass(frozen=True)
class FakeCall:
    member: str
    args: Mapping[str, Any]


@dataclass
class FakeLmsWorld:
    site_info: LmsSiteInfo
    #: user id -> the course ids the user is enrolled in (resolution reads only this)
    courses_of: dict[int, set[int]] = field(default_factory=dict)
    #: (kind, activity id) -> the activity
    activities: dict[tuple[ActivityKind, int], ActivityRef] = field(
        default_factory=dict
    )
    enrolments: dict[tuple[int, int], Enrolment] = field(default_factory=dict)
    staff: dict[tuple[int, StaffRole], tuple[StaffMember, ...]] = field(
        default_factory=dict
    )
    quizzes: dict[tuple[int, int], QuizSettings] = field(default_factory=dict)
    review_options: dict[tuple[int, int], ReviewOptions] = field(default_factory=dict)
    attempts: dict[tuple[int, int], tuple[AttemptSummary, ...]] = field(
        default_factory=dict
    )
    reviews: dict[int, AttemptReview] = field(default_factory=dict)
    assignments: dict[tuple[int, int], AssignmentSettings] = field(default_factory=dict)
    dates: dict[tuple[int, int], AssignmentDates] = field(default_factory=dict)
    submissions: dict[tuple[int, int], Submission] = field(default_factory=dict)
    #: locator -> file bytes
    files: dict[str, bytes] = field(default_factory=dict)
    releases: dict[tuple[int, int, int], AssignmentRelease] = field(
        default_factory=dict
    )
    grade_items: dict[tuple[int, int, int], GradeItem] = field(default_factory=dict)
    gradings: dict[int, GradingDefinition] = field(default_factory=dict)
    fillings: dict[int, tuple[RubricFilling, ...]] = field(default_factory=dict)
    #: LMS-side content that exists but that no returned value may carry
    hidden: dict[str, str] = field(default_factory=dict)


class FakeLmsProvider:
    __vektra_test_double__ = True

    def __init__(
        self,
        world: FakeLmsWorld | None = None,
        *,
        name: str = "fake",
        setup_items: Sequence[str] = ("token_works", "required_reads"),
        secret: str = "fake-transport-credential",
        problems: Sequence[StartupProblem] = (),
    ) -> None:
        self.world = world if world is not None else default_world()
        self._name = name
        self._setup_items = tuple(setup_items)
        self.secret = secret
        self._problems = tuple(problems)
        self.calls: list[FakeCall] = []
        self.closed = False
        self._fail: dict[str, Unreadable] = {}
        self._transport: dict[str, BaseException] = {}
        self._escape: dict[str, BaseException] = {}

    # --- scripting and recording -----------------------------------------------------------

    def fail(self, member: str, reading: Unreadable) -> FakeLmsProvider:
        self._fail[member] = reading
        return self

    def transport_error(self, member: str, exc: BaseException) -> FakeLmsProvider:
        self._transport[member] = exc
        return self

    def escape(self, member: str, exc: BaseException) -> FakeLmsProvider:
        self._escape[member] = exc
        return self

    def clear(self) -> None:
        self._fail.clear()
        self._transport.clear()
        self._escape.clear()

    def count(self, member: str) -> int:
        return sum(1 for c in self.calls if c.member == member)

    def assert_never_read(self, *members: str) -> None:
        read = [m for m in members if self.count(m)]
        assert not read, f"read although it must not be: {read}"

    def _scripted(self, member: str) -> Unreadable | None:
        """The scripted outcome of ``member``, or None. Raises an escaping exception."""
        for key in (member, ALL):
            if key in self._escape:
                raise self._escape[key]
        for key in (member, ALL):
            if key in self._transport:
                # what a conforming provider does with a transport exception: never re-raise
                log.info(
                    "lms_fake_transport_error",
                    reader=member,
                    error_type=type(self._transport[key]).__name__,
                )
                return Unreadable(UnreadableReason.UNKNOWN_SHAPE, PROVIDER_ERROR)
        for key in (member, ALL):
            if key in self._fail:
                return self._fail[key]
        return None

    def _read[T](
        self, member: str, args: Mapping[str, Any], value: T | None
    ) -> Reading[T]:
        self.calls.append(FakeCall(member, dict(args)))
        scripted = self._scripted(member)
        result: Reading[T] = (
            scripted
            if scripted is not None
            else (NOT_FOUND if value is None else Readable(value))
        )
        log.debug(
            "lms_fake_call",
            reader=member,
            outcome="ok" if isinstance(result, Readable) else result.reason.value,
        )
        return result

    # --- the Protocol ----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def setup_items(self) -> tuple[str, ...]:
        return self._setup_items

    def startup_problems(self, env: str) -> tuple[StartupProblem, ...]:
        self.calls.append(FakeCall("startup_problems", {"env": env}))
        return self._problems

    async def aclose(self) -> None:
        self.calls.append(FakeCall("aclose", {}))
        self.closed = True

    async def site_info(self, site: LmsSite) -> Reading[LmsSiteInfo]:
        return self._read("site_info", {"site": site.site_id}, self.world.site_info)

    async def service_account_check(
        self,
        site: LmsSite,
        info: Reading[LmsSiteInfo],
        manifest: Mapping[str, Any] | None,
    ) -> tuple[SetupItem, ...]:
        self.calls.append(FakeCall("service_account_check", {"site": site.site_id}))
        failed = self._scripted("service_account_check")
        if failed is None and isinstance(info, Unreadable):
            failed = info
        if failed is not None:  # a fault never proves an item: fail closed
            return tuple(
                SetupItem(n, SETUP_UNVERIFIABLE, failed.reason.value)
                for n in self._setup_items
            )
        return tuple(SetupItem(n, SETUP_OK) for n in self._setup_items)

    async def probe_capabilities(
        self, site: LmsSite, manifest: Mapping[str, Any] | None, flags: Sequence[str]
    ) -> tuple[CapabilityResult, ...]:
        self.calls.append(FakeCall("probe_capabilities", {"flags": tuple(flags)}))
        scripted = self._scripted("probe_capabilities")
        if scripted is not None:
            return tuple(
                CapabilityResult(f, "unreadable", scripted.reason.value, "0" * 64)
                for f in flags
            )
        return tuple(CapabilityResult(f, "readable", None, "0" * 64) for f in flags)

    async def resolve_activity(
        self,
        site: LmsSite,
        *,
        user_id: int,
        activity_id: int,
        kind: ActivityKind,
        course_hint: int | None,
    ) -> Reading[ActivityRef]:
        args = {"user_id": user_id, "activity_id": activity_id, "kind": kind}
        ref = self.world.activities.get((kind, activity_id))
        res = self._read("resolve_activity", {**args, "course_hint": course_hint}, ref)
        # the hint is never trusted: only the user's own course list decides (REQ-095)
        if isinstance(
            res, Readable
        ) and res.value.course_id not in self.world.courses_of.get(user_id, set()):
            return Unreadable(UnreadableReason.ACCESS_EXCEPTION, NOT_IN_USER_COURSES)
        return res

    async def resolve_in_course(
        self, site: LmsSite, *, activity_id: int, kind: ActivityKind, course_id: int
    ) -> Reading[ActivityRef]:
        ref = self.world.activities.get((kind, activity_id))
        if ref is not None and ref.course_id != course_id:
            ref = None
        return self._read(
            "resolve_in_course",
            {"activity_id": activity_id, "kind": kind, "course_id": course_id},
            ref,
        )

    async def enrolment(
        self, site: LmsSite, course_id: int, user_id: int
    ) -> Reading[Enrolment]:
        return self._read(
            "enrolment",
            {"course_id": course_id, "user_id": user_id},
            self.world.enrolments.get((course_id, user_id)),
        )

    async def course_staff(
        self, site: LmsSite, course_id: int, role: StaffRole
    ) -> Reading[tuple[StaffMember, ...]]:
        return self._read(
            "course_staff",
            {"course_id": course_id, "role": role},
            self.world.staff.get((course_id, role)),
        )

    async def quiz_settings(
        self, site: LmsSite, course_id: int, quiz_id: int
    ) -> Reading[QuizSettings]:
        return self._read(
            "quiz_settings",
            {"course_id": course_id, "quiz_id": quiz_id},
            self.world.quizzes.get((course_id, quiz_id)),
        )

    async def review_options(
        self, site: LmsSite, quiz_id: int, user_id: int
    ) -> Reading[ReviewOptions]:
        return self._read(
            "review_options",
            {"quiz_id": quiz_id, "user_id": user_id},
            self.world.review_options.get((quiz_id, user_id)),
        )

    async def attempts(
        self, site: LmsSite, quiz_id: int, user_id: int
    ) -> Reading[tuple[AttemptSummary, ...]]:
        return self._read(
            "attempts",
            {"quiz_id": quiz_id, "user_id": user_id},
            self.world.attempts.get((quiz_id, user_id)),
        )

    async def attempt_review(
        self, site: LmsSite, attempt_id: int
    ) -> Reading[AttemptReview]:
        return self._read(
            "attempt_review",
            {"attempt_id": attempt_id},
            self.world.reviews.get(attempt_id),
        )

    async def assignment_settings(
        self, site: LmsSite, course_id: int, assignment_id: int
    ) -> Reading[AssignmentSettings]:
        return self._read(
            "assignment_settings",
            {"course_id": course_id, "assignment_id": assignment_id},
            self.world.assignments.get((course_id, assignment_id)),
        )

    async def assignment_dates(
        self, site: LmsSite, assignment_id: int, user_id: int
    ) -> Reading[AssignmentDates]:
        return self._read(
            "assignment_dates",
            {"assignment_id": assignment_id, "user_id": user_id},
            self.world.dates.get((assignment_id, user_id)),
        )

    async def submission(
        self, site: LmsSite, assignment_id: int, user_id: int
    ) -> Reading[Submission]:
        return self._read(
            "submission",
            {"assignment_id": assignment_id, "user_id": user_id},
            self.world.submissions.get((assignment_id, user_id)),
        )

    async def submission_file(
        self,
        site: LmsSite,
        assignment_id: int,
        ref: SubmissionFileRef,
        owner_user_id: int,
        submission_id: int,
    ) -> Reading[bytes]:
        sub = self.world.submissions.get((assignment_id, owner_user_id))
        owned = (
            sub is not None
            and sub.submission_id == submission_id
            and any(f.locator == ref.locator for f in sub.files)
        )
        return self._read(
            "submission_file",
            {
                "assignment_id": assignment_id,
                "owner_user_id": owner_user_id,
                "submission_id": submission_id,
            },
            self.world.files.get(ref.locator) if owned else None,
        )

    async def assignment_release(
        self, site: LmsSite, course_id: int, assignment_id: int, user_id: int
    ) -> Reading[AssignmentRelease]:
        return self._read(
            "assignment_release",
            {
                "course_id": course_id,
                "assignment_id": assignment_id,
                "user_id": user_id,
            },
            self.world.releases.get((course_id, assignment_id, user_id)),
        )

    async def grade_item(
        self, site: LmsSite, course_id: int, user_id: int, activity_id: int
    ) -> Reading[GradeItem]:
        return self._read(
            "grade_item",
            {"course_id": course_id, "user_id": user_id, "activity_id": activity_id},
            self.world.grade_items.get((course_id, user_id, activity_id)),
        )

    async def grading_definition(
        self, site: LmsSite, activity_id: int
    ) -> Reading[GradingDefinition]:
        return self._read(
            "grading_definition",
            {"activity_id": activity_id},
            self.world.gradings.get(activity_id),
        )

    async def rubric_fillings(
        self, site: LmsSite, definition_id: int
    ) -> Reading[tuple[RubricFilling, ...]]:
        return self._read(
            "rubric_fillings",
            {"definition_id": definition_id},
            self.world.fillings.get(definition_id),
        )


# --- a consistent default world ------------------------------------------------------------

SITE = LmsSite("site-1", "https://lms.example.org", "1.0")
USER, COURSE, OTHER_COURSE = 7, 3, 4
QUIZ_ACTIVITY, QUIZ = 11, 21
ASSIGN_ACTIVITY, ASSIGNMENT = 12, 22
FOREIGN_ACTIVITY, FOREIGN_QUIZ = 13, 23
ATTEMPT, DEFINITION, SUBMISSION, GRADE_ID = 31, 41, 51, 61
LOCATOR = "fake://file/1"
PROFILE = HtmlExtractionProfile(
    keep={"question_text": "fake-qtext", "response": "fake-response"},
    drop_classes=frozenset({"fake-rightanswer", "fake-feedback"}),
    profile_version="fake-1",
)


def default_world(canaries: Mapping[str, str] | None = None) -> FakeLmsWorld:
    """One student in one course, with one quiz (one finished, graded attempt) and one
    assignment (one submitted file, rubric-graded, released). ``canaries`` maps a hidden field
    name to the text planted in it; the review HTML carries the right-answer canary, as the
    teacher-visible rendering of a real LMS would."""
    hidden = dict(
        canaries
        or {
            "right_answer": "CNRY-fake-right_answer-0001",
            "hidden_feedback": "CNRY-fake-hidden_feedback-0002",
            "unreleased_grade": "CNRY-fake-unreleased_grade-0003",
        }
    )
    visible = ReviewVisibility(True, True, False, True, 2)
    item = ReviewItem(
        slot=1,
        display_number="1",
        question_kind=QuestionKind.MULTIPLE_CHOICE,
        state=QuestionState.FINAL_MARKED,
        status_label="Correct",
        mark=1.0,
        max_mark=1.0,
        needs_grading=False,
        html=ReviewHtml(
            '<div class="fake-qtext">2 + 2?</div>'
            f'<div class="fake-rightanswer">{hidden.get("right_answer", "")}</div>',
            PROFILE,
        ),
    )
    file_ref = SubmissionFileRef(
        "essay.txt", "/", 5, "text/plain", LOCATOR, 1_700_000_000
    )
    grade_item = GradeItem(
        activity_id=ASSIGN_ACTIVITY,
        kind="assign",
        hidden=False,
        hidden_by_date=False,
        locked=False,
        grade=8.0,
        grade_formatted="8.00",
        grade_max=10.0,
        range_formatted="0-10",
        feedback="Good structure.",
    )
    rubric = RubricDefinition(
        DEFINITION,
        "Essay rubric",
        (
            RubricCriterion(
                1,
                1,
                "Argument",
                (RubricLevel(1, 0.0, "weak"), RubricLevel(2, 5.0, "strong")),
            ),
        ),
    )
    return FakeLmsWorld(
        site_info=LmsSiteInfo(release="1.0", branch="1"),
        courses_of={USER: {COURSE}},
        activities={
            ("quiz", QUIZ_ACTIVITY): ActivityRef(COURSE, QUIZ, "C3"),
            ("assign", ASSIGN_ACTIVITY): ActivityRef(COURSE, ASSIGNMENT, "C3"),
            ("quiz", FOREIGN_ACTIVITY): ActivityRef(OTHER_COURSE, FOREIGN_QUIZ, "C4"),
        },
        enrolments={(COURSE, USER): Enrolment(COURSE, USER, True, True, ("student",))},
        staff={
            (COURSE, StaffRole.GRADER): (
                StaffMember(70, "Teacher One", None, ("teacher",)),
            ),
            (COURSE, StaffRole.EDITOR): (
                StaffMember(70, "Teacher One", None, ("teacher",)),
            ),
            (COURSE, StaffRole.OVERRIDE_MANAGER): (),
        },
        quizzes={
            (COURSE, QUIZ): QuizSettings(
                quiz_id=QUIZ,
                course_id=COURSE,
                name="Quiz one",
                time_open=1_700_000_000,
                time_close=1_700_003_600,
                time_limit_s=1800,
                grade_method=GradeMethod.HIGHEST,
                attempts_allowed=1,
                grace=Readable(GracePolicy(0, OverdueMode.AUTOSUBMIT)),
                review_after_close=Readable(visible),
                overrides=Readable(()),
            )
        },
        review_options={(QUIZ, USER): ReviewOptions(visible, visible)},
        attempts={
            (QUIZ, USER): (
                AttemptSummary(
                    ATTEMPT,
                    1,
                    USER,
                    AttemptState.FINISHED,
                    False,
                    1_700_000_100,
                    1_700_000_900,
                    1.0,
                ),
            )
        },
        reviews={
            ATTEMPT: AttemptReview(
                ATTEMPT, QUIZ, USER, AttemptState.FINISHED, 1.0, 10.0, (item,)
            )
        },
        assignments={
            (COURSE, ASSIGNMENT): AssignmentSettings(
                assignment_id=ASSIGNMENT,
                activity_id=ASSIGN_ACTIVITY,
                course_id=COURSE,
                name="Essay",
                due_date=1_700_100_000,
                cutoff_date=0,
                grading_due_date=0,
                allow_submissions_from=1_700_000_000,
                team_submission=False,
                marking_workflow=False,
                grade_type="point",
                scale_id=None,
                grade_max=10.0,
            )
        },
        dates={
            (ASSIGNMENT, USER): AssignmentDates(
                ASSIGNMENT, USER, 1_700_100_000, 0, 1_700_000_000, None, False
            )
        },
        submissions={
            (ASSIGNMENT, USER): Submission(
                assignment_id=ASSIGNMENT,
                user_id=USER,
                submission_id=SUBMISSION,
                team_submission=False,
                state="submitted",
                time_modified=1_700_050_000,
                online_text=None,
                files=(file_ref,),
                grading_status="graded",
                graded=True,
                extension_due_date=None,
                feedback=Feedback(
                    GRADE_ID, 8.0, "8.00", 1_700_200_000, ("Good structure.",)
                ),
            )
        },
        files={LOCATOR: b"essay"},
        releases={
            (COURSE, ASSIGNMENT, USER): AssignmentRelease(
                ASSIGNMENT, USER, grade_item, "graded", False, True
            )
        },
        grade_items={
            (COURSE, USER, ASSIGN_ACTIVITY): grade_item,
            (COURSE, USER, QUIZ_ACTIVITY): dataclasses.replace(
                grade_item, activity_id=QUIZ_ACTIVITY, kind="quiz", feedback=None
            ),
        },
        gradings={
            ASSIGN_ACTIVITY: GradingDefinition(ASSIGN_ACTIVITY, "rubric", rubric, None)
        },
        fillings={
            DEFINITION: (
                RubricFilling(
                    81, GRADE_ID, 0, (RubricCriterionFilling(1, 2, "clear"),)
                ),
            )
        },
        hidden=hidden,
    )


__all__ = ["ALL", "FakeCall", "FakeLmsProvider", "FakeLmsWorld", "default_world"]
