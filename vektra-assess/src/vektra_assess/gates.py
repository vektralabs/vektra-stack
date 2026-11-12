"""START gates (BR-006 (a)-(i), BR-007, BR-010, BR-014, REQ-091): one pure decision function.

``evaluate_start_gates(inputs) -> GateDecision`` has no I/O, no clock of its own and no LMS
client: the caller reads the inputs (the worker, from the plan-4 readers and the database) and
passes ``now``. Every input that could not be read is ``None`` and fails closed. The gates are
checked in a FIXED order and the first one that fails gives the content-free reason ``code``; the
same function serves the advisory ``/state`` (render) and the authoritative pre-flight (Start),
so the button and a crafted Start can never disagree. Order: enabled, enrolment, team / grading
method, end date, sessions, privacy, attempts and BR-014, release, close, ownership.

The effective close of BR-006 (d) is computed here:

- BR-007 first: a quiz needs a close date, an assignment a CUT-OFF date; the REQ-091 fallback
  never replaces a missing one (``no_close_date``);
- automatic, when EVERY close input is readable (the activity close, the grace period, the
  overrides and extensions of ALL participants): latest = max(activity close, every participant's
  override or extension) + grace; with a teacher date the gate is max(automatic, teacher date),
  never the teacher date alone;
- REQ-091 fallback, when ANY close input is unreadable (partial counts as unreadable): without a
  teacher-attested opening date the activity is NOT_AVAILABLE (``close_unreadable``); with one, the
  gate is max(activity effective close, teacher date), never the activity close alone.

``opens_at`` is set only when the date may be shown to this student (REQ-068 state 3): when the
gate equals the activity's own effective close or the student's own override. A date held by
another student's override or extension is never returned.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class ButtonState(StrEnum):
    """REQ-068 advisory states the API can decide (plugin-local states are not here)."""

    NOT_ENABLED = "not_enabled"  # (1)
    NOT_GRADED = "not_graded"  # (2): not fully graded OR not released
    NOT_OPEN = "not_open"  # (3): gate not open, incl. the REQ-091 NOT_AVAILABLE state
    ELIGIBLE = "eligible"  # (4)
    ACTIVE = "active"  # (5)
    CLOSED = "closed"  # (6)
    UNAVAILABLE = "unavailable"  # (7): temporarily unavailable, non-technical message
    RESET_GRANTED = "reset_granted"  # (10)
    ENDED = "ended"  # (11): the teacher's end date passed


class Code(StrEnum):
    OK = "ok"
    NOT_ENABLED = "not_enabled"  # (a)
    TEAM_SUBMISSION = "team_submission"  # (a)
    MARKING_GUIDE = "marking_guide"  # (a), EX-023
    UNSUPPORTED_GRADING = "unsupported_grading"  # (a)
    ATTEMPT_IN_PROGRESS = "attempt_in_progress"  # (b)
    NO_SUBMITTED_ATTEMPT = "no_submitted_attempt"  # (b)
    REGRADE_IN_PROGRESS = "regrade_in_progress"  # (b) / BR-014
    NOT_FULLY_GRADED = "not_fully_graded"  # (b) / BR-014
    NOT_RELEASED = "not_released"  # (c) / BR-010
    NO_CLOSE_DATE = "no_close_date"  # BR-007
    CLOSE_UNREADABLE = "close_unreadable"  # (d) / REQ-091 fallback, no teacher date
    CLOSE_NOT_PASSED = "close_not_passed"  # (d)
    NOT_OWNER = "not_owner"  # (e)
    NOT_ENROLLED = "not_enrolled"  # (f)
    SESSION_OPEN = "session_open"  # (g)
    ALREADY_DISCUSSED = "already_discussed"  # (g)
    PRIVACY_NOT_CONFIGURED = "privacy_not_configured"  # (h)
    END_DATE_PASSED = "end_date_passed"  # (i)
    UNREADABLE = "unreadable"  # any other input that could not be read (fail closed)


_STATE: dict[Code, ButtonState] = {
    Code.OK: ButtonState.ELIGIBLE,
    Code.NOT_ENABLED: ButtonState.NOT_ENABLED,
    Code.TEAM_SUBMISSION: ButtonState.NOT_ENABLED,
    Code.MARKING_GUIDE: ButtonState.NOT_ENABLED,
    Code.UNSUPPORTED_GRADING: ButtonState.NOT_ENABLED,
    Code.ATTEMPT_IN_PROGRESS: ButtonState.NOT_OPEN,
    Code.NO_SUBMITTED_ATTEMPT: ButtonState.NOT_GRADED,
    Code.REGRADE_IN_PROGRESS: ButtonState.NOT_GRADED,
    Code.NOT_FULLY_GRADED: ButtonState.NOT_GRADED,
    Code.NOT_RELEASED: ButtonState.NOT_GRADED,
    Code.NO_CLOSE_DATE: ButtonState.NOT_OPEN,
    Code.CLOSE_UNREADABLE: ButtonState.NOT_OPEN,
    Code.CLOSE_NOT_PASSED: ButtonState.NOT_OPEN,
    Code.NOT_OWNER: ButtonState.UNAVAILABLE,
    Code.NOT_ENROLLED: ButtonState.UNAVAILABLE,
    Code.SESSION_OPEN: ButtonState.ACTIVE,
    Code.ALREADY_DISCUSSED: ButtonState.CLOSED,
    Code.PRIVACY_NOT_CONFIGURED: ButtonState.UNAVAILABLE,
    Code.END_DATE_PASSED: ButtonState.ENDED,
    Code.UNREADABLE: ButtonState.UNAVAILABLE,
}

#: the gates decided from Vektra's own database (the configuration, the sessions, privacy); every
#: other failing code needs LMS facts, which only the worker reads (render or pre-flight). A
#: teacher reset (REQ-087) re-checks these live and takes the others from a fresh render.
DB_SIDE: frozenset[Code] = frozenset(
    {
        Code.NOT_ENABLED,
        Code.END_DATE_PASSED,
        Code.SESSION_OPEN,
        Code.ALREADY_DISCUSSED,
        Code.PRIVACY_NOT_CONFIGURED,
    }
)

#: assignment grading methods discussable in v1 (BR-014); "guide" is refused (EX-023)
DISCUSSABLE_METHODS = frozenset({"none", "simple", "rubric", ""})


@dataclass(frozen=True)
class GateInputs:
    """Everything the gates read. ``None`` = unreadable (fails closed), never "no" or "zero"."""

    now: int  # epoch seconds, the caller's clock (the database's in production)
    activity_kind: Literal["quiz", "assign"]
    # (a)
    enabled: bool
    team_submission: bool | None = None  # assignments
    grading_method: str | None = (
        None  # assignments: none | simple | rubric | guide | ...
    )
    # (b) + BR-014
    attempt_in_progress: bool | None = None  # any inprogress / overdue attempt
    has_submitted_attempt: bool | None = None
    regrade_in_progress: bool | None = None
    fully_graded: bool | None = None
    # (c) BR-010: grade AND feedback visible to the student
    released: bool | None = None
    # (d) BR-007: the activity close (quiz timeclose / assignment cut-off), 0 = none configured
    activity_close_at: int | None = None
    grace_s: int | None = (
        0  # quiz graceperiod when overdue handling is graceperiod; else 0
    )
    #: overrides and extensions of ALL participants; None = unreadable (or partial)
    participant_closes: Sequence[int] | None = None
    own_close_at: int | None = None  # this student's own override / extension, if any
    teacher_opening_at: int | None = None  # REQ-091 attested "Apertura discussione"
    # (e) the attempt is the requester's own, verified server-side (BR-012)
    owner_verified: bool | None = True
    # (f) enrolled as a student (impersonation is refused at the token, REQ-084)
    enrolled: bool | None = None
    # (g)
    open_session: bool = False
    closed_sessions: int = 0
    reset_grants: int = 0
    # (h) REQ-089
    privacy_configured: bool = False
    # (i) "Discussione disponibile fino al"
    end_at: int | None = None


@dataclass(frozen=True)
class GateDecision:
    code: Code
    state: ButtonState
    gate_at: int | None = None  # the effective opening, when computable
    opens_at: int | None = None  # only when it may be shown to THIS student
    session_no_next: int = 1
    reasons: tuple[Code, ...] = field(default=())

    @property
    def open(self) -> bool:
        return self.code is Code.OK


@dataclass(frozen=True)
class Opening:
    """The one BR-006 (d) / BR-007 / REQ-091 decision (``decide_opening``). Start reads
    ``code`` and ``at``; the teacher pages show ``at``, ``source``, ``fallback_required``;
    ``PUT opening-fallback`` accepts a date only while ``fallback_applies``; the end date
    ("disponibile fino al") must follow ``floor``. Nothing recomputes any of them."""

    code: Code  # OK | CLOSE_UNREADABLE | NO_CLOSE_DATE
    #: the opening Start waits for; None while it cannot be decided
    at: int | None
    #: ``activity_close`` | ``attested`` | ``unreadable`` | ``unknown``
    source: str
    #: the activity's own close (quiz close / assignment cut-off): 0 none, None unreadable
    close_at: int | None
    #: the REQ-091 path: the close is known but another close input is not readable, so a
    #: teacher-attested date is what opens the activity (PUT opening-fallback accepts one)
    fallback_applies: bool
    #: ``fallback_applies`` and no teacher date yet: Start refuses ``close_unreadable`` until
    #: one is set, and a date would open it
    fallback_required: bool
    #: the earliest the opening can be: ``at`` when decided, the close plus grace while a
    #: teacher date is required (the date is on or after the close, the gate adds the grace),
    #: None when no opening can exist (no close configured, or the close unreadable)
    floor: int | None


def decide_opening(
    activity_close_at: int | None,
    *,
    grace_s: int | None,
    automatic_close_at: int | None,
    teacher_opening_at: int | None,
) -> Opening:
    """BR-006 (d) with BR-007 and REQ-091. ``automatic_close_at``: max(close, every
    participant's override or extension) + grace, None when ANY of those inputs is unreadable
    (partial counts as unreadable); ``grace_s``: the grace when readable (None counts 0 on the
    REQ-091 path, as the gate always did). The Start gate (live LMS reads) and every teacher
    route (the worker's teacher facts merged with the capability matrix,
    ``teacher.validation.opening_of``) decide through this function only."""
    close = activity_close_at
    teacher = teacher_opening_at

    def made(code: Code, at: int | None, source: str, **flags: Any) -> Opening:
        return Opening(
            code=code,
            at=at,
            source=source,
            close_at=close,
            fallback_applies=flags.get("applies", False),
            fallback_required=flags.get("required", False),
            floor=flags.get("floor", at),
        )

    if close is None:
        # the activity's own close cannot be read: no gate can be computed, with or without a
        # teacher date (REQ-091: max(activity close, teacher date) needs both)
        return made(Code.CLOSE_UNREADABLE, None, "unreadable")
    if close <= 0:
        # BR-007: the fallback never replaces a missing close, a stale teacher date included
        return made(Code.NO_CLOSE_DATE, None, "unknown")
    if automatic_close_at is not None:
        at = max(automatic_close_at, teacher or 0)
        source = (
            "attested"
            if teacher is not None and teacher >= automatic_close_at
            else "activity_close"
        )
        return made(Code.OK, at, source)
    own = close + (grace_s or 0)
    if teacher is None:  # REQ-091: NOT_AVAILABLE, fail closed
        return made(
            Code.CLOSE_UNREADABLE,
            None,
            "unreadable",
            applies=True,
            required=True,
            floor=own,
        )
    source = "attested" if teacher >= own else "activity_close"
    return made(Code.OK, max(own, teacher), source, applies=True)


def _effective_gate(i: GateInputs) -> tuple[Code, int | None, int | None]:
    """(code, gate_at, showable date) of BR-006 (d) with BR-007 and REQ-091."""
    close = i.activity_close_at
    grace = i.grace_s
    automatic = None
    if (
        close is not None
        and close > 0
        and grace is not None
        and i.participant_closes is not None
    ):
        automatic = max([close, *i.participant_closes]) + grace
    opening = decide_opening(
        close,
        grace_s=grace,
        automatic_close_at=automatic,
        teacher_opening_at=i.teacher_opening_at,
    )
    if opening.code is not Code.OK:
        return opening.code, None, None
    gate = opening.at
    assert close is not None and gate is not None
    own_effective = close + (grace or 0)
    showable = {own_effective}
    if i.own_close_at is not None:
        showable.add(i.own_close_at + (grace or 0))
    shown = gate if gate in showable else None
    if i.now < gate:
        return Code.CLOSE_NOT_PASSED, gate, shown
    return Code.OK, gate, shown


def evaluate_start_gates(i: GateInputs) -> GateDecision:
    failures: list[Code] = []
    gate_at = shown = None

    # (a) enabled
    if not i.enabled:
        failures.append(Code.NOT_ENABLED)
    # (f) enrolment, early: for a requester who is not a participant every other read is moot
    if i.enrolled is None:
        failures.append(Code.UNREADABLE)
    elif not i.enrolled:
        failures.append(Code.NOT_ENROLLED)
    # (a) no team submission, no marking guide (EX-023)
    if i.activity_kind == "assign":
        if i.team_submission is None or i.grading_method is None:
            failures.append(Code.UNREADABLE)
        elif i.team_submission:
            failures.append(Code.TEAM_SUBMISSION)
        elif i.grading_method == "guide":
            failures.append(Code.MARKING_GUIDE)
        elif i.grading_method not in DISCUSSABLE_METHODS:
            failures.append(Code.UNSUPPORTED_GRADING)
    # (i) the teacher's end date: checked early, it overrides every "not yet" state (11)
    if i.end_at is not None and i.now >= i.end_at and not i.open_session:
        failures.append(Code.END_DATE_PASSED)
    # (g) no open session; no consumed one unless a reset was granted since
    if i.open_session:
        failures.append(Code.SESSION_OPEN)
    elif i.closed_sessions > i.reset_grants:
        failures.append(Code.ALREADY_DISCUSSED)
    # (h) privacy settings
    if not i.privacy_configured:
        failures.append(Code.PRIVACY_NOT_CONFIGURED)
    # (b) attempts and BR-014
    if i.attempt_in_progress is None:
        failures.append(Code.UNREADABLE)
    elif i.attempt_in_progress:
        failures.append(Code.ATTEMPT_IN_PROGRESS)
    if i.has_submitted_attempt is None:
        failures.append(Code.UNREADABLE)
    elif not i.has_submitted_attempt:
        failures.append(Code.NO_SUBMITTED_ATTEMPT)
    if i.regrade_in_progress is None or i.fully_graded is None:
        failures.append(Code.UNREADABLE)
    elif i.regrade_in_progress:
        failures.append(Code.REGRADE_IN_PROGRESS)
    elif not i.fully_graded:
        failures.append(Code.NOT_FULLY_GRADED)
    # (c) BR-010
    if i.released is None:
        failures.append(Code.UNREADABLE)
    elif not i.released:
        failures.append(Code.NOT_RELEASED)
    # (d) effective close (BR-007, REQ-091)
    close_code, gate_at, shown = _effective_gate(i)
    if close_code is not Code.OK:
        failures.append(close_code)
    # (e) the attempt is the requester's own
    if i.owner_verified is None:
        failures.append(Code.UNREADABLE)
    elif not i.owner_verified:
        failures.append(Code.NOT_OWNER)

    code = failures[0] if failures else Code.OK
    state = _STATE[code]
    if code is Code.OK and i.closed_sessions and i.reset_grants >= i.closed_sessions:
        state = ButtonState.RESET_GRANTED
    return GateDecision(
        code=code,
        state=state,
        gate_at=gate_at,
        opens_at=shown if code is Code.CLOSE_NOT_PASSED else None,
        session_no_next=i.closed_sessions + 1,
        reasons=tuple(dict.fromkeys(failures)),
    )
