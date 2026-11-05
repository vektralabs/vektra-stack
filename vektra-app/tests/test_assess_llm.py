"""Composition of the discussion runner (plan assess-llm-runner, task C)."""

from __future__ import annotations

from typing import Any

import pytest

from vektra_app.assess_llm import (
    AnalyticsTraceWriter,
    build_assess_llm,
    build_assess_runner,
    build_assess_safeguard,
)
from vektra_assess.config import VektraAssessSettings
from vektra_shared.safeguards import PassthroughSafeguard
from vektra_shared.startup import StartupValidationError
from vektra_shared.testing_recorders import RecordingLLMProvider


class _Real:
    async def pre_query(self, *a: Any) -> Any: ...
    async def post_retrieval(self, *a: Any) -> Any: ...
    async def pre_response(self, *a: Any) -> Any: ...


def _settings(**kw: Any) -> VektraAssessSettings:
    return VektraAssessSettings.model_validate(
        {"VEKTRA_ASSESS_LLM_MODEL": "ollama/x", **kw}
    )


def test_presidio_falling_open_is_a_startup_error() -> None:
    with pytest.raises(StartupValidationError, match="fell back to passthrough"):
        build_assess_safeguard("presidio", factory=lambda _m: PassthroughSafeguard())


def test_a_loaded_presidio_is_returned() -> None:
    real = _Real()
    assert build_assess_safeguard("presidio", factory=lambda _m: real) is real


def test_none_only_when_explicit() -> None:
    assert isinstance(build_assess_safeguard("none"), PassthroughSafeguard)
    assert _settings().safeguard_mode == "presidio"  # the default is the safe one


def test_the_assess_llm_has_its_own_model_and_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VEKTRA_LLM_PROVIDER", "openai/gpt-rag")
    monkeypatch.setenv("VEKTRA_LLM_FALLBACK_MODEL", "openai/gpt-fallback")
    monkeypatch.setenv("VEKTRA_LLM_API_BASE", "http://rag-endpoint")
    llm = build_assess_llm(_settings(VEKTRA_ASSESS_LLM_API_BASE="http://assess:11434"))
    cfg = llm._config  # type: ignore[attr-defined]
    assert cfg.provider == "ollama/x" and cfg.fallback_model is None
    assert cfg.api_base == "http://assess:11434" and cfg.api_key is None


def test_no_model_is_a_startup_error() -> None:
    with pytest.raises(StartupValidationError, match="VEKTRA_ASSESS_LLM_MODEL"):
        build_assess_llm(VektraAssessSettings())


async def test_the_trace_writer_stores_through_analytics_and_commits() -> None:
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from vektra_shared.types import QueryTrace

    session = AsyncMock()

    class _Factory:
        async def __aenter__(self) -> Any:
            return session

        async def __aexit__(self, *a: Any) -> None:
            return None

    trace = QueryTrace(
        uuid4(), [], 1, [], "m", "abcd1234", datetime.now(UTC), "assess-discussion"
    )
    await AnalyticsTraceWriter(lambda: _Factory()).write(trace, "assess-discussion")
    orm = session.add.call_args[0][0]
    assert (
        orm.namespace_id == "assess-discussion" and orm.pipeline == "assess-discussion"
    )
    session.commit.assert_awaited_once()


def test_build_runner_wires_the_injected_parts() -> None:
    llm = RecordingLLMProvider([])
    r = build_assess_runner(
        _settings(), safeguard=_Real(), trace_writer=AnalyticsTraceWriter(lambda: None),
        audit=_audit(), llm=llm,
    )  # fmt: skip
    assert r.model == "ollama/x" and r._llm is llm  # type: ignore[attr-defined]


def _audit() -> Any:
    async def audit(event: str, data: Any) -> None:
        return None

    return audit
