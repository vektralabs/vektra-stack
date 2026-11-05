"""Attempt snapshot: the permitted projection of one attempt, frozen at Start (BR-008, BR-015).

The models are an ALLOWLIST. Every field a model may ever see is declared here, by item kind;
there is no free-form ``dict`` or ``Any`` and no raw Moodle payload. New Moodle data reaches the
model only through a change to these classes, and a change here needs a canary cell
(``tests/canary``). Moodle ids (attempt, slot, cmid, user) are deliberately absent: items are
addressed by snapshot-local labels only (REQ-095).

Canonical form: sorted keys, marks as decimal strings, every string NFC-normalised;
``content_hash = sha256(canonical_bytes)``. The hash goes in the content-free commit audit event and
is re-checked at transcript generation (BR-015).

This module imports nothing from ``vektra_assess.moodle`` or the parser: the API process imports
it to read a session's snapshot.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from vektra_assess.parser_protocol import FileStatus

SCHEMA_VERSION: Literal[1] = 1


def mark_str(value: float | int | None) -> str | None:
    """A mark as a canonical decimal string: ``7.5``, ``10``, ``0.333``. Never a float."""
    if value is None:
        return None
    if isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError("a mark must be a finite number")
    # Moodle stores marks as DECIMAL(12,7): 7 places is the precision that exists
    text = format(Decimal(repr(round(float(value), 7))).normalize(), "f")
    return "0" if text in ("-0", "") else text


def _check_mark(value: str) -> str:
    try:
        canonical = mark_str(float(Decimal(value)))
    except (ArithmeticError, ValueError):
        raise ValueError(f"mark {value!r} is not a decimal") from None
    if value != canonical:
        raise ValueError(
            f"mark {value!r} is not in canonical decimal form ({canonical!r})"
        )
    return value


#: a mark, a maximum or points: always a canonical decimal string (``mark_str``)
Mark = Annotated[str, AfterValidator(_check_mark)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ActivityKind(StrEnum):
    QUIZ = "quiz"
    ASSIGN = "assign"


class GradeMethod(StrEnum):
    HIGHEST = "highest"
    AVERAGE = "average"
    FIRST = "first"
    LAST = "last"


class MarksHidden(StrEnum):
    """Why a mark is absent from an item (stated in Part A, REQ-094)."""

    REVIEW_OPTIONS = "review_options"  # marks hidden by after-close review options
    NOT_GRADED = "not_graded"  # a zero-mark or information item


# --- items --------------------------------------------------------------------------------------


class QuizItem(_Frozen):
    """One question of the selected attempt, as the student may see it."""

    label: str  # display numbering as on the review page: "3", "i"
    qtype: str
    discussable: bool  # REQ-101 list; others are mark-and-feedback level and flagged
    state: str | None
    status: str | None  # the student-visible state label ("Corretta" ...)
    mark: Mark | None  # None when hidden or not applicable (see marks_hidden)
    max_mark: Mark | None
    marks_hidden: MarksHidden | None = None
    #: plain text extracted by ALLOWLIST from the review HTML (ADR-0032 amendment 2026-11-04):
    #: only the question text block and the student's response. ``None`` when the type is not
    #: enabled for extraction or the blocks could not be isolated (then ``degraded``).
    question_text: str | None = None
    response_text: str | None = None
    #: student-visible text from the sanitized review HTML (filterable path only). ``None`` on
    #: the structured-only path: the html never left the capture frame.
    review_text: str | None = None
    degraded: bool = False  # flagged in Part A: the item could not be fully read


class AssignCriterion(_Frozen):
    """One rubric criterion: level, points and remark always; descriptors only if permitted."""

    label: str  # "C1", "C2" ... snapshot-local
    level_index: int  # 1-based position of the chosen level, ordered by score
    level_count: int
    points: Mark
    max_points: Mark
    remark: str
    #: criterion description and level texts: ``None`` unless the rubric display settings are
    #: READABLE and student-visible. Today they are never readable (OQ-025 / OQ-035).
    description: str | None = None
    chosen_level_text: str | None = None
    adjacent_level_texts: tuple[str, ...] = ()


class GradeKind(StrEnum):
    POINTS = "points"
    SCALE = "scale"


class OverallItem(_Frozen):
    """The released grade and overall comment of the attempt or submission."""

    grade_kind: GradeKind
    grade: Mark | None  # decimal string for points; None for a scale (see scale_label)
    grade_max: Mark | None
    #: the grade exactly as Moodle shows it to the student ("2.31", "85.00"): what the BR-009
    #: statement quotes. ``grade`` keeps the full value for the regrade comparison.
    grade_display: str | None = None
    #: BR-014: a scale grade is its label, never "punti/max"
    scale_label: str | None = None
    overall_feedback: str | None = None  # released and visible only
    grade_read_at: int  # epoch seconds: the "letto il <data>" of the BR-009 statement


class SubmissionItem(_Frozen):
    onlinetext: str | None
    truncated: bool = False  # over the text budget
    # the sanitizer could not read the online text (not merely blank): the model is told so,
    # as it is for an unreadable file, instead of the text silently vanishing
    unreadable: bool = False


class FileEntry(_Frozen):
    """One submitted file and exactly one REQ-101 status."""

    label: str  # "F1", "F2" ... snapshot-local
    filename: str
    status: FileStatus
    text: str = ""  # only for READ / READ_PARTIAL
    upto_page: int | None = None
    pages_total: int | None = None

    @field_validator("text")
    @classmethod
    def _text_only_when_read(cls, v: str, info: Any) -> str:
        status = info.data.get("status")
        if v and status not in (FileStatus.READ, FileStatus.READ_PARTIAL):
            raise ValueError(
                "file text is allowed only with status READ or READ_PARTIAL"
            )
        return v


class ExclusionRecord(_Frozen):
    """The BR-011 decision: which field paths were excluded and which fallback was used.

    Paths only (``q3.right_answer``, ``solution_text``, ``rubric.c2.descriptors``), never values.
    """

    policy_version: int
    excluded_paths: tuple[str, ...]
    html_structured_only: bool
    rubric_descriptors_excluded: bool
    solution_shown: bool

    @field_validator("excluded_paths")
    @classmethod
    def _sorted_unique(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if list(v) != sorted(set(v)):
            raise ValueError("excluded_paths must be sorted and unique")
        return v

    def field_set_hash(self) -> str:
        """sha256 over the sorted path list (the exclusion audit event's hash)."""
        return hashlib.sha256(
            "\n".join(self.excluded_paths).encode("utf-8")
        ).hexdigest()


class FrozenFlags(_Frozen):
    """Config flags frozen at Start (BR-008): a later toggle never widens a running session."""

    show_solution: bool
    rubric_source: Literal["none", "pasted", "moodle"]
    course_material: bool = False  # RAG: locked OFF in v1 (NFR-015)


class SnapshotHeader(_Frozen):
    schema_version: Literal[1] = SCHEMA_VERSION
    activity_kind: ActivityKind
    moodle_release: str
    attempt_number: int  # "#k"
    attempt_count: int  # "di M"
    grade_method: GradeMethod | None  # quiz only
    review_options_closed: bool  # quiz: the after-close review options applied
    grade_released: bool
    grade_modified_at: int | None
    captured_at: int
    flags: FrozenFlags


class Snapshot(_Frozen):
    header: SnapshotHeader
    quiz_items: tuple[QuizItem, ...] = ()
    criteria: tuple[AssignCriterion, ...] = ()
    overall: OverallItem
    submission: SubmissionItem | None = None
    files: tuple[FileEntry, ...] = ()
    exclusion: ExclusionRecord
    #: present only when show_solution was ON at Start (BR-011)
    solution_text: str | None = None
    #: student-visible by definition (BR-011): always included when the source is "pasted"
    pasted_rubric: str | None = None
    #: verified student-visible structured fields only: how many items were degraded
    degraded_count: int = Field(default=0, ge=0)

    def labels(self) -> tuple[str, ...]:
        return tuple(i.label for i in self.quiz_items) + tuple(
            c.label for c in self.criteria
        )


# --- canonical form ------------------------------------------------------------------------------


def _nfc(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {_nfc(k): _nfc(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_nfc(v) for v in value]
    if isinstance(value, float):
        raise TypeError("floats are not allowed in a snapshot: use mark_str()")
    return value


def canonical_bytes(snapshot: Snapshot) -> bytes:
    data = _nfc(snapshot.model_dump(mode="json"))
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def content_hash(snapshot: Snapshot) -> str:
    return hashlib.sha256(canonical_bytes(snapshot)).hexdigest()


def from_canonical_bytes(data: bytes) -> Snapshot:
    """Parse a decrypted snapshot. Strict: an unknown field or a wrong type is an error."""
    return Snapshot.model_validate_json(data)
