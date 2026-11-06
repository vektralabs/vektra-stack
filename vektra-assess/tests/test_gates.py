"""M2.1: ``evaluate_start_gates`` fixture table, every gate negated individually.

The baseline passes every gate; each row changes ONE input and names the code and the REQ-068
state it must produce. The REQ-091 rows cover the fallback (any close input unreadable) and the
readable-again rule (max(automatic, teacher date), never the teacher date alone, never the
activity close alone), and the date-disclosure rule (another student's extension is never shown).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from vektra_assess.gates import (
    ButtonState,
    Code,
    GateInputs,
    evaluate_start_gates,
)

NOW = 2_000_000_000
CLOSE = NOW - 3600  # the activity closed an hour ago


def base(kind: str = "assign", **over: Any) -> GateInputs:
    i = GateInputs(
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
        activity_close_at=CLOSE,
        grace_s=0,
        participant_closes=(),
        own_close_at=None,
        teacher_opening_at=None,
        owner_verified=True,
        enrolled=True,
        open_session=False,
        closed_sessions=0,
        reset_grants=0,
        privacy_configured=True,
        end_at=None,
    )
    return replace(i, **over)


def test_baseline_is_eligible_for_both_kinds() -> None:
    for kind in ("assign", "quiz"):
        d = evaluate_start_gates(base(kind))
        assert d.open and d.code is Code.OK and d.state is ButtonState.ELIGIBLE
        assert d.reasons == () and d.gate_at == CLOSE


NEGATED: list[tuple[str, dict[str, Any], Code, ButtonState]] = [
    ("a: not enabled", {"enabled": False}, Code.NOT_ENABLED, ButtonState.NOT_ENABLED),
    ("a: team submission", {"team_submission": True}, Code.TEAM_SUBMISSION, ButtonState.NOT_ENABLED),
    ("a: marking guide (EX-023)", {"grading_method": "guide"}, Code.MARKING_GUIDE, ButtonState.NOT_ENABLED),
    ("a: unknown grading plugin", {"grading_method": "checklist"}, Code.UNSUPPORTED_GRADING, ButtonState.NOT_ENABLED),
    ("a: grading method unreadable", {"grading_method": None}, Code.UNREADABLE, ButtonState.UNAVAILABLE),
    ("b: attempt in progress/overdue", {"attempt_in_progress": True}, Code.ATTEMPT_IN_PROGRESS, ButtonState.NOT_OPEN),
    ("b: no submitted attempt", {"has_submitted_attempt": False}, Code.NO_SUBMITTED_ATTEMPT, ButtonState.NOT_GRADED),
    ("b/BR-014: regrade in progress", {"regrade_in_progress": True}, Code.REGRADE_IN_PROGRESS, ButtonState.NOT_GRADED),
    ("b/BR-014: not fully graded", {"fully_graded": False}, Code.NOT_FULLY_GRADED, ButtonState.NOT_GRADED),
    ("b: attempts unreadable", {"attempt_in_progress": None}, Code.UNREADABLE, ButtonState.UNAVAILABLE),
    ("c/BR-010: not released", {"released": False}, Code.NOT_RELEASED, ButtonState.NOT_GRADED),
    ("c: release unreadable", {"released": None}, Code.UNREADABLE, ButtonState.UNAVAILABLE),
    ("d: close not passed", {"activity_close_at": NOW + 60}, Code.CLOSE_NOT_PASSED, ButtonState.NOT_OPEN),
    ("d: grace not passed", {"grace_s": 7200}, Code.CLOSE_NOT_PASSED, ButtonState.NOT_OPEN),
    ("d: an extension not passed", {"participant_closes": (NOW + 600,)}, Code.CLOSE_NOT_PASSED, ButtonState.NOT_OPEN),
    ("BR-007: no close / cut-off", {"activity_close_at": 0}, Code.NO_CLOSE_DATE, ButtonState.NOT_OPEN),
    ("REQ-091: overrides unreadable", {"participant_closes": None}, Code.CLOSE_UNREADABLE, ButtonState.NOT_OPEN),
    ("REQ-091: grace unreadable", {"grace_s": None}, Code.CLOSE_UNREADABLE, ButtonState.NOT_OPEN),
    ("REQ-091: activity close unreadable", {"activity_close_at": None}, Code.CLOSE_UNREADABLE, ButtonState.NOT_OPEN),
    ("e: not the owner", {"owner_verified": False}, Code.NOT_OWNER, ButtonState.UNAVAILABLE),
    ("f: not enrolled", {"enrolled": False}, Code.NOT_ENROLLED, ButtonState.UNAVAILABLE),
    ("f: enrolment unreadable", {"enrolled": None}, Code.UNREADABLE, ButtonState.UNAVAILABLE),
    ("g: a session is open", {"open_session": True}, Code.SESSION_OPEN, ButtonState.ACTIVE),
    ("g: already discussed", {"closed_sessions": 1}, Code.ALREADY_DISCUSSED, ButtonState.CLOSED),
    ("h: privacy not configured", {"privacy_configured": False}, Code.PRIVACY_NOT_CONFIGURED, ButtonState.UNAVAILABLE),
    ("i: end date passed", {"end_at": NOW - 1}, Code.END_DATE_PASSED, ButtonState.ENDED),
]  # fmt: skip


@pytest.mark.parametrize(
    ("name", "over", "code", "state"), NEGATED, ids=[n[0] for n in NEGATED]
)
def test_every_gate_negated_individually(
    name: str, over: dict[str, Any], code: Code, state: ButtonState
) -> None:
    d = evaluate_start_gates(base(**over))
    assert not d.open, name
    assert d.code is code, (name, d.code, d.reasons)
    assert d.state is state, (name, d.state)
    assert d.reasons == (code,), (name, d.reasons)  # exactly one gate failed


def test_every_code_but_ok_is_reachable_by_the_table() -> None:
    covered = {row[2] for row in NEGATED}
    assert covered == set(Code) - {Code.OK}


def test_team_and_guide_do_not_apply_to_a_quiz() -> None:
    d = evaluate_start_gates(base("quiz", team_submission=None, grading_method=None))
    assert d.open


def test_reset_granted_after_a_closed_session() -> None:
    d = evaluate_start_gates(base(closed_sessions=1, reset_grants=1))
    assert d.open and d.state is ButtonState.RESET_GRANTED and d.session_no_next == 2


def test_end_date_in_the_future_is_open() -> None:
    assert evaluate_start_gates(base(end_at=NOW + 1)).open


# --- REQ-091 fallback and BR-006 (d) dates -------------------------------------------------------


def test_fallback_with_teacher_date_gates_on_max_of_close_and_teacher_date() -> None:
    later = NOW + 600
    d = evaluate_start_gates(base(participant_closes=None, teacher_opening_at=later))
    assert d.code is Code.CLOSE_NOT_PASSED and d.gate_at == later
    d = evaluate_start_gates(base(participant_closes=None, teacher_opening_at=NOW - 10))
    assert d.open and d.gate_at == NOW - 10
    # a teacher date before the activity's own close never opens before that close
    d = evaluate_start_gates(
        base(
            activity_close_at=NOW + 900,
            participant_closes=None,
            teacher_opening_at=NOW - 10,
        )
    )
    assert d.code is Code.CLOSE_NOT_PASSED and d.gate_at == NOW + 900


def test_readable_again_gate_is_max_of_automatic_and_teacher_date() -> None:
    # automatic (an extension) later than the teacher date: the extension wins
    d = evaluate_start_gates(
        base(participant_closes=(NOW + 300,), teacher_opening_at=NOW - 10)
    )
    assert d.code is Code.CLOSE_NOT_PASSED and d.gate_at == NOW + 300
    # teacher date later than the automatic close: the teacher date wins
    d = evaluate_start_gates(base(participant_closes=(), teacher_opening_at=NOW + 50))
    assert d.code is Code.CLOSE_NOT_PASSED and d.gate_at == NOW + 50


def test_the_fallback_never_replaces_a_missing_cutoff() -> None:
    d = evaluate_start_gates(
        base(activity_close_at=0, participant_closes=None, teacher_opening_at=NOW - 10)
    )
    assert d.code is Code.NO_CLOSE_DATE


def test_another_students_extension_date_is_never_shown() -> None:
    other = NOW + 3 * 86400  # someone else's extension holds the gate
    d = evaluate_start_gates(base(participant_closes=(other,)))
    assert d.code is Code.CLOSE_NOT_PASSED
    assert d.gate_at == other and d.opens_at is None
    # the student's OWN extension may be shown
    d = evaluate_start_gates(base(participant_closes=(other,), own_close_at=other))
    assert d.opens_at == other
    # the activity's own close may be shown
    d = evaluate_start_gates(base(activity_close_at=NOW + 60))
    assert d.opens_at == NOW + 60


def test_quiz_grace_is_added_to_every_close() -> None:
    d = evaluate_start_gates(
        base(
            "quiz",
            activity_close_at=NOW - 100,
            grace_s=300,
            participant_closes=(NOW - 200,),
        )
    )
    assert d.code is Code.CLOSE_NOT_PASSED and d.gate_at == NOW + 200
    assert d.opens_at == NOW + 200  # the activity's own effective close (close + grace)
