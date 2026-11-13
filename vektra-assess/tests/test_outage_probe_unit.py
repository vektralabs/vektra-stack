"""The outage probe's call (REQ-086): a budget a reasoning model accepts, the deployment's
extra body, and any completed answer counts as "up".

Bug of the first live run with a reasoning model: the probe asked for ``max_tokens=1``, which an
OpenAI reasoning model refuses with a 400, so a session in OUTAGE could never recover.
"""

from __future__ import annotations

from typing import Any

import pytest

from vektra_assess.worker.outage_probe import PROBE_MAX_TOKENS, make_llm_probe
from vektra_shared.types import CompletionResponse


class ReasoningLlm:
    """Refuses a completion budget below ``floor`` like an OpenAI reasoning model (400), and
    otherwise answers with no text and ``finish_reason=length``: the budget went on reasoning."""

    def __init__(self, floor: int = 16) -> None:
        self.floor = floor
        self.calls: list[dict[str, Any]] = []
        self.temperatures: list[float] = []

    async def complete(  # type: ignore[no-untyped-def]
        self, messages, model, temperature=0.7, max_tokens=None, **kw
    ) -> CompletionResponse:
        self.temperatures.append(temperature)
        self.calls.append({"model": model, "max_tokens": max_tokens, "kw": kw})
        if max_tokens is not None and max_tokens < self.floor:
            litellm = pytest.importorskip("litellm")
            raise litellm.BadRequestError(
                message="max_tokens too small", model=model, llm_provider="openai"
            )
        return CompletionResponse(
            content="",
            model=model,
            prompt_tokens=1,
            completion_tokens=max_tokens or 0,
            total_tokens=1 + (max_tokens or 0),
        )


def test_the_probe_budget_is_small_but_not_one() -> None:
    # 64 is the floor of VEKTRA_ASSESS_LLM_MAX_TOKENS
    assert 16 <= PROBE_MAX_TOKENS <= 64


async def test_the_probe_succeeds_on_a_reasoning_model() -> None:
    llm = ReasoningLlm(floor=16)
    probe = make_llm_probe(llm, "openai/reasoner", timeout_s=1)
    assert await probe() is True
    (call,) = llm.calls
    assert call["max_tokens"] == PROBE_MAX_TOKENS


async def test_an_empty_length_cut_answer_means_up() -> None:
    """Any completed response is the provider answering, even an empty one cut at the budget."""
    probe = make_llm_probe(ReasoningLlm(floor=0), "openai/reasoner", timeout_s=1)
    assert await probe() is True


async def test_the_budget_never_exceeds_the_configured_max_tokens() -> None:
    llm = ReasoningLlm(floor=0)
    probe = make_llm_probe(llm, "m", timeout_s=1, llm_max_tokens=20)
    await probe()
    assert llm.calls[0]["max_tokens"] == 20
    llm.calls.clear()
    await make_llm_probe(llm, "m", timeout_s=1, llm_max_tokens=600)()
    assert llm.calls[0]["max_tokens"] == PROBE_MAX_TOKENS


async def test_the_probe_sends_no_session_content_and_no_extra_kwargs() -> None:
    """The extra body is the provider's own (``LitellmProvider`` base kwargs, the same path as
    every discussion call): the probe adds nothing of its own on top."""
    llm = ReasoningLlm(floor=0)
    await make_llm_probe(llm, "m", timeout_s=1)()
    assert llm.calls == [{"model": "m", "max_tokens": PROBE_MAX_TOKENS, "kw": {}}]


async def test_the_probe_uses_the_discussion_temperature() -> None:
    """Some reasoning models accept only their own temperature: the probe sends the one the
    discussion calls send, so it is refused exactly when they would be."""
    llm = ReasoningLlm(floor=0)
    await make_llm_probe(llm, "m", timeout_s=1, temperature=1.0)()
    assert llm.temperatures == [1.0]
