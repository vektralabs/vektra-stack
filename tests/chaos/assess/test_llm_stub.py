"""The fault-injecting LLM stub behaves as each mode says, through the platform's LLM client.

The calls go through ``litellm`` (the same library and ``openai/`` route the assess runner uses), not
through a hand-written HTTP client, so a mode that does not reach the client as a timeout or a
provider error fails here.
"""

from __future__ import annotations

import time

import litellm
import pytest
from llm_stub import REPLY, LlmStub

pytestmark = pytest.mark.integration


async def _call(stub: LlmStub, timeout: float = 10.0, stream: bool = False) -> str:
    resp = await litellm.acompletion(
        model="openai/stub",
        api_base=stub.base_url,
        api_key="not-a-key",
        messages=[{"role": "user", "content": "x"}],
        timeout=timeout,
        num_retries=0,
        stream=stream,
    )
    if stream:
        parts = []
        async for chunk in resp:  # type: ignore[union-attr]
            parts.append(chunk.choices[0].delta.content or "")
        return "".join(parts).strip()
    return str(resp.choices[0].message.content)  # type: ignore[union-attr]


async def test_ok_answers_at_once_streamed_and_not() -> None:
    stub = await LlmStub().start()
    try:
        t = time.monotonic()
        assert await _call(stub) == REPLY
        assert await _call(stub, stream=True) == REPLY
        assert time.monotonic() - t < 2.0
        assert stub.requests["ok"] == 2
    finally:
        await stub.stop()


async def test_latency_delays_the_reply() -> None:
    stub = await LlmStub(mode="latency", delay_s=1.5).start()
    try:
        t = time.monotonic()
        assert await _call(stub) == REPLY
        assert time.monotonic() - t >= 1.5
    finally:
        await stub.stop()


async def test_5xx_reaches_the_client_as_a_provider_error() -> None:
    stub = await LlmStub(mode="5xx").start()
    try:
        with pytest.raises(Exception) as exc:
            await _call(stub)
        assert "503" in str(exc.value) or "overloaded" in str(exc.value)
        assert stub.requests["5xx"] >= 1
    finally:
        await stub.stop()


async def test_stall_is_ended_only_by_the_client_timeout() -> None:
    stub = await LlmStub(mode="stall").start()
    try:
        t = time.monotonic()
        with pytest.raises(Exception) as exc:
            await _call(stub, timeout=2.0)
        assert 1.5 <= time.monotonic() - t < 10.0
        assert (
            "timeout" in type(exc.value).__name__.lower()
            or "timed out" in str(exc.value).lower()
        )
        assert stub.requests["stall"] == 1
    finally:
        await stub.stop()


async def test_the_mode_switches_while_running() -> None:
    """A scenario flips the fault mid-run: 5xx first, then healthy again."""
    stub = await LlmStub(mode="5xx").start()
    try:
        with pytest.raises(Exception, match=r"503|overloaded"):
            await _call(stub)
        stub.mode = "ok"
        assert await _call(stub) == REPLY
        assert stub.requests["5xx"] >= 1 and stub.requests["ok"] == 1
    finally:
        await stub.stop()
