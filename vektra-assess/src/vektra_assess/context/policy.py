"""BR-011 disclosure decision, computed BEFORE any content is read (ARCH-069, ADR-0032).

``derive_policy`` is a pure function of four inputs:

- the teacher's "mostra soluzione" flag (frozen at Start);
- the student's combined after-close review options (``all_attempts``: what is visible on EVERY
  attempt; ``some_attempts`` would leak);
- the capability flags ``review_html_filterable`` and ``rubric_display_settings``;
- the release state (BR-010).

Every unknown fails closed: an unreadable review option hides what it governs, an unreadable
capability selects the fallback, an unreleased grade makes the attempt not eligible. The capture
step projects the readers' output through the result, and excluded fields are dropped inside the
capture frame as soon as a reader returns.

Pure and dependency-free (no Moodle client, no DB): the API process imports it to read a session's
frozen policy.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from vektra_assess.context.snapshot import FrozenFlags

#: bump when the decision table changes: the exclusion audit event records it
POLICY_VERSION = 1

CapabilityState = Literal["readable", "partial", "unreadable"]


class HtmlPath(StrEnum):
    #: review HTML goes to the sidecar sanitizer with a removal list; text comes back
    FILTERABLE = "filterable"
    #: review HTML never leaves the capture frame; only verified structured fields are used.
    #: The DEFAULT today: OQ-025 found the HTML not filterable on 4.3 or 5.2.
    STRUCTURED_ONLY = "structured_only"


class ReviewOptions(BaseModel):
    """The student's combined review options on every attempt (``UserReviewOptions.all_attempts``).

    ``marks``: 0 hidden, 1 maximum only, 2 mark and maximum.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    feedback: bool
    generalfeedback: bool
    rightanswer: bool
    overallfeedback: bool
    marks: Literal[0, 1, 2]


class PolicyInputs(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    flags: FrozenFlags
    #: None = unreadable (fails closed: everything it governs is hidden). Assignments: None.
    review: ReviewOptions | None
    review_html_filterable: CapabilityState
    rubric_display_settings: CapabilityState
    #: whether rubric texts are visible to students, when the setting is readable at all
    rubric_descriptors_visible: bool | None
    released: bool


class DisclosurePolicy(BaseModel):
    """What the snapshot may contain. Every ``False`` is an exclusion that is recorded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    eligible: (
        bool  # False: grade or feedback not released (BR-010) -> NotEligible, 0 rows
    )
    solution: bool
    marks: bool  # the student's mark per item
    max_marks: bool  # the maximum per item
    specific_feedback: bool
    general_feedback: bool
    right_answer: bool
    overall_feedback: bool
    html_path: HtmlPath
    rubric_descriptors: bool
    pasted_rubric: bool  # student-visible by definition when the source is "pasted"

    def removal_classes(self) -> tuple[str, ...]:
        """Moodle question-part CSS classes the sanitizer must remove (filterable path)."""
        out = []
        if not self.right_answer:
            out.append("rightanswer")
        if not self.specific_feedback:
            out += ["specificfeedback", "outcome"]
        if not self.general_feedback:
            out.append("generalfeedback")
        if not self.marks:
            out.append("grade")
        return tuple(sorted(out))


class FrozenPolicy(BaseModel):
    """Persisted with the session at Start and never changed after commit (BR-008)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    inputs: PolicyInputs
    policy: DisclosurePolicy


#: an unreadable review option hides everything it governs (fail closed)
_HIDDEN = ReviewOptions(
    feedback=False,
    generalfeedback=False,
    rightanswer=False,
    overallfeedback=False,
    marks=0,
)


def derive_policy(inputs: PolicyInputs) -> DisclosurePolicy:
    r = inputs.review if inputs.review is not None else _HIDDEN
    html_ok = inputs.review_html_filterable == "readable"
    rubric_ok = (
        inputs.rubric_display_settings == "readable"
        and inputs.rubric_descriptors_visible is True
    )
    return DisclosurePolicy(
        version=POLICY_VERSION,
        eligible=inputs.released,
        solution=inputs.flags.show_solution,
        marks=r.marks == 2,
        max_marks=r.marks >= 1,
        specific_feedback=r.feedback,
        general_feedback=r.generalfeedback,
        right_answer=r.rightanswer,
        overall_feedback=r.overallfeedback,
        html_path=HtmlPath.FILTERABLE if html_ok else HtmlPath.STRUCTURED_ONLY,
        rubric_descriptors=rubric_ok,
        pasted_rubric=inputs.flags.rubric_source == "pasted",
    )


def freeze(inputs: PolicyInputs) -> FrozenPolicy:
    return FrozenPolicy(inputs=inputs, policy=derive_policy(inputs))
