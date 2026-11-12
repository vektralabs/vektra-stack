"""Domain values returned by the Moodle readers (COMP-004, anti-corruption layer).

Readers return these, never a raw Moodle shape. They are frozen dataclasses of primitives and
tuples. Moodle ids (user, course, quiz, assignment...) appear ONLY inside these values, never
in anything a client sends (REQ-095, NFR-014).

Where Moodle answers differently depending on who asks (``graceperiod``, quiz-level review
options) the field is itself a ``Reading``: ``null`` / absent is *not* "zero" or "none", it is
``Unreadable`` (spike trap 2).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from vektra_lms_moodle.result import Reading

# --- site ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SiteInfo:
    release: str
    version: str
    branch: str  # "4.3" | "5.2"
    userid: int
    username: str
    user_is_site_admin: bool
    functions: tuple[str, ...]
    downloadfiles: bool  # the service may download files (pluginfile)


# --- quiz ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class GracePolicy:
    graceperiod_s: int
    overduehandling: str  # autosubmit | graceperiod | autoabandon


@dataclass(frozen=True)
class ReviewVisibility:
    """What the student may see (BR-011). ``feedback`` is the specific (per-question) feedback.

    ``marks``: 0 hidden, 1 maximum only, 2 mark and maximum. The WS does not expose the
    ``attempt`` and ``correctness`` options: they are deliberately absent, not guessed.
    """

    feedback: bool
    generalfeedback: bool
    rightanswer: bool
    overallfeedback: bool
    marks: int


@dataclass(frozen=True)
class UserReviewOptions:
    """``mod_quiz_get_combined_review_options`` for one student.

    ``all_attempts`` is visible on every attempt, ``some_attempts`` on at least one. A
    consumer that must not leak uses ``all_attempts``.
    """

    all_attempts: ReviewVisibility
    some_attempts: ReviewVisibility


@dataclass(frozen=True)
class QuizOverride:
    userid: int | None  # exactly one of userid / groupid is set
    groupid: int | None
    timeopen: int | None
    timeclose: int | None
    timelimit_s: int | None
    attempts: int | None


@dataclass(frozen=True)
class QuizCloseInputs:
    quizid: int
    courseid: int
    name: str
    timeopen: int
    timeclose: int  # 0 = no close
    timelimit_s: int  # 0 = no limit
    grademethod: str  # highest | average | first | last
    attempts_allowed: int  # 0 = unlimited
    #: caller-relative: readable only while the quiz is open for the service account
    grace: Reading[GracePolicy]
    review_after_close: Reading[ReviewVisibility]
    #: user and group overrides of ALL participants; absent on Moodle 4.3
    overrides: Reading[tuple[QuizOverride, ...]]


class AttemptState(StrEnum):
    IN_PROGRESS = "inprogress"
    OVERDUE = "overdue"
    FINISHED = "finished"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class AttemptSummary:
    attemptid: int
    attempt_number: int
    userid: int
    state: AttemptState
    preview: bool
    timestart: int
    timefinish: int  # 0 = not finished
    sumgrades: float | None  # None = not (fully) graded


@dataclass(frozen=True)
class ReviewSlot:
    slot: int
    display_number: str  # "3", or "i" for an information item
    qtype: str
    state: (
        str | None
    )  # question state; None when Moodle does not expose one (zero-mark items)
    status: str | None  # human label ("Correct"...)
    mark: float | None  # None = no mark yet
    maxmark: float
    needs_grading: bool
    #: Rendered HTML. The service account always sees it with teacher visibility, so it
    #: contains material the student may not see (spike trap 4): the capture step DROPS it
    #: with ``AttemptReview.without_html()``; nothing derived from it may reach a model.
    html: str | None


@dataclass(frozen=True)
class AttemptReview:
    attemptid: int
    quizid: int
    userid: int
    state: AttemptState
    sumgrades: float | None
    grade: float | None
    slots: tuple[ReviewSlot, ...]

    def without_html(self) -> AttemptReview:
        return replace(self, slots=tuple(replace(s, html=None) for s in self.slots))


# --- assignment ----------------------------------------------------------------------------


@dataclass(frozen=True)
class AssignSettings:
    assignid: int
    cmid: int
    courseid: int
    name: str
    duedate: int  # 0 = none
    cutoffdate: int  # 0 = none
    gradingduedate: int
    allowsubmissionsfromdate: int
    teamsubmission: bool
    markingworkflow: bool
    grade_type: str  # point | scale | none
    scale_id: int | None
    grade_max: float | None  # None for a scale


@dataclass(frozen=True)
class UserFlag:
    userid: int
    extensionduedate: int | None
    locked: bool
    workflowstate: str | None


@dataclass(frozen=True)
class AssignCloseInputs:
    """Effective dates for ONE student: the activity values with the student's override
    (``mod_assign_get_participant``) and extension (``get_submission_status``) applied."""

    assignid: int
    userid: int
    duedate: int
    cutoffdate: int
    allowsubmissionsfromdate: int
    extensionduedate: int | None
    granted_extension: bool


@dataclass(frozen=True)
class SubmissionFileRef:
    """A file of a submission. Moodle does not return a content hash: it is computed after the
    download (``FetchedFile.sha256``)."""

    filename: str
    filepath: str
    size: int
    mimetype: str
    fileurl: str  # carries no token; NEVER log it (COMP-004)
    timemodified: int


@dataclass(frozen=True)
class FeedbackView:
    grade_id: int | None  # joins rubric fillings (``RubricFilling.item_id``)
    grade: float | None
    grade_for_display: str
    graded_date: int | None
    comments: tuple[str, ...]


@dataclass(frozen=True)
class SubmissionStatus:
    assignid: int
    userid: int
    submission_id: int | None  # the TEAM's submission id when ``team_submission``
    team_submission: bool
    state: str | None  # new | draft | submitted | reopened
    timemodified: int | None
    onlinetext: str | None
    files: tuple[SubmissionFileRef, ...]
    grading_status: (
        str  # notgraded | graded | inmarking | readyforreview | released ...
    )
    graded: bool
    extensionduedate: int | None
    #: None when Moodle withholds it (hidden grade item, unreleased)
    feedback: FeedbackView | None


@dataclass(frozen=True)
class GradeReleaseState:
    """Release state of ONE grade item for ONE student, from the grade report (trap 3).

    ``hidden`` covers a hidden item and a hidden-until date. The value fields are None
    whenever the item is hidden: the reader itself never surfaces an unreleased grade.
    """

    cmid: int
    itemmodule: str
    hidden: bool
    hidden_by_date: bool
    locked: bool
    grade: float | None
    grade_formatted: str | None
    grade_max: float
    range_formatted: str
    feedback: str | None


@dataclass(frozen=True)
class AssignReleaseState:
    """BR-010 for an assignment: grade item visible AND, with marking workflow, workflow state
    ``released``. ``mod_assign_get_grades`` is NEVER consulted (it returns unreleased grades)."""

    assignid: int
    userid: int
    grade_item: GradeReleaseState
    grading_status: str
    marking_workflow: bool
    released: bool


# --- grading -------------------------------------------------------------------------------


@dataclass(frozen=True)
class RubricLevel:
    levelid: int
    score: float
    definition: str


@dataclass(frozen=True)
class RubricCriterion:
    criterionid: int
    sortorder: int
    description: str
    levels: tuple[RubricLevel, ...]


@dataclass(frozen=True)
class RubricDefinitionView:
    """Criteria and level texts. Whether a student may see them is NOT readable
    (``read_rubric_display_options`` is always Unreadable), so BR-011 excludes them."""

    definitionid: int
    name: str
    criteria: tuple[RubricCriterion, ...]


@dataclass(frozen=True)
class MarkingGuideSummary:
    """Detection only. The guide's marker notes (``descriptionmarkers``) are teacher-only and
    are deliberately never read into a domain value."""

    definitionid: int
    criteria_count: int
    max_total: float


@dataclass(frozen=True)
class GradingView:
    cmid: int
    method: str  # none | rubric | guide | <other plugin name>
    rubric: RubricDefinitionView | None
    guide: MarkingGuideSummary | None


@dataclass(frozen=True)
class RubricCriterionFilling:
    criterionid: int
    levelid: int
    remark: str


@dataclass(frozen=True)
class RubricFilling:
    instanceid: int
    item_id: int  # the grade id (``FeedbackView.grade_id``)
    status: int
    criteria: tuple[RubricCriterionFilling, ...]


@dataclass(frozen=True)
class ScaleDefinition:
    """What the WS exposes of a scale: its size and the labels of its two ends (from the grade
    report range). The full label list is NOT exposed, hence ``labels`` is Unreadable."""

    scale_id: int
    size: int
    first_label: str
    last_label: str
    labels: Reading[tuple[str, ...]]


# --- enrolment, graders --------------------------------------------------------------------


@dataclass(frozen=True)
class CourseEnrolment:
    courseid: int
    shortname: str
    hidden: bool


@dataclass(frozen=True)
class CourseParticipant:
    userid: int
    fullname: str
    roles: tuple[str, ...]  # role shortnames


@dataclass(frozen=True)
class EnrolmentView:
    """Active enrolment of one user in one course (the re-check of NFR-014)."""

    courseid: int
    userid: int
    active: bool
    is_student: bool
    roles: tuple[str, ...]


@dataclass(frozen=True)
class GraderView:
    userid: int
    fullname: str
    #: the service account has no ``moodle/user:viewalldetails``: Moodle returns no email.
    email: str | None
    roles: tuple[str, ...]


@dataclass(frozen=True)
class GraderList:
    courseid: int
    graders: tuple[GraderView, ...]
