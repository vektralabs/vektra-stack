"""Request and response shapes of the teacher endpoints (plan assess-teacher-ops, INT-001).

``ConfigPut`` is strict (``extra="forbid"``, strict scalar types): a malformed body is the 400
``ERR-ASSESS-014`` envelope of ``api.common.parse_body``, never FastAPI's 422. The BR-013 ranges are
NOT encoded here: a value out of range is a field-level 400 ``ERR-ASSESS-022`` that names the
range (``teacher.validation``), which a schema constraint could not do.

Write-only fields (OQ-034): ``solution``, ``pasted_rubric`` and ``transcript_recipients`` are
tri-state. Absent = unchanged; ``null`` = clear; a value = replace. Responses never carry them:
only ``impostata`` / ``non impostata`` (and the recipients' count).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from dataclasses import field as dc_field
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

#: the values ``*_state`` takes in a response (Italian, as the plugin shows them)
STATE_SET = "impostata"
STATE_UNSET = "non impostata"

ActivityType = Literal["quiz", "assign"]
RubricSource = Literal["none", "pasted", "moodle"]

#: schema bounds of the recipients list (the rules allow 1..3 addresses of <= 254 bytes; these
#: only bound the body: beyond them a request is malformed, below them it gets field errors)
MAX_RECIPIENT_ITEMS = 20
MAX_RECIPIENT_CHARS = 512
RecipientStr = Annotated[StrictStr, Field(max_length=MAX_RECIPIENT_CHARS)]

#: the write-only fields of a PUT, in their sealed-field names
SECRET_FIELDS = ("solution", "pasted_rubric", "transcript_recipients")


class TriState(StrEnum):
    UNCHANGED = "unchanged"
    CLEAR = "clear"
    REPLACE = "replace"


class ConfigPut(BaseModel):
    """``PUT /api/v1/assess/activities/{cmid}/config``."""

    model_config = ConfigDict(extra="forbid")

    actor_userid: StrictInt = Field(ge=1)
    #: hints for the worker's teacher_gate refresh: Vektra re-resolves the cmid inside this
    #: course as this type, it never trusts them as facts
    courseid: StrictInt = Field(ge=1)
    activity_type: ActivityType
    #: optimistic concurrency: the ``version`` the form was loaded with (0 = never saved)
    expected_version: StrictInt = Field(ge=0)

    enabled: StrictBool | None = None
    cap_minutes: StrictInt | None = None
    rubric_source: RubricSource | None = None
    show_solution: StrictBool | None = None
    rag_enabled: StrictBool | None = None
    #: "Discussione disponibile fino al": null = no limit (until retention)
    available_until: datetime | None = None

    solution: StrictStr | None = Field(None, max_length=200_000)
    pasted_rubric: StrictStr | None = Field(None, max_length=200_000)
    transcript_recipients: list[RecipientStr] | None = Field(
        None, max_length=MAX_RECIPIENT_ITEMS
    )

    def tri_state(self, name: str) -> TriState:
        """Absent / null / value of one optional field (OQ-034 write-only semantics)."""
        if name not in self.model_fields_set:
            return TriState.UNCHANGED
        return TriState.CLEAR if getattr(self, name) is None else TriState.REPLACE

    def given(self, name: str) -> bool:
        return name in self.model_fields_set


#: body bound of a teacher's reason; the rule (1..1000 code points after normalisation) is a
#: field error naming its range (``teacher.validation.check_reason``)
MAX_REASON_BODY_CHARS = 4000
ReasonStr = Annotated[StrictStr, Field(max_length=MAX_REASON_BODY_CHARS)]


class TeacherAction(BaseModel):
    """The common part of every teacher operation (plan assess-teacher-ops 11b): the acting
    Moodle user (verified as a fresh grader) and the hints for the worker's refresh."""

    model_config = ConfigDict(extra="forbid")

    actor_userid: StrictInt = Field(ge=1)
    courseid: StrictInt = Field(ge=1)
    activity_type: ActivityType


class ResetPost(TeacherAction):
    """``POST /activities/{cmid}/sessions/{sid}/reset`` (REQ-087)."""

    reason: ReasonStr
    #: REQ-087 (b): absent = the default (ON only after a ``service_failure`` close)
    include_previous_turns: StrictBool | None = None


class TerminatePost(TeacherAction):
    """``POST /activities/{cmid}/terminate`` (REQ-081)."""

    reason: ReasonStr
    #: the "N sessioni attive" the teacher confirmed; another number now is a 409
    expected_active_count: StrictInt = Field(ge=0)
    also_disable: StrictBool = False


class ExtraTimePut(TeacherAction):
    """``PUT /activities/{cmid}/students/{userid}/extra-time`` (REQ-078). Exactly one of
    ``minutes`` / ``percent`` (a field error otherwise); no reason field exists."""

    minutes: StrictInt | None = None
    percent: StrictInt | None = None
    confirm_over_100: StrictBool = False


class OpeningFallbackPut(TeacherAction):
    """``PUT /activities/{cmid}/opening-fallback`` (REQ-091)."""

    opening_at: datetime
    #: the teacher ticked the attestation; required on every edit
    attestation: StrictBool
    #: the version of the attestation text the plugin showed (audited with the date)
    attestation_text_version: Annotated[
        StrictStr, Field(pattern=r"^[A-Za-z0-9._-]{1,32}$")
    ]


@dataclass(frozen=True)
class FieldError:
    """One field-level error: the field, a content-free code and the rule's bounds. Never the
    submitted value."""

    field: str
    code: str
    range: str | None = None
    extra: dict[str, Any] = dc_field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        out = {k: v for k, v in asdict(self).items() if k != "extra" and v is not None}
        out.update(self.extra)
        return out


def field_errors(errors: list[FieldError]) -> list[dict[str, Any]]:
    """The ``details.fields`` list of ``ERR-ASSESS-022``, in a stable order."""
    return [e.as_json() for e in errors]


def state(is_set: bool) -> str:
    return STATE_SET if is_set else STATE_UNSET
