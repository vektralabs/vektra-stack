"""BR-013 / REQ-077 rules, one test per boundary (plan assess-teacher-ops 11a)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from vektra_assess.gates import Code, GateInputs, evaluate_start_gates
from vektra_assess.teacher import validation as v
from vektra_assess.teacher.schemas import FieldError, field_errors

UNI_DOMAINS = frozenset({"uni.example"})
NOW = 2_000_000_000
CLOSE_AT = NOW - 3600  # the activity closed an hour ago


def gate_inputs(kind: str, **over: Any) -> GateInputs:
    """Start-gate inputs passing every gate but the close (BR-006 (d)) under test."""
    return GateInputs(
        now=NOW,
        activity_kind=kind,  # type: ignore[arg-type]
        enabled=True,
        team_submission=False,
        grading_method="rubric",
        attempt_in_progress=False,
        has_submitted_attempt=True,
        regrade_in_progress=False,
        fully_graded=True,
        released=True,
        owner_verified=True,
        enrolled=True,
        privacy_configured=True,
        **over,
    )


def codes(errors: list[FieldError]) -> list[tuple[str, str]]:
    return [(e.field, e.code) for e in errors]


# --- text: 20,000 / 20,001, counted in code points after NFC and control removal ----------------


def test_text_boundary_20000_accepted_20001_refused_and_named() -> None:
    ok, errs = v.check_text("solution", "a" * 20_000)
    assert errs == [] and ok == "a" * 20_000
    _, errs = v.check_text("solution", "a" * 20_001)
    assert field_errors(errs) == [
        {"field": "solution", "code": "too_long", "range": "0-20000"}
    ]
    _, errs = v.check_text("pasted_rubric", "b" * 20_001)
    assert errs[0].field == "pasted_rubric"


def test_text_is_counted_after_nfc_and_control_removal() -> None:
    decomposed = "é"  # e + combining acute: one code point after NFC
    assert v.text_length(decomposed * 20_000) == 20_000
    assert v.check_text("solution", decomposed * 20_000)[1] == []
    # control characters do not count (and are removed), newline and tab do
    assert v.normalize_text("a\x00b\x07c​d") == "abcd"
    assert v.normalize_text("riga 1\r\nriga\t2\rfine") == "riga 1\nriga\t2\nfine"
    padded = "a" * 20_000 + "\x00" * 50
    assert v.check_text("solution", padded)[1] == []


def test_blank_text_counts_as_cleared() -> None:
    assert v.check_text("solution", " \n\t\x00 ") == (None, [])


# --- cap: 4 / 5 / 120 / 121 -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cap", "ok"), [(4, False), (5, True), (120, True), (121, False)]
)
def test_cap_boundaries(cap: int, ok: bool) -> None:
    errs = v.check_cap(cap)
    assert (errs == []) is ok
    if not ok:
        assert field_errors(errs) == [
            {"field": "cap_minutes", "code": "out_of_range", "range": "5-120"}
        ]


# --- extra time: total 240 / 241, +100 % / +101 % confirmation ---------------------------------


def test_total_duration_240_accepted_241_refused() -> None:
    assert v.check_extra_time(120, minutes=120) == []
    assert codes(v.check_extra_time(120, minutes=121)) == [
        ("extra_time", "total_too_long")
    ]
    assert v.check_extra_time(120, minutes=121)[0].range == "<= 240 min"
    assert v.check_extra_time(120, percent=100) == []
    assert codes(v.check_extra_time(120, percent=101)) == [
        ("extra_time", "total_too_long")
    ]


def test_over_100_percent_needs_confirmation() -> None:
    assert v.check_extra_time(30, percent=100) == []
    assert codes(v.check_extra_time(30, percent=101)) == [
        ("extra_time", "confirm_over_100")
    ]
    assert v.check_extra_time(30, percent=101, confirm_over_100=True) == []
    # the same rule in minutes: +30 on a 30-minute cap is +100 %, +31 is over
    assert v.check_extra_time(30, minutes=30) == []
    assert codes(v.check_extra_time(30, minutes=31)) == [
        ("extra_time", "confirm_over_100")
    ]


def test_extra_time_ranges_are_named() -> None:
    assert field_errors(v.check_extra_time(30, percent=201)) == [
        {"field": "extra_time", "code": "out_of_range", "range": "0-200%"}
    ]
    assert v.check_extra_time(5, minutes=241)[0].range == "0-240 min"
    assert codes(v.check_extra_time(30)) == [("extra_time", "minutes_or_percent")]
    assert codes(v.check_extra_time(30, minutes=1, percent=1)) == [
        ("extra_time", "minutes_or_percent")
    ]


# --- pasted rubric, rubric source, RAG -------------------------------------------------------


def test_empty_pasted_rubric_with_source_pasted_is_refused() -> None:
    assert field_errors(v.check_pasted_rubric("pasted", False)) == [
        {"field": "pasted_rubric", "code": "empty", "range": "1-20000"}
    ]
    assert v.check_pasted_rubric("pasted", True) == []
    assert v.check_pasted_rubric("none", False) == []


def test_rubric_source_per_activity_type() -> None:
    assert v.check_rubric_source("lms", "assign") == []
    assert codes(v.check_rubric_source("lms", "quiz")) == [
        ("rubric_source", "not_for_activity_type")
    ]
    assert v.check_rubric_source("pasted", "quiz") == []
    # ADR-0033 D5 (OD-3): the LMS's rubric is "lms"; the old product name is no source, with
    # no alias (no plugin sends it yet)
    assert v.check_rubric_source("moodle", "assign") != []


def test_rag_on_is_a_field_error_with_its_reason() -> None:
    assert v.check_rag(False) == []
    assert field_errors(v.check_rag(True)) == [
        {
            "field": "rag_enabled",
            "code": "rag_unavailable",
            "reason": "operator_flag_off",
        }
    ]


# --- end date after the effective opening -----------------------------------------------------


def _at(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


def test_end_date_must_follow_the_effective_opening() -> None:
    opening = v.effective_opening(1_000, 2_000)
    assert opening == 2_000  # the later of the activity close and the attested date
    assert codes(v.check_end_date(_at(2_000), opening)) == [
        ("available_until", "not_after_opening")
    ]
    assert codes(v.check_end_date(_at(1_999), opening)) == [
        ("available_until", "not_after_opening")
    ]
    assert v.check_end_date(_at(2_001), opening) == []
    assert codes(v.check_end_date(_at(5_000), None)) == [
        ("available_until", "opening_unknown")
    ]
    assert codes(v.check_end_date(datetime(2030, 1, 1), opening)) == [
        ("available_until", "timezone_required")
    ]
    # an activity with no close configured (0) and no attestation has no opening
    assert v.effective_opening(0, None) is None


# --- recipients --------------------------------------------------------------------------------


def test_recipients_are_validated_deduped_and_capped() -> None:
    got, errs = v.check_recipients(
        ["Prof.Rossi@UNI.example", "prof.rossi@uni.example", "m.bianchi@uni.example"],
        UNI_DOMAINS,
    )
    assert errs == [] and got == ("Prof.Rossi@uni.example", "m.bianchi@uni.example")
    _, errs = v.check_recipients(
        ["a@uni.example", "b@uni.example", "c@uni.example", "d@uni.example"],
        UNI_DOMAINS,
    )
    assert field_errors(errs) == [
        {"field": "transcript_recipients", "code": "too_many", "range": "1-3"}
    ]
    _, errs = v.check_recipients([], UNI_DOMAINS)
    assert codes(errs) == [("transcript_recipients", "empty")]


def test_a_recipient_outside_the_allowlist_names_the_allowed_domains() -> None:
    _, errs = v.check_recipients(
        ["a@uni.example", "x@gmail.com"], frozenset({"uni.example", "b.it"})
    )
    assert field_errors(errs) == [
        {
            "field": "transcript_recipients",
            "code": "domain_not_allowed",
            "index": 1,
            "allowed_domains": ["b.it", "uni.example"],
        }
    ]
    assert "gmail" not in str(field_errors(errs))  # the address never comes back
    # an empty allowlist refuses every address
    _, errs = v.check_recipients(["a@uni.example"], frozenset())
    assert (
        errs[0].code == "domain_not_allowed" and errs[0].extra["allowed_domains"] == []
    )
    _, errs = v.check_recipients(
        ["Name <a@uni.example>", "a b@uni.example"], UNI_DOMAINS
    )
    assert codes(errs) == [("transcript_recipients", "invalid_address")] * 2


# --- enable refusals (REQ-077) -------------------------------------------------------------------

GOOD = v.Proposed(
    enabled=True, cap_minutes=30, rubric_source="lms", pasted_rubric_set=False,
    recipients_count=1,
)  # fmt: skip
FACTS = v.ActivityFacts(
    "assign", close_at=1_900_000_000, team_submission=False, grading_method="rubric",
    lms_rubric=True,
)  # fmt: skip


def refusals(
    p: v.Proposed = GOOD, f: v.ActivityFacts = FACTS, **kw: bool
) -> list[tuple[str, str]]:
    status = {"privacy_configured": True, "setup_green": True, **kw}
    return codes(v.enable_refusals(p, f, **status))


def test_a_complete_activity_may_be_enabled_and_disable_is_never_refused() -> None:
    assert refusals() == []
    off = v.Proposed(False, None, "pasted", False, 0)
    assert refusals(off, v.ActivityFacts("quiz"), privacy_configured=False) == []


@pytest.mark.parametrize(
    ("facts", "want"),
    [
        (v.ActivityFacts("quiz", close_at=0), "quiz_without_close_date"),
        (
            v.ActivityFacts("assign", 0, False, "rubric", True),
            "assignment_without_cutoff",
        ),
        (v.ActivityFacts("assign", 1, True, "rubric", True), "team_submission"),
        (v.ActivityFacts("assign", 1, False, "guide", False), "marking_guide"),
        (v.ActivityFacts("assign", None, None, None, None), "lms_facts_unreadable"),
    ],
)
def test_enable_is_refused_for_each_moodle_fact(
    facts: v.ActivityFacts, want: str
) -> None:
    p = (
        GOOD
        if facts.activity_type == "assign"
        else v.Proposed(True, 30, "none", False, 1)
    )
    got = refusals(p, facts)
    assert ("enabled", want) in got


def test_enable_is_refused_for_each_configuration_gap() -> None:
    assert ("enabled", "privacy_unset") in refusals(privacy_configured=False)
    assert ("enabled", "setup_check_red") in refusals(setup_green=False)
    no_rubric = v.ActivityFacts("assign", 1, False, "simple", False)
    assert refusals(GOOD, no_rubric) == [("rubric_source", "lms_rubric_missing")]
    assert refusals(v.Proposed(True, None, "none", False, 1)) == [
        ("cap_minutes", "required")
    ]
    assert refusals(v.Proposed(True, 30, "none", False, 0)) == [
        ("transcript_recipients", "empty")
    ]


def test_a_save_of_an_enabled_activity_rechecks_only_what_it_can_break() -> None:
    """Already enabled, not switching on: Moodle facts, privacy and setup are not re-gated here
    (the Start gates re-check the facts), the configuration gaps still are."""
    stale_facts = v.ActivityFacts("assign", 0, True, "guide", True)
    kw = {"privacy_configured": False, "setup_green": False, "turning_on": False}
    assert codes(v.enable_refusals(GOOD, stale_facts, **kw)) == []
    assert codes(
        v.enable_refusals(v.Proposed(True, 30, "lms", False, 0), stale_facts, **kw)
    ) == [("transcript_recipients", "empty")]


def test_null_on_an_always_valued_setting_is_named() -> None:
    assert field_errors(v.check_not_null(["enabled", "show_solution"])) == [
        {"field": "enabled", "code": "null_not_allowed"},
        {"field": "show_solution", "code": "null_not_allowed"},
    ]


@pytest.mark.parametrize("kind", ["quiz", "assign"])
@pytest.mark.parametrize("stored", [None, "none", "pasted", "lms", "moodle", 3])
def test_the_api_and_the_capture_read_the_same_rubric_source(
    kind: str, stored: object
) -> None:
    """Review 9: one function decides the effective rubric source, for the config view and for
    the snapshot capture; a quiz never reads "lms" (REQ-077); the old value "moodle" is no source any more."""
    from vektra_assess.worker import start_source

    flags = {} if stored is None else {"rubric_source": stored}
    want = (
        stored if stored in v.SOURCES[kind] else ("lms" if kind == "assign" else "none")
    )
    assert v.rubric_source_of(flags, kind) == want
    view = start_source.teacher_view(flags, kind, None, "griglia incollata")
    assert view.rubric_source == want
    # CodeRabbit nitpick (PR 170): the pasted rubric reaches the capture view only with the
    # source "pasted" (the capture copies TeacherConfigView.pasted_rubric into the snapshot)
    assert view.pasted_rubric == ("griglia incollata" if want == "pasted" else None)


def test_the_end_date_is_compared_at_the_second_it_is_stored() -> None:
    """CodeRabbit 4505511242: the end date is stored in whole seconds; 0.5 s after the opening
    truncates TO the opening and must be refused."""
    assert codes(v.check_end_date(datetime.fromtimestamp(2_000.5, UTC), 2_000)) == [
        ("available_until", "not_after_opening")
    ]
    assert v.check_end_date(datetime.fromtimestamp(2_001.0, UTC), 2_000) == []


def test_an_end_date_outside_the_representable_range_is_a_field_error() -> None:
    """CodeRabbit 4505511246: 9999-12-31T23:30-01:00 is year 10000 in UTC; it must be a field
    error, not a failure when the stored value is rebuilt."""
    from datetime import timedelta, timezone

    late = datetime(9999, 12, 31, 23, 30, tzinfo=timezone(timedelta(hours=-1)))
    assert codes(v.check_end_date(late, 2_000)) == [("available_until", "out_of_range")]


# --- 11b: reasons, reset, extra time stored, opening fallback ---------------------------------


def test_reason_is_required_and_bounded_at_1000_code_points() -> None:
    for blank in ("", "   ", "\x07\n\t"):
        _, errs = v.check_reason(blank)
        assert field_errors(errs) == [
            {"field": "reason", "code": "required", "range": "1-1000"}
        ]
    ok, errs = v.check_reason(" " + "è" * 1000 + " ")
    assert errs == [] and ok == "è" * 1000
    _, errs = v.check_reason("è" * 1001)  # NFC: 1001 code points after composing
    assert codes(errs) == [("reason", "too_long")]


@pytest.mark.parametrize(
    ("close", "requested", "expected"),
    [
        ("service_failure", None, True),  # REQ-087 (b): auto ON
        ("finished", None, False),  # default OFF
        ("timed_out", True, True),  # the teacher's opt-in
        ("service_failure", False, False),  # an explicit choice is kept
    ],
)
def test_previous_turns_default_follows_the_close_reason(
    close: str, requested: bool | None, expected: bool
) -> None:
    assert v.include_previous_turns(close, requested) is expected


def _gate(**over: object) -> str | None:
    args: dict[str, object] = {
        "now": 1_000,
        "enabled": True,
        "cap_set": True,
        "end_at": None,
        "privacy_configured": True,
        "lms_reasons": [],
        **over,
    }
    return v.reset_gate(**args)  # type: ignore[arg-type]


def test_reset_gate_rechecks_the_database_half_live_and_the_moodle_half_as_rendered() -> (
    None
):
    assert _gate() is None
    assert _gate(enabled=False) == "not_enabled"
    assert _gate(cap_set=False) == "not_enabled"
    assert _gate(end_at=1_000) == "end_date_passed"  # at the end date: refused
    assert _gate(end_at=1_001) is None  # the date moved later: accepted
    assert _gate(privacy_configured=False) == "privacy_not_configured"
    assert _gate(lms_reasons=["not_released"]) == "not_released"
    # the session gate is what the reset lifts: a stored "already discussed" is not a refusal
    assert _gate(lms_reasons=["already_discussed", "session_open"]) is None
    assert _gate(lms_reasons=["bogus"]) == "unreadable"  # fail closed
    # order: the end date before any Moodle reason
    assert _gate(end_at=10, lms_reasons=["not_released"]) == "end_date_passed"


def test_extra_time_is_stored_in_seconds_of_the_current_cap() -> None:
    assert v.extra_seconds(30, percent=50) == 15 * 60  # +50 % of 30 min -> 45 min total
    assert v.extra_seconds(30, minutes=20) == 20 * 60


@pytest.mark.parametrize(
    ("kind", "readable", "needed"),
    [
        ("quiz", {"quiz_overrides": True, "quiz_grace": True}, False),
        ("quiz", {"quiz_overrides": True, "quiz_grace": False}, True),  # partial grace
        ("quiz", {"quiz_overrides": False, "quiz_grace": True}, True),  # Moodle 4.3
        ("quiz", {}, True),  # not probed
        # assignment extensions / overrides of all participants are never listable
        ("assign", {"quiz_overrides": True, "quiz_grace": True}, True),
    ],
)
def test_fallback_is_needed_only_when_a_close_input_is_unreadable(
    kind: str, readable: dict[str, bool], needed: bool
) -> None:
    assert v.fallback_needed(kind, readable) is needed


CLOSE = 1_900_000_000


def _fallback(at: int, **over: object) -> list[tuple[str, str]]:
    args: dict[str, object] = {
        "attestation": True,
        "needed": True,
        "close_at": CLOSE,
        "current_at": None,
        "started": False,
        **over,
    }
    return codes(
        v.check_opening_fallback(datetime.fromtimestamp(at, UTC), **args)  # type: ignore[arg-type]
    )


def test_opening_fallback_bounds_attestation_and_later_only() -> None:
    assert _fallback(CLOSE) == []  # >= the close
    assert _fallback(CLOSE - 1) == [("opening_at", "before_close")]
    assert _fallback(CLOSE, attestation=False) == [("attestation", "required")]
    assert _fallback(CLOSE, needed=False) == [("opening_at", "fallback_not_needed")]
    assert _fallback(CLOSE, close_at=0) == [("opening_at", "no_close_date")]  # BR-007
    assert _fallback(CLOSE, close_at=None) == [("opening_at", "close_unreadable")]
    # before any session started, an earlier move is allowed; after, only later
    assert _fallback(CLOSE + 10, current_at=CLOSE + 20) == []
    assert _fallback(CLOSE + 10, current_at=CLOSE + 20, started=True) == [
        ("opening_at", "only_later")
    ]
    assert _fallback(CLOSE + 20, current_at=CLOSE + 20, started=True) == [
        ("opening_at", "only_later")
    ]
    assert _fallback(CLOSE + 21, current_at=CLOSE + 20, started=True) == []
    # review 11b-2: the end date must stay after the opening (the rule of check_end_date)
    assert _fallback(CLOSE + 10, end_at=CLOSE + 11) == []
    assert _fallback(CLOSE + 10, end_at=CLOSE + 10) == [
        ("opening_at", "not_before_end")
    ]
    assert _fallback(CLOSE + 10, end_at=CLOSE + 5) == [("opening_at", "not_before_end")]
    naive = v.check_opening_fallback(
        datetime(2030, 1, 1),
        attestation=True,
        needed=True,
        close_at=CLOSE,
        current_at=None,
        started=False,
    )
    assert codes(naive) == [("opening_at", "timezone_required")]


@pytest.mark.parametrize(
    ("close", "automatic", "grace", "attested", "expected"),
    [
        (None, None, None, None, (None, "unknown", False)),
        (0, None, None, None, (None, "unknown", False)),  # no close, no teacher date
        # BR-007: a stale teacher date never replaces a missing close
        (0, None, None, 500, (None, "unknown", False)),
        # every close input readable (the automatic close): no teacher date needed
        (100, 150, 0, None, (150, "activity_close", False)),
        # a tie: the teacher's date decides
        (100, 150, 0, 150, (150, "attested", False)),
        (100, 150, 0, 120, (150, "activity_close", False)),
        (100, 150, 0, 200, (200, "attested", False)),
        # some close input unreadable (an assignment, or a quiz's overrides / grace): the
        # REQ-091 fallback; without a teacher date Start refuses close_unreadable
        (100, None, None, None, (None, "unreadable", True)),
        (100, None, 30, None, (None, "unreadable", True)),
        (100, None, None, 100, (100, "attested", False)),
        (100, None, None, 200, (200, "attested", False)),
        (100, None, None, 50, (100, "activity_close", False)),
        (100, None, 30, 110, (130, "activity_close", False)),  # the grace decides
        # the activity's own close unreadable: no date can be computed, a teacher date
        # cannot help (the PUT refuses it with close_unreadable)
        (None, None, None, 70, (None, "unreadable", False)),
    ],
)
def test_one_opening_rule_for_the_config_view_and_the_gate_panel(
    close: int | None,
    automatic: int | None,
    grace: int | None,
    attested: int | None,
    expected: tuple[int | None, str, bool],
) -> None:
    """Review 11b-8: the config view and the session list's gate panel share it; and it is
    the Start gate's own rule (``gates.effective_close``), so the view never says
    ``activity_close`` while Start refuses ``close_unreadable``."""
    view = v.opening_view(close, attested, automatic_close_at=automatic, grace_s=grace)
    assert (view.at, view.source, view.fallback_required) == expected


@pytest.mark.parametrize("kind", ["quiz", "assign"])
@pytest.mark.parametrize("participants", [(), (CLOSE_AT + 600,), None])
@pytest.mark.parametrize("grace", [0, 120, None])
@pytest.mark.parametrize(
    "attested", [None, CLOSE_AT - 60, CLOSE_AT, CLOSE_AT + 300, CLOSE_AT + 900]
)
@pytest.mark.parametrize("close", [CLOSE_AT, 0, None])
def test_the_view_agrees_with_the_start_gate(
    kind: str,
    participants: tuple[int, ...] | None,
    grace: int | None,
    attested: int | None,
    close: int | None,
) -> None:
    """The plugin-team report: GET config said ``activity_close`` while ``/state`` said
    ``not_open/close_unreadable``. Over every combination of the close inputs the view's date is
    the gate's ``gate_at``, and ``unreadable`` / ``fallback_required`` follow its refusal."""
    if kind == "assign":
        grace, participants = 0, None  # extensions are never listable (OQ-025)
    inputs = gate_inputs(
        kind,
        activity_close_at=close,
        grace_s=grace,
        participant_closes=participants,
        teacher_opening_at=attested,
    )
    decision = evaluate_start_gates(inputs)
    # the teacher facts as the worker writes them (start_source.teacher_facts)
    automatic = (
        max([close, *participants]) + grace
        if close and participants is not None and grace is not None
        else None
    )
    view = v.opening_view(close, attested, automatic_close_at=automatic, grace_s=grace)
    assert view.at == decision.gate_at
    refused = Code.CLOSE_UNREADABLE in decision.reasons
    # "unknown" stays "neither known": no readable close and no teacher date
    assert (view.source == "unreadable") == (
        refused and (close is not None or attested is not None)
    )
    assert view.fallback_required == (refused and close is not None)
    if not refused and Code.NO_CLOSE_DATE not in decision.reasons:
        assert view.source in ("attested", "activity_close")


def test_a_percentage_is_whole_minutes_rounded_up() -> None:
    """CodeRabbit 4506916942: the session lasts whole minutes, so the pre-session screen's
    ``duration_min`` is exact: +25 % of 30 min is 8 min (7.5 rounded up, never cut short)."""
    assert v.extra_seconds(30, percent=25) == 8 * 60
    assert v.extra_seconds(30, percent=50) == 15 * 60
    assert v.extra_seconds(7, percent=10) == 1 * 60  # 0.7 min -> 1
    assert v.extra_seconds(30, percent=0) == 0
    # the total rule sees the same rounding: 115 + 5 % (5.75 -> 6) = 121 > 120 is checked
    # against 240 like any other total
    assert v.check_extra_time(120, percent=100, confirm_over_100=False) == []
    assert codes(v.check_extra_time(120, percent=101, confirm_over_100=True)) == [
        ("extra_time", "total_too_long")
    ]
    # 119 min + 102 % = 121.38 min, rounded UP to 122: total 241 > 240 (rounding down
    # would have accepted 240)
    assert codes(v.check_extra_time(119, percent=102, confirm_over_100=True)) == [
        ("extra_time", "total_too_long")
    ]
