"""Neutral LMS values and the typed read outcome (ADR-0033 D2).

Everything an ``LmsProvider`` hands to a vertical is one of these frozen dataclasses of
primitives, tuples and enums. They use the vocabulary of the LMS domain (course, activity,
attempt, rubric) and name no product: a provider maps its own wire shapes onto them, and the
vertical never sees a provider's raw document.

Two rules hold for every type here.

- **The Reading discipline.** Every read returns ``Reading[T] = Readable[T] | Unreadable``. No
  ``None`` and no exception escapes a provider. Consumers pattern-match; with ``assert_never``
  in the default branch mypy enforces exhaustiveness, so a new ``UnreadableReason`` cannot be
  silently ignored. Every unknown is ``Unreadable``, which fails closed.
- **Type-level minimisation (ADR-0032 layer 1).** No type has a field for a solution, a right
  answer, hidden feedback or an unreleased grade value: a provider cannot hand the vertical what
  the vertical has no slot for. The one free-text channel is ``ReviewHtml``, which carries the
  extraction profile that removes what the LMS hides (ADR-0033 D4).

Ids are the LMS's own integers. Where an LMS answers differently depending on who asks, the
field is itself a ``Reading``: absent is not "zero" or "none", it is ``Unreadable``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Literal

# --- the typed read outcome ----------------------------------------------------------------


class UnreadableReason(StrEnum):
    FUNCTION_NOT_IN_SERVICE = "function_not_in_service"
    ACCESS_EXCEPTION = "access_exception"
    UNKNOWN_SHAPE = "unknown_shape"
    PARTIAL = "partial"
    BREAKER_OPEN = "breaker_open"
    TIMEOUT = "timeout"
    SIZE_CAP = "size_cap"
    TLS = "tls"
    REDIRECT = "redirect"
    VERSION_UNSUPPORTED = "version_unsupported"
    NOT_PROBED = "not_probed"
    RELEASE_CHANGED = "release_changed"


@dataclass(frozen=True)
class ShapeEvidence:
    """Content-free description of what a call returned: never a value, never an LMS id."""

    #: the provider's name for the remote call (a web-service function, an endpoint...)
    call: str
    release: str | None
    signature: str  # sha256 over the canonical shape signature (keys, types, counts)


@dataclass(frozen=True)
class Readable[T]:
    value: T
    evidence: ShapeEvidence | None = None


@dataclass(frozen=True)
class Unreadable:
    reason: UnreadableReason
    #: a short machine code (an LMS error code, an HTTP status class...): never a message
    detail_code: str = ""

    def __post_init__(self) -> None:
        if len(self.detail_code) > 64 or not all(
            c.isalnum() or c in "_-." for c in self.detail_code
        ):
            raise ValueError("detail_code must be a short [A-Za-z0-9_.-] code")


type Reading[T] = Readable[T] | Unreadable

#: the ``detail_code`` of the re-resolution refusal (REQ-095, ADR-0033 D2 invariant 3)
NOT_IN_USER_COURSES = "activity_not_in_user_courses"
#: the ``detail_code`` the vertical uses when a provider let an exception escape (D2 invariant 1)
PROVIDER_ERROR = "provider_error"
#: ``submission_file`` refusals of an ownership re-check (the vertical audits them): the
#: submission is not the given one of the given owner, or the file is not in it
SUBMISSION_MISMATCH = "submission_mismatch"
FILE_NOT_IN_SUBMISSION = "file_not_in_submission"

# --- vocabulary ----------------------------------------------------------------------------

#: vertical vocabulary, kept as is: renaming it would touch CHECK constraints and sealed literals
ActivityKind = Literal["quiz", "assign"]


class StaffRole(StrEnum):
    GRADER = "grader"
    EDITOR = "editor"
    OVERRIDE_MANAGER = "override_manager"


class OverdueMode(StrEnum):
    """What happens to an attempt still open at the close time."""

    AUTOSUBMIT = "autosubmit"
    GRACE = "grace"
    ABANDON = "abandon"


class GradeMethod(StrEnum):
    """How the activity grade is derived from several attempts."""

    HIGHEST = "highest"
    AVERAGE = "average"
    FIRST = "first"
    LAST = "last"


class AttemptState(StrEnum):
    IN_PROGRESS = "in_progress"
    OVERDUE = "overdue"
    FINISHED = "finished"
    ABANDONED = "abandoned"


class QuestionState(StrEnum):
    """The state of one reviewed item, normalised by the provider.

    ``UNKNOWN`` is any state the provider cannot place: it is never final (fail closed).
    """

    FINAL_MARKED = "final_marked"
    FINAL_NO_MARK = "final_no_mark"  # final, but not answered: the LMS shows no mark
    PENDING = (
        "pending"  # not final yet: unanswered, incomplete, or waiting for a grader
    )
    UNKNOWN = "unknown"


class QuestionKind(StrEnum):
    """Vektra's own question kinds. A provider maps its question types onto them, and maps
    any type it cannot place onto ``OTHER``, which the vertical keeps at mark-and-feedback
    level."""

    MULTIPLE_CHOICE = "multiple_choice"
    TRUE_FALSE = "true_false"
    SHORT_ANSWER = "short_answer"
    NUMERICAL = "numerical"
    ESSAY = "essay"
    MATCHING = "matching"
    EMBEDDED_ANSWERS = "embedded_answers"  # gaps embedded in one text ("cloze")
    GAP_SELECT = "gap_select"
    INFORMATION = "information"  # never graded
    OTHER = "other"


# --- site and resolution -------------------------------------------------------------------


@dataclass(frozen=True)
class LmsSite:
    site_id: str
    base_url: str  # https://lms.example.org (no path, no credentials)
    release: str | None = None


@dataclass(frozen=True)
class LmsSiteInfo:
    """What the vertical may know about a site. The function list and the service account's
    flags stay inside the provider, which uses them for its own setup items (D3)."""

    release: str
    branch: str


@dataclass(frozen=True)
class ActivityRef:
    course_id: int
    instance_id: int  # the quiz id or the assignment id
    #: the course's short name, from the same course list the resolution read (the transcript
    #: file name); not part of the identity
    course_shortname: str | None = field(default=None, compare=False)


# --- people --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Enrolment:
    """Active enrolment of one user in one course (the re-check of NFR-014)."""

    course_id: int
    user_id: int
    active: bool
    is_student: bool
    roles: tuple[str, ...]


@dataclass(frozen=True)
class StaffMember:
    user_id: int
    fullname: str
    email: str | None  # None when the LMS does not disclose it to the service account
    roles: tuple[str, ...]


# --- quiz ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class GracePolicy:
    grace_period_s: int
    overdue: OverdueMode


@dataclass(frozen=True)
class ReviewVisibility:
    """Which parts of a reviewed attempt the student may see (BR-011). Flags only: none of
    these fields carries the content it governs.

    ``marks``: 0 hidden, 1 maximum only, 2 mark and maximum.
    """

    show_feedback: bool  # the specific, per-question feedback
    show_general_feedback: bool
    show_right_answer: bool
    show_overall_feedback: bool
    marks: int


@dataclass(frozen=True)
class ReviewOptions:
    """Review visibility for one student. ``all_attempts`` holds on every attempt,
    ``some_attempts`` on at least one: a consumer that must not leak uses ``all_attempts``."""

    all_attempts: ReviewVisibility
    some_attempts: ReviewVisibility


@dataclass(frozen=True)
class QuizOverride:
    user_id: int | None  # exactly one of user_id / group_id is set
    group_id: int | None
    time_open: int | None
    time_close: int | None
    time_limit_s: int | None
    attempts: int | None


@dataclass(frozen=True)
class QuizSettings:
    quiz_id: int
    course_id: int
    name: str
    time_open: int
    time_close: int  # 0 = no close
    time_limit_s: int  # 0 = no limit
    grade_method: GradeMethod
    attempts_allowed: int  # 0 = unlimited
    #: caller-relative: readable only while the quiz is open for the service account
    grace: Reading[GracePolicy]
    review_after_close: Reading[ReviewVisibility]
    #: user and group overrides of ALL participants; Unreadable where the LMS does not expose them
    overrides: Reading[tuple[QuizOverride, ...]]


@dataclass(frozen=True)
class AttemptSummary:
    attempt_id: int
    attempt_number: int
    user_id: int
    state: AttemptState
    preview: bool
    time_start: int
    time_finish: int  # 0 = not finished
    sum_grades: float | None  # None = not (fully) graded


@dataclass(frozen=True)
class HtmlExtractionProfile:
    """Declarative extraction rules for one provider's review HTML (ADR-0033 D4).

    ``keep`` maps a block name the vertical understands (``question_text``, ``response``...)
    to the CSS class that marks it in this provider's markup. ``drop_classes`` are removed
    wherever they appear inside a kept block. The sidecar holds the parsing, the isolation and
    the escaping; the profile holds every class name.

    ``kinds`` are the question kinds the provider has proved the profile safe for (its own
    per-kind oracle on its recorded reviews): the vertical extracts text only for a kind that
    is both in its own list and here. Empty means none (fail closed).
    """

    keep: Mapping[str, str]
    drop_classes: frozenset[str]
    profile_version: str
    kinds: frozenset[QuestionKind] = frozenset()


@dataclass(frozen=True)
class ReviewHtml:
    """Rendered review HTML, as the service account sees it: with teacher visibility, so it
    contains material the student may not see. It never reaches a model as is; only what the
    profile keeps does."""

    html: str
    profile: HtmlExtractionProfile


@dataclass(frozen=True)
class ReviewItem:
    slot: int
    display_number: str  # "3", or "i" for an information item
    question_kind: QuestionKind
    state: QuestionState
    status_label: str | None  # the LMS's human label ("Correct"...)
    mark: float | None  # None = no mark yet
    max_mark: float
    needs_grading: bool
    html: ReviewHtml | None


@dataclass(frozen=True)
class AttemptReview:
    attempt_id: int
    quiz_id: int
    user_id: int
    state: AttemptState
    sum_grades: float | None
    grade: float | None
    items: tuple[ReviewItem, ...]

    def without_html(self) -> AttemptReview:
        return replace(self, items=tuple(replace(i, html=None) for i in self.items))


# --- assignment ----------------------------------------------------------------------------


@dataclass(frozen=True)
class AssignmentSettings:
    assignment_id: int
    activity_id: int
    course_id: int
    name: str
    due_date: int  # 0 = none
    cutoff_date: int  # 0 = none
    grading_due_date: int
    allow_submissions_from: int
    team_submission: bool
    marking_workflow: bool
    grade_type: Literal["point", "scale", "none"]
    scale_id: int | None
    grade_max: float | None  # None for a scale


@dataclass(frozen=True)
class AssignmentDates:
    """Effective dates for ONE student: the activity values with the student's override and
    extension applied."""

    assignment_id: int
    user_id: int
    due_date: int
    cutoff_date: int
    allow_submissions_from: int
    extension_due_date: int | None
    granted_extension: bool


@dataclass(frozen=True)
class SubmissionFileRef:
    """A file of a submission. ``locator`` is opaque to the vertical: only the provider that
    returned it can fetch it. It carries no credential and is NEVER logged."""

    filename: str
    filepath: str
    size: int
    mimetype: str
    locator: str
    time_modified: int


@dataclass(frozen=True)
class Feedback:
    grade_id: int | None  # joins rubric fillings (``RubricFilling.item_id``)
    grade: float | None
    grade_for_display: str
    graded_date: int | None
    comments: tuple[str, ...]


@dataclass(frozen=True)
class Submission:
    assignment_id: int
    user_id: int
    submission_id: int | None  # the TEAM's submission id when ``team_submission``
    team_submission: bool
    state: str | None  # new | draft | submitted | reopened
    time_modified: int | None
    online_text: str | None
    files: tuple[SubmissionFileRef, ...]
    grading_status: (
        str  # notgraded | graded | inmarking | readyforreview | released ...
    )
    graded: bool
    extension_due_date: int | None
    #: None when the LMS withholds it (hidden grade item, unreleased)
    feedback: Feedback | None


# --- grading -------------------------------------------------------------------------------


@dataclass(frozen=True)
class GradeItem:
    """Release state of ONE grade item for ONE student.

    ``hidden`` covers a hidden item and a hidden-until date. The value fields are None
    whenever the item is hidden: a provider never surfaces an unreleased grade.
    """

    activity_id: int
    kind: ActivityKind
    hidden: bool
    hidden_by_date: bool
    locked: bool
    grade: float | None
    grade_formatted: str | None
    grade_max: float
    range_formatted: str
    feedback: str | None


@dataclass(frozen=True)
class AssignmentRelease:
    """BR-010 for an assignment: grade item visible AND, with a marking workflow, the workflow
    state released. Derived from student-visible reads only, never from a grade list that
    includes unreleased values."""

    assignment_id: int
    user_id: int
    grade_item: GradeItem
    grading_status: str
    marking_workflow: bool
    released: bool


@dataclass(frozen=True)
class RubricLevel:
    level_id: int
    score: float
    definition: str


@dataclass(frozen=True)
class RubricCriterion:
    criterion_id: int
    sort_order: int
    description: str
    levels: tuple[RubricLevel, ...]


@dataclass(frozen=True)
class RubricDefinition:
    """Criteria and level texts. Whether a student may see them is not readable, so BR-011
    excludes them from the student context."""

    definition_id: int
    name: str
    criteria: tuple[RubricCriterion, ...]


@dataclass(frozen=True)
class MarkingGuideSummary:
    """Detection only: a marking guide's marker notes are teacher-only and have no field."""

    definition_id: int
    criteria_count: int
    max_total: float


@dataclass(frozen=True)
class GradingDefinition:
    activity_id: int
    method: str  # none | rubric | guide | <another method's name>
    rubric: RubricDefinition | None
    guide: MarkingGuideSummary | None


@dataclass(frozen=True)
class RubricCriterionFilling:
    criterion_id: int
    level_id: int
    remark: str


@dataclass(frozen=True)
class RubricFilling:
    instance_id: int
    item_id: int  # the grade id (``Feedback.grade_id``)
    status: int
    criteria: tuple[RubricCriterionFilling, ...]
