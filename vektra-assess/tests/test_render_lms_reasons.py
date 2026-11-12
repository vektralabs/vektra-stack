"""The render evaluation stores the Moodle half a teacher reset re-checks (plan 11b, REQ-087)."""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from typing import Any

from vektra_assess.gates import (
    DB_SIDE,
    ButtonState,
    Code,
    GateDecision,
    GateInputs,
    evaluate_start_gates,
)
from vektra_assess.worker.render import store_render


class _Conn:
    def __init__(self, sink: list[Any]) -> None:
        self.sink = sink

    async def execute(self, stmt: Any, params: Any = None) -> None:
        self.sink.append((str(stmt), params))

    def begin(self) -> Any:
        @asynccontextmanager
        async def tx():  # type: ignore[no-untyped-def]
            yield self

        return tx()


async def _stored(decision: GateDecision) -> dict[str, Any]:
    sink: list[Any] = []

    @asynccontextmanager
    async def acquire():  # type: ignore[no-untyped-def]
        yield _Conn(sink)

    await store_render(acquire, uuid.uuid4(), 5, 9, decision)
    (params,) = [p for s, p in sink if "assess_gate_inputs" in s]
    return json.loads(params["i"])  # type: ignore[no-any-return]


async def test_a_full_evaluation_stores_its_lms_reasons_only() -> None:
    d = evaluate_start_gates(
        GateInputs(
            now=10,
            activity_kind="quiz",
            enabled=True,
            attempt_in_progress=False,
            has_submitted_attempt=True,
            regrade_in_progress=False,
            fully_graded=True,
            released=False,
            activity_close_at=5,
            participant_closes=(),
            enrolled=True,
            closed_sessions=1,
            privacy_configured=True,
            end_at=10,
        )
    )
    assert Code.ALREADY_DISCUSSED in d.reasons and Code.END_DATE_PASSED in d.reasons
    got = await _stored(d)
    assert got["lms_reasons"] == [
        "not_released"
    ]  # the DB-side codes are re-checked live
    ok = evaluate_start_gates(
        GateInputs(
            now=10,
            activity_kind="quiz",
            enabled=True,
            attempt_in_progress=False,
            has_submitted_attempt=True,
            regrade_in_progress=False,
            fully_graded=True,
            released=True,
            activity_close_at=5,
            participant_closes=(),
            enrolled=True,
            privacy_configured=True,
        )
    )
    assert (await _stored(ok))["lms_reasons"] == []


async def test_a_short_cut_evaluation_stores_no_moodle_half() -> None:
    """Disabled or a session open: Moodle was not read, so a reset must ask again."""
    for code, state in (
        (Code.NOT_ENABLED, ButtonState.NOT_ENABLED),
        (Code.SESSION_OPEN, ButtonState.ACTIVE),
    ):
        assert "lms_reasons" not in await _stored(GateDecision(code, state))


def test_db_side_codes_are_the_ones_vektra_decides_alone() -> None:
    assert {c.value for c in DB_SIDE} == {
        "not_enabled",
        "end_date_passed",
        "session_open",
        "already_discussed",
        "privacy_not_configured",
    }
