"""BR-013 and REQ-077 rules as pure functions (plan assess-teacher-ops, validation in Vektra).

The plugin's form is a convenience; these rules are the authority. Every function takes plain
values (the request, the current configuration, the worker-written Moodle facts, the privacy and
setup-check status) and returns ``FieldError`` items: codes and bounds, never the input.

Text (solution, pasted rubric): normalised to NFC, control characters removed except ``\\n`` and
``\\t``, then counted in code points (the plugin's live counter must use the same rule); at most
``MAX_TEXT_CHARS``. Text that is empty after normalisation (only whitespace) counts as cleared.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from vektra_assess.delivery.address import normalize_address
from vektra_assess.delivery.recipients import MAX_RECIPIENTS, domain_allowed
from vektra_assess.teacher.schemas import FieldError

MAX_TEXT_CHARS = 20_000
CAP_MIN, CAP_MAX = 5, 120
MAX_TOTAL_MINUTES = 240
MAX_EXTRA_PERCENT = 200
MAX_EXTRA_MINUTES = 240
CONFIRM_ABOVE_PERCENT = 100

TEXT_RANGE = f"0-{MAX_TEXT_CHARS}"
RUBRIC_RANGE = f"1-{MAX_TEXT_CHARS}"
CAP_RANGE = f"{CAP_MIN}-{CAP_MAX}"
RECIPIENTS_RANGE = f"1-{MAX_RECIPIENTS}"

#: rubric sources a type may use (REQ-077: Moodle rubric for assignments, pasted text for quizzes)
SOURCES = {"quiz": ("none", "pasted"), "assign": ("none", "pasted", "moodle")}


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


def effective_opening(close_at: int | None, attested_at: int | None) -> int | None:
    """The opening the end date must follow (BR-006(d) or REQ-091), epoch seconds: the later of
    the activity's own close (``close_at``, 0 = none configured) and the teacher's attested date.
    None when neither is known."""
    known = [t for t in (close_at or None, attested_at) if t]
    return max(known) if known else None


def check_end_date(end_at: datetime, opening_at: int | None) -> list[FieldError]:
    if end_at.tzinfo is None:
        return [FieldError("available_until", "timezone_required")]
    if opening_at is None:
        return [FieldError("available_until", "opening_unknown")]
    if end_at.timestamp() <= opening_at:
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


# --- extra time (BR-013 / REQ-078; the endpoint arrives with 11b) -----------------------------


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
        extra_s = cap_minutes * 60 * percent // 100
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


# --- enable (REQ-077) ------------------------------------------------------------------------


@dataclass(frozen=True)
class ActivityFacts:
    """The worker-written facts of ``assess_teacher_gate`` (None: not readable)."""

    activity_type: str
    close_at: int | None = None
    team_submission: bool | None = None
    grading_method: str | None = None
    moodle_rubric: bool | None = None


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
    the field to fix). Empty when it may. Unreadable Moodle facts refuse (fail closed). The empty
    pasted rubric is ``check_pasted_rubric`` (refused whether enabled or not).

    ``turning_on``: the request itself sets ``enabled=true``. A save of an activity that is
    already enabled re-checks only what that save can break (cap, rubric source, recipients); the
    Moodle facts, privacy and setup check are the gate of switching it on (and the Start gates
    re-check the Moodle facts at every Start)."""
    if not p.enabled:
        return []
    errs: list[FieldError] = []

    def refuse(code: str, fld: str = "enabled", range_: str | None = None) -> None:
        errs.append(FieldError(fld, code, range_))

    if not turning_on:
        facts = ActivityFacts(
            facts.activity_type, 1, False, "none", facts.moodle_rubric
        )
        privacy_configured = setup_green = True
    if facts.close_at is None:
        refuse("moodle_facts_unreadable")
    elif facts.close_at == 0:  # BR-007
        refuse(
            "quiz_without_close_date"
            if facts.activity_type == "quiz"
            else "assignment_without_cutoff"
        )
    if facts.activity_type == "assign":
        if facts.team_submission is None or facts.grading_method is None:
            if facts.close_at is not None:
                refuse("moodle_facts_unreadable")
        elif facts.team_submission:
            refuse("team_submission")
        elif facts.grading_method == "guide":  # EX-023
            refuse("marking_guide")
    if p.rubric_source == "moodle" and facts.moodle_rubric is not True:
        refuse("moodle_rubric_missing", "rubric_source")
    if p.cap_minutes is None:
        refuse("required", "cap_minutes", CAP_RANGE)
    if p.recipients_count < 1:
        refuse("empty", "transcript_recipients", RECIPIENTS_RANGE)
    if not privacy_configured:
        refuse("privacy_unset")
    if not setup_green:
        refuse("setup_check_red")
    return errs
