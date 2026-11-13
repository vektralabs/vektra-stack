"""The closed outage cause a ``ProviderOutageError`` carries (Start pre-flight observability).

A Start rolled back on ``provider_unavailable`` said nothing about why the opening call failed.
The runner now records a closed cause code and the HTTP status on the call record, and the
Part B generation the outage ended, from exception TYPE names and integer statuses only: the
exception message can echo the prompt and is never read.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import structlog
from fakes import TimedLLM
from test_llm_runner import PART_B_OK, _timed_runner, _visible_bundle

from vektra_assess.llm import ProviderOutageError, StudentTurn
from vektra_assess.llm.runner import outage_cause
from vektra_assess.llm.types import (
    OUTAGE_CAUSES,
    REFUSAL_CAUSES,
    ProviderCallRecord,
)
from vektra_shared.types import CompletionChunk

CANARY = "CNRY-prompt-echo-9f8e7d"


# provider-library look-alikes, matched by type name like the real ones (litellm, openai)
class Timeout(Exception):
    status_code = 408  # litellm's own Timeout claims a 408 without any response


class APIConnectionError(Exception):
    status_code = 500  # litellm's connection error claims a 500 without any response


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class InternalServerError(Exception):
    """A status error the provider sent: its ``response`` carries the status."""

    def __init__(self, msg: str, status_code: int) -> None:
        super().__init__(msg)
        self.response = _Resp(status_code)


class RateLimitError(Exception):
    response = _Resp(429)


class APIStatusError(Exception):
    """The OpenAI SDK's base of every received-response error (status on the exception)."""

    status_code = 404


class NotFoundError(APIStatusError):
    pass


class BudgetExceededError(Exception):
    status_code = 429  # litellm's local spend limit: a claimed status, no HTTP answer


class ServerTimeoutError(TimeoutError):
    """aiohttp's read timeout: a provider ``TimeoutError`` subclass, not a stall."""


def _from(outer: BaseException, inner: BaseException) -> BaseException:
    """``outer`` raised FROM ``inner`` (``__cause__``)."""
    try:
        try:
            raise inner
        except BaseException as e:
            raise outer from e
    except BaseException as e:
        return e


def _during(outer: BaseException, inner: BaseException) -> BaseException:
    """``outer`` raised while ``inner`` was being handled (``__context__`` only)."""
    try:
        try:
            raise inner
        except BaseException:
            raise outer  # the implicit __context__ is the point
    except BaseException as e:
        return e


@pytest.mark.parametrize(
    ("exc", "cause", "status"),
    [
        # a timeout or a connection failure never reports a status: nothing was answered
        (_from(Timeout(CANARY), httpx.ConnectTimeout(CANARY)), "connect_timeout", None),
        (_from(Timeout(CANARY), httpx.ReadTimeout(CANARY)), "read_timeout", None),
        (_from(Timeout(CANARY), httpx.PoolTimeout(CANARY)), "pool_timeout", None),
        (Timeout(CANARY), "timeout", None),
        (TimeoutError(), "timeout", None),
        (ServerTimeoutError(CANARY), "timeout", None),
        (APIConnectionError(CANARY), "connection_error", None),
        (ConnectionError(CANARY), "connection_error", None),
        # a status only from an answer the provider sent
        (InternalServerError(CANARY, 503), "http_5xx", 503),
        (InternalServerError(CANARY, 400), "http_4xx", 400),
        (RateLimitError(CANARY), "http_4xx", 429),
        (NotFoundError(CANARY), "http_4xx", 404),
        (BudgetExceededError(CANARY), "budget_exceeded", None),
        (RuntimeError(CANARY), "provider_error", None),
        # the outermost type wins; __context__ is never followed
        (
            _during(InternalServerError(CANARY, 503), httpx.ReadTimeout(CANARY)),
            "http_5xx",
            503,
        ),
        (
            _during(RuntimeError(CANARY), httpx.ReadTimeout(CANARY)),
            "provider_error",
            None,
        ),
        (
            _from(InternalServerError(CANARY, 502), httpx.ReadTimeout(CANARY)),
            "http_5xx",
            502,
        ),
    ],
)
def test_a_provider_exception_maps_to_a_closed_cause(
    exc: BaseException, cause: str, status: int | None
) -> None:
    assert outage_cause(exc) == (cause, status)
    assert cause in OUTAGE_CAUSES


def test_litellm_exceptions_map_as_expected() -> None:
    """The contract with the real provider library: its classes, as it builds them."""
    litellm = pytest.importorskip("litellm")
    kw = {"message": CANARY, "model": "m", "llm_provider": "openai"}
    expected = {
        "RateLimitError": ("http_4xx", 429),
        "AuthenticationError": ("http_4xx", 401),
        "NotFoundError": ("http_4xx", 404),
        "BadRequestError": ("http_4xx", 400),
        "ContextWindowExceededError": ("http_4xx", 400),
        "ContentPolicyViolationError": ("http_4xx", 400),
        "InternalServerError": ("http_5xx", 500),
        "ServiceUnavailableError": ("http_5xx", 503),
        "Timeout": ("timeout", None),
        "APIConnectionError": ("connection_error", None),
    }
    for name, want in expected.items():
        assert outage_cause(getattr(litellm, name)(**kw)) == want, name
    budget = litellm.BudgetExceededError(current_cost=2.0, max_budget=1.0)
    assert outage_cause(budget) == ("budget_exceeded", None)


def test_refusals_are_answers_and_configuration_not_unavailability() -> None:
    assert REFUSAL_CAUSES == {"http_4xx", "budget_exceeded", "max_tokens_exhausted"}
    assert REFUSAL_CAUSES <= OUTAGE_CAUSES


def test_an_exception_without_a_cause_on_its_record_is_classified_by_outcome() -> None:
    """Records built before the cause existed (and by tests) still give a closed code."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    plain = ProviderOutageError(ProviderCallRecord(now, None, now, "error"))
    assert plain.cause == "provider_error" and not plain.is_refusal
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
    exc = _from(Timeout(CANARY), httpx.ReadTimeout(CANARY))
    r, _ = _timed_runner(_Raising(exc))
    with pytest.raises(ProviderOutageError) as e:
        await r.run_part_b(_visible_bundle())
    assert (e.value.cause, e.value.expired_budget) == (
        "read_timeout",
        "provider_timeout",
    )
    assert e.value.record.status_code is None


async def test_the_second_part_b_attempt_is_recorded_on_its_outage() -> None:
    class SecondFails(TimedLLM):
        def __init__(self) -> None:
            super().__init__([])
            self.n = 0

        async def stream(self, *a: Any, **kw: Any) -> AsyncIterator[CompletionChunk]:  # type: ignore[override]
            self.n += 1
            if self.n == 2:
                raise InternalServerError(CANARY, 502)

            async def gen() -> AsyncIterator[CompletionChunk]:
                yield CompletionChunk(content="Il voto è 7.")  # breaks a Part B rule

            return gen()

    r, _ = _timed_runner(SecondFails())
    with pytest.raises(ProviderOutageError) as e:
        await r.run_part_b(_visible_bundle())
    assert (e.value.cause, e.value.attempt) == ("http_5xx", 2)


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


class _MidRaise(TimedLLM):
    """Text, then the provider's own ``exc`` from inside the stream."""

    def __init__(self, exc: BaseException) -> None:
        super().__init__([])
        self.exc = exc

    async def stream(self, *a: Any, **kw: Any) -> AsyncIterator[CompletionChunk]:  # type: ignore[override]
        outer = self

        async def gen() -> AsyncIterator[CompletionChunk]:
            yield CompletionChunk(content="Ciao. ")
            await asyncio.sleep(0)
            raise outer.exc

        return gen()


@pytest.mark.parametrize("path", ["part_b", "turn"])
async def test_a_provider_timeout_mid_stream_is_not_taken_for_a_stall(
    path: str,
) -> None:
    """Only the runner's own stall is ``stream_stall``; a provider ``TimeoutError`` subclass
    keeps its type name and is classified like any provider error."""
    r, _ = _timed_runner(_MidRaise(ServerTimeoutError(CANARY)), timeout=5)
    with pytest.raises(ProviderOutageError) as e:
        if path == "part_b":
            await r.run_part_b(_visible_bundle())
        else:
            await r.run(_visible_bundle(), StudentTurn("ciao", 0))
    rec = e.value.record
    assert (rec.error_class, rec.cause, rec.status_code) == (
        "ServerTimeoutError",
        "timeout",
        None,
    )


async def test_a_turn_open_failure_carries_its_cause_too() -> None:
    r, _ = _timed_runner(TimedLLM([(0, "x")], fail="open"))
    with pytest.raises(ProviderOutageError) as e:
        await r.run(_visible_bundle(), StudentTurn("ciao", 0))
    assert e.value.cause == "connection_error" and e.value.attempt is None


class _Scripted(TimedLLM):
    """Plays ``chunks`` (content, finish_reason) with no delay."""

    def __init__(self, chunks: list[tuple[str, str | None]]) -> None:
        super().__init__([])
        self.chunks = chunks

    async def stream(self, *a: Any, **kw: Any) -> AsyncIterator[CompletionChunk]:  # type: ignore[override]
        outer = self

        async def gen() -> AsyncIterator[CompletionChunk]:
            try:
                for text, finish in outer.chunks:
                    yield CompletionChunk(
                        content=text, done=finish is not None, finish_reason=finish
                    )
            finally:
                outer.closed = True

        return gen()


@pytest.mark.parametrize(
    ("chunks", "cause", "budget"),
    [
        (
            [("", None), ("", None), ("", "length")],
            "max_tokens_exhausted",
            "max_tokens",
        ),
        # whitespace before a length finish is no text either
        (
            [("", None), ("\n", None), (" ", "length")],
            "max_tokens_exhausted",
            "max_tokens",
        ),
        ([("", None), ("", "stop")], "empty_stream", None),
        ([("", None), ("", None)], "empty_stream", None),
    ],
)
async def test_a_stream_spent_on_reasoning_is_max_tokens_exhausted(
    chunks: list[tuple[str, str | None]], cause: str, budget: str | None
) -> None:
    llm = _Scripted(chunks)
    r, _ = _timed_runner(llm)
    with pytest.raises(ProviderOutageError) as e:
        await r.run_part_b(_visible_bundle())
    assert (e.value.cause, e.value.expired_budget) == (cause, budget)
    assert e.value.record.error_class == "EmptyStream" and llm.closed
    assert e.value.is_refusal is (cause == "max_tokens_exhausted")


@pytest.fixture
def worker_log() -> Iterator[io.StringIO]:
    from vektra_shared.log_safety import configure_assess_worker_logging

    buf = io.StringIO()
    configure_assess_worker_logging(buf, level=logging.DEBUG)
    yield buf
    structlog.reset_defaults()
    for name in ("", "vektra_assess"):
        lg = logging.getLogger(name)
        for h in [h for h in lg.handlers if getattr(h, "_vektra_assess", False)]:
            lg.removeHandler(h)
    logging.getLogger("vektra_assess").setLevel(logging.NOTSET)
    logging.getLogger().setLevel(logging.WARNING)


def _truncated(buf: io.StringIO) -> list[dict[str, Any]]:
    lines = [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]
    return [ln for ln in lines if ln.get("event") == "assess_llm_answer_truncated"]


@pytest.mark.parametrize("path", ["part_b", "turn"])
async def test_an_answer_cut_at_max_tokens_is_logged_and_still_delivered(
    path: str, worker_log: io.StringIO
) -> None:
    """finish_reason "length" after text: the answer is truncated. Delivery is unchanged (the
    post-check or the Part B rules decide); one warning names the cause, max_tokens and the
    remediation code."""
    words = PART_B_OK.split(" ")
    chunks = [(w + " ", None) for w in words[:-1]] + [(words[-1], "length")]
    r, _ = _timed_runner(_Scripted(chunks))
    if path == "part_b":
        out = await r.run_part_b(_visible_bundle())
    else:
        out = await r.run(_visible_bundle(), StudentTurn("ciao", 0))
    assert out.text
    (line,) = _truncated(worker_log)
    assert line["level"] == "warning" and line["kind"] == path
    assert line["outage_cause"] == "max_tokens_truncated"
    assert line["max_tokens"] == 600
    assert line["remediation_code"] == "raise_llm_max_tokens_or_lower_reasoning_effort"


async def test_a_complete_answer_logs_no_truncation(worker_log: io.StringIO) -> None:
    r, _ = _timed_runner(_Scripted([(PART_B_OK, "stop")]))
    await r.run_part_b(_visible_bundle())
    assert _truncated(worker_log) == []
