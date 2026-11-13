"""The worker's outage probe calls the discussion provider exactly as a turn does: the same
litellm path (streamed), the deployment's ``VEKTRA_ASSESS_LLM_EXTRA_BODY``, the discussion
``max_tokens``, temperature and seed."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from vektra_app import assess_worker
from vektra_assess.config import VektraAssessSettings


class _Delta:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str, finish: str | None) -> None:
        self.delta = _Delta(content)
        self.finish_reason = finish


class _Chunk:
    model = "openai/reasoner"

    def __init__(self, content: str, finish: str | None = None) -> None:
        self.choices = [_Choice(content, finish)]


async def test_the_probe_is_a_turn_shaped_litellm_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vektra_core.providers.litellm_provider as provider

    calls: list[dict[str, Any]] = []

    async def acompletion(**kw: Any) -> AsyncIterator[_Chunk]:
        calls.append(kw)
        if not kw.get("stream"):
            raise AssertionError("the probe must stream, like a turn")

        async def gen() -> AsyncIterator[_Chunk]:
            yield _Chunk("")  # reasoning
            yield _Chunk("Ciao")

        return gen()

    monkeypatch.setattr(provider.litellm, "acompletion", acompletion)
    settings = VektraAssessSettings.model_validate(
        {
            "VEKTRA_ASSESS_LLM_MODEL": "openai/reasoner",
            "VEKTRA_ASSESS_LLM_EXTRA_BODY": '{"reasoning_effort": "low"}',
            "VEKTRA_ASSESS_LLM_MAX_TOKENS": 4000,
            "VEKTRA_ASSESS_LLM_CONTEXT_WINDOW": 16000,
            "VEKTRA_ASSESS_LLM_TEMPERATURE": 1.0,
            "VEKTRA_ASSESS_LLM_SEED": 42,
        }
    )
    probe = assess_worker.make_outage_probe(settings)
    assert await probe() is True
    (call,) = calls
    assert call["stream"] is True
    assert call["extra_body"] == {"reasoning_effort": "low"}
    assert call["max_tokens"] == 4000
    assert call["temperature"] == 1.0
    assert call["seed"] == 42
    assert call["model"] == "openai/reasoner"
