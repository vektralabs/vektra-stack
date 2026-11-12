"""Teacher config request model: strict, extra fields refused, write-only tri-state (OQ-034)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from pydantic import ValidationError

from vektra_assess import privacy
from vektra_assess.errors import config_conflict, config_invalid
from vektra_assess.teacher.schemas import (
    STATE_SET,
    STATE_UNSET,
    ConfigPut,
    FieldError,
    TriState,
    field_errors,
    state,
)
from vektra_shared.errors import http_status_for

BASE = {
    "actor_userid": 5,
    "courseid": 7,
    "activity_type": "quiz",
    "expected_version": 0,
}


def test_absent_null_and_value_are_three_states() -> None:
    body = ConfigPut.model_validate({**BASE, "solution": None, "pasted_rubric": "x"})
    assert body.tri_state("solution") is TriState.CLEAR
    assert body.tri_state("pasted_rubric") is TriState.REPLACE
    assert body.tri_state("transcript_recipients") is TriState.UNCHANGED
    assert body.given("solution") and not body.given("enabled")


@pytest.mark.parametrize(
    "bad",
    [
        {"recipients_extra": ["a@uni.example"]},  # unknown field
        {"cap_minutes": "30"},  # strict int
        {"enabled": "true"},  # strict bool
        {"rubric_source": "html"},
        {"activity_type": "forum"},
        {"actor_userid": 0},
        {"expected_version": -1},
        {"transcript_recipients": ["a@uni.example"] * 21},
    ],
)
def test_malformed_bodies_are_refused(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ConfigPut.model_validate({**BASE, **bad})


def test_out_of_range_values_reach_the_field_rules_not_the_schema() -> None:
    """A cap of 121 or a 20,001-char solution must become a field error naming the range."""
    body = ConfigPut.model_validate(
        {**BASE, "cap_minutes": 121, "solution": "a" * 20_001}
    )
    assert body.cap_minutes == 121


def test_states_and_error_envelopes() -> None:
    assert (
        (state(True), state(False))
        == (STATE_SET, STATE_UNSET)
        == (
            "impostata",
            "non impostata",
        )
    )
    err = config_invalid(
        field_errors([FieldError("cap_minutes", "out_of_range", "5-120")])
    )
    assert http_status_for(err) == 400 and err.code == "ERR-ASSESS-022"
    assert err.details["fields"] == [
        {"field": "cap_minutes", "code": "out_of_range", "range": "5-120"}
    ]
    conflict = config_conflict(4)
    assert http_status_for(conflict) == 409 and conflict.details == {
        "current_version": 4
    }


def test_privacy_is_not_configured_until_plan_12() -> None:
    """Fail closed: the REQ-089 store does not exist yet."""
    assert asyncio.run(privacy.is_configured(None, uuid.uuid4())) is False


def test_an_unbound_teacher_key_is_told_to_use_a_teacher_key() -> None:
    """CodeRabbit 4505511216: the 403 of an unbound key names the scope of its route."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from vektra_assess.api.common import site_of_key

    class Conn:
        async def execute(self, *_a: object) -> SimpleNamespace:
            # the definer lookup (assess_site_of_key) finds no binding
            return SimpleNamespace(first=lambda: None, scalar=lambda: None)

    key = SimpleNamespace(key_id=uuid.uuid4())
    for purpose, scope in (("teacher", "assess:teacher"), ("mint", "assess:mint")):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(site_of_key(Conn(), key, purpose))
        error = exc.value.detail["error"]  # type: ignore[index]
        assert error["code"] == "ERR-ASSESS-007"
        assert scope in error["remediation"]


def test_teacher_operation_bodies_are_strict() -> None:
    """11b: the reset / terminate / extra-time / fallback bodies forbid unknown fields (a
    ``reason`` on extra time is refused: REQ-078 has none)."""
    import pydantic

    from vektra_assess.teacher.schemas import (
        ExtraTimePut,
        OpeningFallbackPut,
        ResetPost,
        TerminatePost,
    )

    base = {"actor_userid": 5, "courseid": 7, "activity_type": "quiz"}
    ResetPost.model_validate({**base, "reason": "x"})
    TerminatePost.model_validate({**base, "reason": "x", "expected_active_count": 0})
    ExtraTimePut.model_validate({**base, "percent": 50})
    OpeningFallbackPut.model_validate(
        {
            **base,
            "opening_at": "2030-01-01T00:00:00+00:00",
            "attestation": True,
            "attestation_text_version": "it-1",
        }
    )
    for model, body in (
        (ExtraTimePut, {**base, "percent": 50, "reason": "dsa"}),
        (ResetPost, {**base}),  # the reason is mandatory
        (TerminatePost, {**base, "reason": "x", "expected_active_count": -1}),
        (ResetPost, {**base, "reason": "x", "include_previous_turns": "yes"}),
        (
            OpeningFallbackPut,
            {
                **base,
                "opening_at": "2030-01-01T00:00:00+00:00",
                "attestation": True,
                "attestation_text_version": "bad version!",
            },
        ),
    ):
        with pytest.raises(pydantic.ValidationError):
            model.model_validate(body)


def test_reset_and_terminate_refusals_are_409_with_their_codes() -> None:
    from vektra_assess import errors as e
    from vektra_shared.errors import http_status_for

    r = e.reset_refused("gate", gate="end_date_passed", field="available_until")
    assert (r.code, http_status_for(r)) == ("ERR-ASSESS-024", 409)
    assert r.details == {
        "reason": "gate",
        "gate": "end_date_passed",
        "field": "available_until",
    }
    t = e.terminate_count_mismatch(3)
    assert (t.code, http_status_for(t), t.details) == (
        "ERR-ASSESS-025",
        409,
        {"current": 3},
    )
