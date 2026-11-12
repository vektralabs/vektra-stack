"""The closed outage cause a ``ProviderOutageError`` carries (Start pre-flight observability).

A Start rolled back on ``provider_unavailable`` said nothing about why the opening call failed.
The runner now records a closed cause code and the HTTP status on the call record, and the
Part B generation the outage ended, from exception TYPE names and integer statuses only: the
exception message can echo the prompt and is never read.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fakes import TimedLLM
from test_llm_runner import _timed_runner, _visible_bundle

from vektra_assess.llm import ProviderOutageError
from vektra_assess.llm.runner import outage_cause
from vektra_assess.llm.types import OUTAGE_CAUSES, ProviderCallRecord
from vektra_shared.types import CompletionChunk

CANARY = "CNRY-prompt-echo-9f8e7d"


# provider-library look-alikes, matched by type name like the real ones (litellm, openai)
class Timeout(Exception):
    status_code = 408  # litellm's own Timeout carries a 408


class APIConnectionError(Exception):
    status_code = 500  # litellm's connection error claims a 500


class InternalServerError(Exception):
    def __init__(self, msg: str, status_code: int) -> None:
        super().__init__(msg)
        self.status_code = status_code


class _Resp:
    status_code = 429


class RateLimitError(Exception):
    response = _Resp()


def _wrapped(outer: BaseException, inner: BaseException) -> BaseException:
    try:
        try:
            raise inner
        except BaseException as e:
            raise outer from e
    except BaseException as e:
        return e


@pytest.mark.parametrize(
    ("exc", "cause", "status"),
    [
        (
            _wrapped(Timeout(CANARY), httpx.ConnectTimeout(CANARY)),
            "connect_timeout",
            408,
        ),
        (_wrapped(Timeout(CANARY), httpx.ReadTimeout(CANARY)), "read_timeout", 408),
        (_wrapped(Timeout(CANARY), httpx.PoolTimeout(CANARY)), "pool_timeout", 408),
        (Timeout(CANARY), "timeout", 408),
        (TimeoutError(), "timeout", None),
        (APIConnectionError(CANARY), "connection_error", 500),
        (ConnectionError(CANARY), "connection_error", None),
        (InternalServerError(CANARY, 503), "http_5xx", 503),
        (InternalServerError(CANARY, 400), "http_4xx", 400),
        (RateLimitError(CANARY), "http_4xx", 429),
        (RuntimeError(CANARY), "provider_error", None),
    ],
)
def test_a_provider_exception_maps_to_a_closed_cause(
    exc: BaseException, cause: str, status: int | None
) -> None:
    assert outage_cause(exc) == (cause, status)
    assert cause in OUTAGE_CAUSES


def test_an_exception_without_a_cause_on_its_record_is_classified_by_outcome() -> None:
    """Records built before the cause existed (and by tests) still give a closed code."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    assert ProviderOutageError(ProviderCallRecord(now, None, now, "error")).cause == (
        "provider_error"
    )
    timed_out = ProviderOutageError(
        ProviderCallRecord(now, None, now, "first_token_timeout")
    )
    assert timed_out.cause == "first_token_timeout"
    assert timed_out.expired_budget == "outage_timeout"
    bogus = ProviderOutageError(
        ProviderCallRecord(now, None, now, "error", cause=CANARY)
    )
    assert bogus.cause == "provider_error"  # never a free value


class _Raising(TimedLLM):
    def __init__(self, exc: BaseException) -> None:
        super().__init__([])
        self.exc = exc

    async def stream(self, *a: Any, **kw: Any) -> AsyncIterator[CompletionChunk]:  # type: ignore[override]
        raise self.exc


async def test_a_part_b_open_failure_carries_cause_status_and_attempt() -> None:
    r, _ = _timed_runner(_Raising(InternalServerError(CANARY, 503)))
    with pytest.raises(ProviderOutageError) as e:
        await r.run_part_b(_visible_bundle())
    err = e.value
    assert (err.cause, err.record.status_code, err.attempt) == ("http_5xx", 503, 1)
    assert err.record.error_class == "InternalServerError"
    assert err.expired_budget is None
    assert CANARY not in repr(err.record) and CANARY not in str(err)


async def test_a_part_b_read_timeout_names_the_provider_budget() -> None:
    exc = _wrapped(Timeout(CANARY), httpx.ReadTimeout(CANARY))
    r, _ = _timed_runner(_Raising(exc))
    with pytest.raises(ProviderOutageError) as e:
        await r.run_part_b(_visible_bundle())
    assert (e.value.cause, e.value.expired_budget) == (
        "read_timeout",
        "provider_timeout",
    )


@pytest.mark.parametrize(
    ("chunks", "timeout", "cause", "budget"),
    [
        ([(0, ""), (0, "")], 0.2, "empty_stream", None),
        ([(0.5, "Tardi.")], 0.1, "first_token_timeout", "outage_timeout"),
        ([(0, "Ciao. "), (0.5, "mai")], 0.1, "stream_stall", "outage_timeout"),
    ],
)
async def test_part_b_stream_outages_have_their_own_causes(
    chunks: list[tuple[float, str]], timeout: float, cause: str, budget: str | None
) -> None:
    r, _ = _timed_runner(TimedLLM(chunks), timeout=timeout)
    with pytest.raises(ProviderOutageError) as e:
        await r.run_part_b(_visible_bundle())
    assert (e.value.cause, e.value.expired_budget, e.value.attempt) == (
        cause,
        budget,
        1,
    )


async def test_a_turn_open_failure_carries_its_cause_too() -> None:
    from vektra_assess.llm import StudentTurn

    r, _ = _timed_runner(TimedLLM([(0, "x")], fail="open"))
    with pytest.raises(ProviderOutageError) as e:
        await r.run(_visible_bundle(), StudentTurn("ciao", 0))
    assert e.value.cause == "connection_error" and e.value.attempt is None


class _Reasoning(TimedLLM):
    """A reasoning model with too small a max_tokens: empty content chunks (the reasoning is
    not ``content``), then ``finish_reason`` on the last one, and no text at all."""

    def __init__(self, finish: str | None) -> None:
        super().__init__([])
        self.finish = finish

    async def stream(self, *a: Any, **kw: Any) -> AsyncIterator[CompletionChunk]:  # type: ignore[override]
        outer = self

        async def gen() -> AsyncIterator[CompletionChunk]:
            try:
                yield CompletionChunk(content="")
                yield CompletionChunk(content="")
                yield CompletionChunk(content="", done=True, finish_reason=outer.finish)
            finally:
                outer.closed = True

        return gen()


@pytest.mark.parametrize(
    ("finish", "cause", "budget"),
    [
        ("length", "max_tokens_exhausted", "max_tokens"),
        ("stop", "empty_stream", None),
        (None, "empty_stream", None),
    ],
)
async def test_a_stream_spent_on_reasoning_is_max_tokens_exhausted(
    finish: str | None, cause: str, budget: str | None
) -> None:
    llm = _Reasoning(finish)
    r, _ = _timed_runner(llm)
    with pytest.raises(ProviderOutageError) as e:
        await r.run_part_b(_visible_bundle())
    assert (e.value.cause, e.value.expired_budget) == (cause, budget)
    assert e.value.record.error_class == "EmptyStream" and llm.closed
