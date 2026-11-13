"""The outage probe's call mirrors a discussion turn's (REQ-086).

Bug of the first live run: the probe was a non-streamed ``max_tokens=1`` call, which an OpenAI
reasoning model refuses (400), so an OUTAGE never ended. Review of #219: a fixed small budget is
not the turn's either. The probe now sends what a turn sends (streamed, the turn's max_tokens,
temperature and seed, through the same provider, so its extra body too), and stops at the first
text or at completion.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fakes import RecordingAuditSink as _Audit
from fakes import RecordingSafeguard as Guard
from fakes import RecordingTraceWriter as _Trace
from fakes import TimedLLM
from support.litellm_errors import litellm_error
from test_llm_runner import _visible_bundle, settings

from vektra_assess.llm import DiscussionTurnRunner, StudentTurn
from vektra_assess.worker.outage_probe import make_llm_probe
from vektra_shared.types import CompletionChunk


class TurnShapedLlm:
    """A provider that accepts only a turn-shaped call: streamed (``complete`` is refused, as a
    deployment may allow streaming only), and ``max_tokens`` at least ``floor`` (a reasoning
    model refuses a budget too small for its reasoning). Answers with ``chunks``."""

    def __init__(
        self,
        floor: int = 64,
        chunks: tuple[tuple[str, str | None], ...] = (("", None), ("Ciao", None)),
    ) -> None:
        self.floor = floor
        self.chunks = chunks
        self.calls: list[dict[str, Any]] = []
        self.read = 0
        self.closed = False

    async def complete(self, *a: Any, **kw: Any) -> Any:
        raise litellm_error("BadRequestError", 400)

    async def stream(  # type: ignore[no-untyped-def]
        self, messages, model, temperature=0.7, max_tokens=None, **kw
    ) -> AsyncIterator[CompletionChunk]:
        self.calls.append(
            {"model": model, "temperature": temperature, "max_tokens": max_tokens, **kw}
        )
        if max_tokens is None or max_tokens < self.floor:
            raise litellm_error("BadRequestError", 400)
        outer = self

        async def gen() -> AsyncIterator[CompletionChunk]:
            try:
                for content, finish in outer.chunks:
                    outer.read += 1
                    yield CompletionChunk(
                        content=content, done=finish is not None, finish_reason=finish
                    )
            finally:
                outer.closed = True

        return gen()


def _probe(llm: Any, **kw: Any) -> Any:
    args = {"timeout_s": 1.0, "max_tokens": 600, "temperature": 0.2, "seed": None}
    return make_llm_probe(llm, "openai/reasoner", **{**args, **kw})


async def test_the_probe_streams_and_never_calls_complete() -> None:
    llm = TurnShapedLlm()
    assert await _probe(llm)() is True
    assert len(llm.calls) == 1


async def test_the_probe_sends_the_turn_budget_a_reasoning_model_needs() -> None:
    """A provider that refuses any max_tokens below 600 answers the probe: its budget is the
    turn's ``VEKTRA_ASSESS_LLM_MAX_TOKENS``, not a small constant."""
    llm = TurnShapedLlm(floor=600)
    assert await _probe(llm, max_tokens=600)() is True
    with pytest.raises(Exception) as e:
        await _probe(TurnShapedLlm(floor=600), max_tokens=599)()
    assert (
        type(e.value).__name__ == "BadRequestError"
    )  # refused exactly as a turn would be


async def test_the_seed_and_temperature_reach_the_call() -> None:
    llm = TurnShapedLlm()
    await _probe(llm, seed=7, temperature=1.0)()
    assert llm.calls == [
        {"model": "openai/reasoner", "temperature": 1.0, "max_tokens": 600, "seed": 7}
    ]
    llm = TurnShapedLlm()
    await _probe(llm, seed=None)()
    assert "seed" not in llm.calls[0]


async def test_the_probe_stops_at_the_first_text_and_closes_the_stream() -> None:
    llm = TurnShapedLlm(
        chunks=(("", None), ("Ciao", None), (" mondo", None), ("!", "stop"))
    )
    assert await _probe(llm)() is True
    assert llm.read == 2 and llm.closed


async def test_a_stream_completed_with_no_text_means_up() -> None:
    """A reasoning model that spent the budget on reasoning: empty chunks, finish "length"."""
    llm = TurnShapedLlm(chunks=(("", None), (" ", None), ("", "length")))
    assert await _probe(llm)() is True


async def test_a_stream_with_no_first_text_in_time_is_a_failure() -> None:
    class Slow(TurnShapedLlm):
        async def stream(self, *a: Any, **kw: Any) -> AsyncIterator[CompletionChunk]:  # type: ignore[override]
            async def gen() -> AsyncIterator[CompletionChunk]:
                await asyncio.sleep(5)
                yield CompletionChunk(content="tardi")

            return gen()

    with pytest.raises(TimeoutError):
        await _probe(Slow(), timeout_s=0.05)()


async def test_the_probe_call_is_the_turn_call() -> None:
    """The same keyword arguments a real turn passes to ``stream``, from the same settings."""

    class Recording(TimedLLM):
        def __init__(self) -> None:
            super().__init__([(0, "Bene. Quale scelta vuoi spiegare?")])
            self.kwargs: list[dict[str, Any]] = []

        async def stream(  # type: ignore[override]
            self,
            messages: Any,
            model: str,
            temperature: float = 0.7,
            max_tokens: int | None = None,
            **kw: Any,
        ) -> AsyncIterator[CompletionChunk]:
            self.kwargs.append(
                {
                    "model": model,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    **kw,
                }
            )
            return await super().stream(messages, model, temperature, max_tokens, **kw)

    s = settings(
        VEKTRA_ASSESS_LLM_SEED=11,
        VEKTRA_ASSESS_LLM_TEMPERATURE=0.7,
        VEKTRA_ASSESS_OUTAGE_TIMEOUT_S=1.0,
    )
    turn_llm = Recording()
    r = DiscussionTurnRunner(
        llm=turn_llm,
        safeguard=Guard(),
        trace_writer=_Trace(),
        settings=s,
        audit=_Audit(),
    )
    await r.run(_visible_bundle(), StudentTurn("ciao", 0))
    probe_llm = Recording()
    await make_llm_probe(
        probe_llm,
        r.model,
        timeout_s=1.0,
        max_tokens=s.llm_max_tokens,
        temperature=s.llm_temperature,
        seed=s.llm_seed,
    )()
    assert probe_llm.kwargs == [turn_llm.kwargs[0]]
