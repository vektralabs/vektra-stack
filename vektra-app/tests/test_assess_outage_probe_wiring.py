"""The worker's outage probe goes through the discussion provider exactly as a turn does: the
same litellm path, the deployment's ``VEKTRA_ASSESS_LLM_EXTRA_BODY``, the discussion temperature,
and a completion budget a reasoning model accepts (never ``max_tokens=1``)."""

from __future__ import annotations

from typing import Any

import pytest

from vektra_app import assess_worker
from vektra_assess.config import VektraAssessSettings
from vektra_assess.worker.outage_probe import PROBE_MAX_TOKENS


class _Choice:
    class message:  # litellm's response shape: choices[0].message.content
        content = ""

    finish_reason = "length"


class _Response:
    choices = [_Choice()]
    usage = None
    model = "openai/reasoner"


async def test_the_probe_carries_the_extra_body_and_a_sufficient_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vektra_core.providers.litellm_provider as provider

    calls: list[dict[str, Any]] = []

    async def acompletion(**kw: Any) -> _Response:
        calls.append(kw)
        if (kw.get("max_tokens") or 0) < 16:  # an OpenAI reasoning model's 400
            raise AssertionError("max_tokens below what a reasoning model accepts")
        return _Response()

    monkeypatch.setattr(provider.litellm, "acompletion", acompletion)
    settings = VektraAssessSettings.model_validate(
        {
            "VEKTRA_ASSESS_LLM_MODEL": "openai/reasoner",
            "VEKTRA_ASSESS_LLM_EXTRA_BODY": '{"reasoning_effort": "low"}',
            "VEKTRA_ASSESS_LLM_MAX_TOKENS": 4000,
            "VEKTRA_ASSESS_LLM_TEMPERATURE": 1.0,
        }
    )
    probe = assess_worker.make_outage_probe(settings)
    assert await probe() is True
    (call,) = calls
    assert call["extra_body"] == {"reasoning_effort": "low"}
    assert call["max_tokens"] == PROBE_MAX_TOKENS
    assert call["temperature"] == 1.0
    assert call["model"] == "openai/reasoner"
