"""BR-013 and REQ-077 rules as pure functions (plan assess-teacher-ops, validation in Vektra).

The plugin's form is a convenience; these rules are the authority. Every function takes plain
values (the request, the current configuration, the worker-written LMS facts, the privacy and
setup-check status) and returns ``FieldError`` items: codes and bounds, never the input.

Text (solution, pasted rubric): normalised to NFC, control characters removed except ``\\n`` and
``\\t``, then counted in code points (the plugin's live counter must use the same rule); at most
``MAX_TEXT_CHARS``. Text that is empty after normalisation (only whitespace) counts as cleared.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from vektra_assess.delivery.address import normalize_address
from vektra_assess.delivery.recipients import MAX_RECIPIENTS, domain_allowed
from vektra_assess.gates import DB_SIDE, Code, Opening, decide_opening
from vektra_assess.teacher.schemas import FieldError

MAX_TEXT_CHARS = 20_000
CAP_MIN, CAP_MAX = 5, 120
MAX_TOTAL_MINUTES = 240
MAX_EXTRA_PERCENT = 200
MAX_EXTRA_MINUTES = 240
CONFIRM_ABOVE_PERCENT = 100
MAX_REASON_CHARS = 1000

TEXT_RANGE = f"0-{MAX_TEXT_CHARS}"
RUBRIC_RANGE = f"1-{MAX_TEXT_CHARS}"
CAP_RANGE = f"{CAP_MIN}-{CAP_MAX}"
RECIPIENTS_RANGE = f"1-{MAX_RECIPIENTS}"
REASON_RANGE = f"1-{MAX_REASON_CHARS}"

#: rubric sources a type may use (REQ-077: LMS rubric for assignments, pasted text for quizzes)
SOURCES = {"quiz": ("none", "pasted"), "assign": ("none", "pasted", "lms")}


# --- text ------------------------------------------------------------------------------------


def normalize_text(value: str) -> str:
    """NFC, then every control character (Unicode category Cc / Cf) removed except ``\\n`` and
    ``\\t``; ``\\r\\n`` and ``\\r`` become ``\\n`` first."""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = unicodedata.normalize("NFC", value)
    return "".join(
        ch
        for ch in value
        if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf", "Cs")
    )


def text_length(value: str) -> int:
    """Code points after ``normalize_text``: the rule the plugin's counter mirrors."""
    return len(normalize_text(value))


def check_text(field: str, value: str) -> tuple[str | None, list[FieldError]]:
    """(normalised text or None when empty, errors)."""
    norm = normalize_text(value)
    if len(norm) > MAX_TEXT_CHARS:
        return None, [FieldError(field, "too_long", TEXT_RANGE)]
    return (norm if norm.strip() else None), []


# --- scalars ---------------------------------------------------------------------------------


def check_cap(value: int) -> list[FieldError]:
    if not CAP_MIN <= value <= CAP_MAX:
        return [FieldError("cap_minutes", "out_of_range", CAP_RANGE)]
    return []


def check_not_null(names: Iterable[str]) -> list[FieldError]:
    """``null`` on a setting that always has a value (``enabled``, ``show_solution``...): an
    error, not a clear and not "unchanged" (absent is how a form leaves it unchanged)."""
    return [FieldError(name, "null_not_allowed") for name in names]


def check_rag(value: bool) -> list[FieldError]:
    """ARCH-072: RAG ships locked OFF; ON is refused with the reason (the operator flag is off,
    and refused at startup when set)."""
    if value:
        return [
            FieldError(
                "rag_enabled", "rag_unavailable", extra={"reason": "operator_flag_off"}
            )
        ]
    return []


def default_rubric_source(activity_type: str) -> str:
    return "lms" if activity_type == "assign" else "none"


def rubric_source_of(flags: Mapping[str, Any], activity_type: str) -> str:
    """The effective rubric source of a stored config (``flags.rubric_source``): the stored one
    when this activity type may use it, else the type's default. The ONE rule the config view
    and the snapshot capture share (a quiz never reads "lms")."""
    source = flags.get("rubric_source")
    if isinstance(source, str) and source in SOURCES.get(activity_type, ()):
        return source
    return default_rubric_source(activity_type)


def check_rubric_source(source: str, activity_type: str) -> list[FieldError]:
    allowed = SOURCES.get(activity_type, ())
    if source not in allowed:
        return [
            FieldError(
                "rubric_source",
                "not_for_activity_type",
                extra={"allowed": list(allowed)},
            )
        ]
    return []


def epoch_fact(facts: Mapping[str, Any], name: str) -> int | None:
    """An epoch fact of the worker-written teacher facts, or None when absent or mistyped."""
    value = facts.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def opening_of(
    facts: Mapping[str, Any],
    attested_at: int | None,
    readable: Mapping[str, bool],
) -> Opening:
    """The opening of an activity as every teacher route sees it: ``gates.decide_opening``
    (the Start gate's own rule) on the worker's teacher facts (``close_at``,
    ``automatic_close_at``, ``grace_s``) merged with the capability matrix ``readable``
    (``readable_close_flags``). When the matrix does not hold every close input readable
    (``fallback_needed``: an assignment always, a quiz with ``quiz_overrides`` or
    ``quiz_grace`` not a fresh ``readable``) the automatic close is not used even when the
    last facts read got it (fail closed, partial counts as unreadable): Start's own live read
    is not expected to give it, so a teacher date is what opens the activity. The grace the
    last facts read got is still added on that path, as Start adds the grace it reads. The
    config view and its end-date rule, the session list's gate panel and PUT opening-fallback
    all call this, once (unresolved facts never get here: ``verify_teacher`` refuses them)."""
    kind = str(facts.get("activity_type"))
    automatic = epoch_fact(facts, "automatic_close_at")
    if fallback_needed(kind, readable):
        automatic = None
    return decide_opening(
        epoch_fact(facts, "close_at"),
        grace_s=epoch_fact(facts, "grace_s"),
        automatic_close_at=automatic,
        teacher_opening_at=attested_at,
    )


def check_end_date(end_at: datetime, opening_at: int | None) -> list[FieldError]:
    if end_at.tzinfo is None:
        return [FieldError("available_until", "timezone_required")]
    try:  # the value is stored as whole UTC seconds and rebuilt from them
        stored = int(end_at.timestamp())
        datetime.fromtimestamp(stored, UTC)
    except (OverflowError, ValueError, OSError):
        return [FieldError("available_until", "out_of_range")]
    if opening_at is None:
        return [FieldError("available_until", "opening_unknown")]
    # compared at the whole second it is stored at (``end_at`` is saved truncated)
    if stored <= opening_at:
        return [
            FieldError(
                "available_until", "not_after_opening", extra={"opening_at": opening_at}
            )
        ]
    return []


def check_pasted_rubric(
    rubric_source: str, pasted_rubric_set: bool
) -> list[FieldError]:
    """BR-013: the source "pasted" with an empty pasted rubric is an error, enabled or not."""
    if rubric_source == "pasted" and not pasted_rubric_set:
        return [FieldError("pasted_rubric", "empty", RUBRIC_RANGE)]
    return []


# --- recipients (REQ-082 as amended 2026-11-07) ----------------------------------------------


def check_recipients(
    raw: Sequence[str], domains: frozenset[str]
) -> tuple[tuple[str, ...], list[FieldError]]:
    """1..3 addresses, each in Vektra's addr-spec subset and in the allowlist (empty refuses all);
    duplicates (case-insensitive) collapse to the first. Errors name the position, never the
    address, and list the allowed domains."""
    out: list[str] = []
    seen: set[str] = set()
    errors: list[FieldError] = []
    allowed = sorted(domains)
    for i, item in enumerate(raw):
        address = normalize_address(item)
        if address is None:
            errors.append(
                FieldError(
                    "transcript_recipients", "invalid_address", extra={"index": i}
                )
            )
            continue
        if not domain_allowed(address, domains):
            errors.append(
                FieldError(
                    "transcript_recipients",
                    "domain_not_allowed",
                    extra={"index": i, "allowed_domains": allowed},
                )
            )
            continue
        if address.lower() not in seen:
            seen.add(address.lower())
            out.append(address)
    if not errors:
        if not out:
            errors.append(
                FieldError("transcript_recipients", "empty", RECIPIENTS_RANGE)
            )
        elif len(out) > MAX_RECIPIENTS:
            errors.append(
                FieldError("transcript_recipients", "too_many", RECIPIENTS_RANGE)
            )
    return tuple(out), errors


# --- extra time (BR-013 / REQ-078) -----------------------------------------------------------


def check_extra_time(
    cap_minutes: int,
    *,
    minutes: int | None = None,
    percent: int | None = None,
    confirm_over_100: bool = False,
) -> list[FieldError]:
    """0-200 % or 0-240 min, but the TOTAL duration of at most 240 min prevails; above +100 % (of
    the cap) needs an explicit confirmation."""
    if (minutes is None) == (percent is None):
        return [FieldError("extra_time", "minutes_or_percent")]
    if percent is not None:
        if not 0 <= percent <= MAX_EXTRA_PERCENT:
            return [FieldError("extra_time", "out_of_range", f"0-{MAX_EXTRA_PERCENT}%")]
        extra_s = _percent_minutes(cap_minutes, percent) * 60
        pct_s = percent * 100
    else:
        assert minutes is not None
        if not 0 <= minutes <= MAX_EXTRA_MINUTES:
            return [
                FieldError("extra_time", "out_of_range", f"0-{MAX_EXTRA_MINUTES} min")
            ]
        extra_s = minutes * 60
        pct_s = (
            minutes * 10_000 // cap_minutes
        )  # percent x 100, exact enough to compare
    if cap_minutes * 60 + extra_s > MAX_TOTAL_MINUTES * 60:
        return [
            FieldError("extra_time", "total_too_long", f"<= {MAX_TOTAL_MINUTES} min")
        ]
    if pct_s > CONFIRM_ABOVE_PERCENT * 100 and not confirm_over_100:
        return [
            FieldError("extra_time", "confirm_over_100", f"<= {CONFIRM_ABOVE_PERCENT}%")
        ]
    return []


def extra_seconds(
    cap_minutes: int, *, minutes: int | None = None, percent: int | None = None
) -> int:
    """The stored extra time, in seconds, of a request ``check_extra_time`` accepted: a
    percentage is of the cap configured NOW (a later cap change does not rescale it), in whole
    minutes rounded up (a session lasts whole minutes: the pre-session screen's
    ``duration_min`` is exact, and the student is never cut short)."""
    if percent is not None:
        return _percent_minutes(cap_minutes, percent) * 60
    return int(minutes or 0) * 60


def _percent_minutes(cap_minutes: int, percent: int) -> int:
    return -(-cap_minutes * percent // 100)  # ceil, in integers


# --- teacher reasons (REQ-087 reset, REQ-081 terminate) ---------------------------------------


def check_reason(value: str) -> tuple[str | None, list[FieldError]]:
    """Mandatory, 1..1000 code points after ``normalize_text`` and trimming; the text itself is
    never echoed in an error."""
    norm = normalize_text(value).strip()
    if not norm:
        return None, [FieldError("reason", "required", REASON_RANGE)]
    if len(norm) > MAX_REASON_CHARS:
        return None, [FieldError("reason", "too_long", REASON_RANGE)]
    return norm, []


# --- reset (REQ-087) ---------------------------------------------------------------------------


def include_previous_turns(close_reason: str | None, requested: bool | None) -> bool:
    """REQ-087 (b): earlier turns are included by default only after a ``service_failure``
    close; otherwise only when the teacher ticks the box (default OFF). An explicit choice is
    kept either way. Whether they reach the model is decided at Start by the BR-011 boundary
    rule (REQ-087 (c)), never here."""
    if requested is not None:
        return requested
    return close_reason == "service_failure"


def reset_gate(
    *,
    now: int,
    enabled: bool,
    cap_set: bool,
    end_at: int | None,
    privacy_configured: bool,
    lms_reasons: Sequence[str],
) -> str | None:
    """The START gates a reset re-runs (REQ-087), in the evaluator's order: the ones Vektra
    decides from its own rows, checked live (the activity enabled and configured, the end date,
    privacy), then the LMS-side failures of the student's fresh render evaluation. The
    session gate (g) is the reset's own precondition (the latest session CLOSED) and the very
    thing a reset lifts. Returns the first refusing code, or None."""
    if not enabled or not cap_set:
        return Code.NOT_ENABLED.value
    if end_at is not None and now >= end_at:
        return Code.END_DATE_PASSED.value
    if not privacy_configured:
        return Code.PRIVACY_NOT_CONFIGURED.value
    for raw in lms_reasons:
        try:
            code = Code(raw)
        except ValueError:
            return Code.UNREADABLE.value  # an unknown stored code is not a pass
        if code not in DB_SIDE and code is not Code.OK:
            return code.value
    return None


# --- opening fallback (REQ-091) ---------------------------------------------------------------

#: the BR-006(d) inputs of a quiz the matrix must mark readable for the automatic close; an
#: assignment's participant extensions and overrides are never listable (OQ-025), so for an
#: assignment the fallback always applies (as the Start gate does)
QUIZ_CLOSE_FLAGS = ("quiz_overrides", "quiz_grace")


def fallback_needed(activity_type: str, readable: Mapping[str, bool]) -> bool:
    """REQ-091: the fallback date may be set only when some BR-006(d) input is unreadable
    (partial counts as unreadable). ``readable``: flag -> whether the capability matrix holds a
    fresh ``readable`` row for it on the site's current release."""
    if activity_type != "quiz":
        return True
    return not all(readable.get(flag) is True for flag in QUIZ_CLOSE_FLAGS)


def check_opening_fallback(
    opening_at: datetime,
    *,
    attestation: bool,
    opening: Opening,
    current_at: int | None,
    started: bool,
    end_at: int | None = None,
) -> list[FieldError]:
    """REQ-091: an attestation on every edit; only while the fallback applies; on or after the
    activity's own close / cut-off (the fallback never replaces a missing one, BR-007); once any
    session of the activity started, only a later date; and before the end date ``end_at``
    ("disponibile fino al" must stay after the effective opening, as ``check_end_date`` requires
    when the end date is saved: otherwise no Start could ever happen)."""
    errors: list[FieldError] = []
    close_at = opening.close_at
    if attestation is not True:
        errors.append(FieldError("attestation", "required"))
    if close_at is not None and close_at > 0 and not opening.fallback_applies:
        # every close input is readable: Start opens without a teacher date
        errors.append(FieldError("opening_at", "fallback_not_needed"))
    if opening_at.tzinfo is None:
        return [*errors, FieldError("opening_at", "timezone_required")]
    try:
        at = int(opening_at.timestamp())
        datetime.fromtimestamp(at, UTC)
    except (OverflowError, ValueError, OSError):
        return [*errors, FieldError("opening_at", "out_of_range")]
    if close_at is None:
        errors.append(FieldError("opening_at", "close_unreadable"))
    elif close_at <= 0:
        errors.append(FieldError("opening_at", "no_close_date"))
    elif at < close_at:
        errors.append(
            FieldError("opening_at", "before_close", extra={"close_at": close_at})
        )
    if started and current_at is not None and at <= current_at:
        errors.append(
            FieldError("opening_at", "only_later", extra={"current_at": current_at})
        )
    if end_at is not None and at >= end_at:
        errors.append(
            FieldError(
                "opening_at", "not_before_end", extra={"available_until": end_at}
            )
        )
    return errors


# --- enable (REQ-077) ------------------------------------------------------------------------


@dataclass(frozen=True)
class ActivityFacts:
    """The worker-written facts of ``assess_teacher_gate`` (None: not readable)."""

    activity_type: str
    close_at: int | None = None
    team_submission: bool | None = None
    grading_method: str | None = None
    lms_rubric: bool | None = None


@dataclass(frozen=True)
class Proposed:
    """The configuration as it would be after the PUT (non-secret values and secret states)."""

    enabled: bool
    cap_minutes: int | None
    rubric_source: str
    pasted_rubric_set: bool
    recipients_count: int


def enable_refusals(
    p: Proposed,
    facts: ActivityFacts,
    *,
    privacy_configured: bool,
    setup_green: bool,
    turning_on: bool = True,
) -> list[FieldError]:
    """Why the discussion may NOT be enabled (REQ-077), each a field error on ``enabled`` (or on
    the field to fix). Empty when it may. Unreadable LMS facts refuse (fail closed). The empty
    pasted rubric is ``check_pasted_rubric`` (refused whether enabled or not).

    ``turning_on``: the request itself sets ``enabled=true``. A save of an activity that is
    already enabled re-checks only what that save can break (cap, rubric source, recipients); the
    LMS facts, privacy and setup check are the gate of switching it on (and the Start gates
    re-check the LMS facts at every Start)."""
    if not p.enabled:
        return []
    errs: list[FieldError] = []

    def refuse(code: str, fld: str = "enabled", range_: str | None = None) -> None:
        errs.append(FieldError(fld, code, range_))

    if not turning_on:
        facts = ActivityFacts(facts.activity_type, 1, False, "none", facts.lms_rubric)
        privacy_configured = setup_green = True
    if facts.close_at is None:
        refuse("lms_facts_unreadable")
    elif facts.close_at == 0:  # BR-007
        refuse(
            "quiz_without_close_date"
            if facts.activity_type == "quiz"
            else "assignment_without_cutoff"
        )
    if facts.activity_type == "assign":
        if facts.team_submission is None or facts.grading_method is None:
            if facts.close_at is not None:
                refuse("lms_facts_unreadable")
        elif facts.team_submission:
            refuse("team_submission")
        elif facts.grading_method == "guide":  # EX-023
            refuse("marking_guide")
    if p.rubric_source == "lms" and facts.lms_rubric is not True:
        refuse("lms_rubric_missing", "rubric_source")
    if p.cap_minutes is None:
        refuse("required", "cap_minutes", CAP_RANGE)
    if p.recipients_count < 1:
        refuse("empty", "transcript_recipients", RECIPIENTS_RANGE)
    if not privacy_configured:
        refuse("privacy_unset")
    if not setup_green:
        refuse("setup_check_red")
    return errs
