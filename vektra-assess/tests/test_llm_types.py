"""vektra_assess.llm public types (plan assess-llm-runner, task A)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

import vektra_assess.llm as llm
from vektra_assess.llm.types import ProviderCallRecord, StudentTurn, TurnUsage


def test_the_package_exports_only_the_public_surface() -> None:
    assert set(llm.__all__) == {
        "DiscussionTurnRunner",
        "PartBRejectedError",
        "ProviderCallRecord",
        "ProviderOutageError",
        "RagNotAvailableError",
        "ReleasedSegment",
        "RunnerMetrics",
        "StudentTurn",
        "TraceWriter",
        "TurnResult",
        "TurnUsage",
    }
    for name in llm.__all__:
        assert hasattr(llm, name)


def test_types_are_frozen() -> None:
    turn = StudentTurn(text="x", turn_index=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        turn.text = "y"  # type: ignore[misc]


def test_outage_error_carries_a_content_free_record() -> None:
    now = datetime.now(UTC)
    rec = ProviderCallRecord(now, None, now, "first_token_timeout")
    err = llm.ProviderOutageError(rec)
    assert err.record is rec and str(err) == "first_token_timeout"
    # timings, an outcome, a type name, a closed cause code and an HTTP status: no text field
    assert {f.name for f in dataclasses.fields(rec)} == {
        "started_at", "first_token_at", "ended_at", "outcome", "error_class", "cause",
        "status_code",
    }  # fmt: skip


def test_usage_is_an_estimate_by_default() -> None:
    assert TurnUsage(10, 5).estimated is True and TurnUsage(10, 5).cost is None
