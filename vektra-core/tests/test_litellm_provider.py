"""Unit tests for LitellmProvider (ADR-0008)."""

from unittest.mock import AsyncMock, MagicMock, patch

from vektra_core.providers.litellm_provider import LitellmProvider
from vektra_shared.config import LLMConfig
from vektra_shared.types import CompletionResponse, Message


def _make_config(**kwargs) -> LLMConfig:
    defaults = {
        "VEKTRA_LLM_PROVIDER": "ollama/llama3",
        "VEKTRA_LLM_FALLBACK_TIMEOUT_MS": 5000,
    }
    defaults.update(kwargs)
    return LLMConfig.model_validate(defaults)


def _mock_usage():
    u = MagicMock()
    u.prompt_tokens = 10
    u.completion_tokens = 20
    u.total_tokens = 30
    return u


def _mock_completion_response(content: str = "Hello!") -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.model = "ollama/llama3"
    resp.usage = _mock_usage()
    return resp


async def test_complete_returns_completion_response():
    config = _make_config()
    provider = LitellmProvider(config)
    messages = [Message(role="user", content="Hi")]

    mock_resp = _mock_completion_response("Hello from LLM!")

    with patch(
        "vektra_core.providers.litellm_provider.litellm.acompletion",
        new=AsyncMock(return_value=mock_resp),
    ):
        result = await provider.complete(messages, model="ollama/llama3")

    assert isinstance(result, CompletionResponse)
    assert result.content == "Hello from LLM!"
    assert result.model == "ollama/llama3"
    assert result.prompt_tokens == 10
    assert result.total_tokens == 30


async def test_stream_yields_chunks():
    config = _make_config()
    provider = LitellmProvider(config)
    messages = [Message(role="user", content="Tell me something")]

    # Build a fake async response that yields streaming chunks
    async def _fake_stream_response(*args, **kwargs):
        for text, finish in [("Hello", None), (" world", None), ("", "stop")]:
            chunk = MagicMock()
            chunk.choices = [MagicMock()]
            chunk.choices[0].delta = MagicMock()
            chunk.choices[0].delta.content = text
            chunk.choices[0].finish_reason = finish
            yield chunk

    with patch(
        "vektra_core.providers.litellm_provider.litellm.acompletion",
        new=AsyncMock(return_value=_fake_stream_response()),
    ):
        stream = await provider.stream(messages, model="ollama/llama3")
        chunks = [c async for c in stream]

    assert len(chunks) == 3
    assert chunks[0].content == "Hello"
    assert chunks[0].done is False
    assert chunks[1].content == " world"
    assert chunks[2].done is True


async def test_health_check_healthy():
    config = _make_config()
    provider = LitellmProvider(config)
    mock_resp = _mock_completion_response("ok")

    with patch(
        "vektra_core.providers.litellm_provider.litellm.acompletion",
        new=AsyncMock(return_value=mock_resp),
    ):
        status = await provider.health_check()

    assert status.status == "healthy"
    assert status.latency_ms is not None
    assert status.latency_ms >= 0


async def test_health_check_unhealthy():
    config = _make_config()
    provider = LitellmProvider(config)

    with patch(
        "vektra_core.providers.litellm_provider.litellm.acompletion",
        new=AsyncMock(side_effect=ConnectionError("LLM unreachable")),
    ):
        status = await provider.health_check()

    assert status.status == "unhealthy"
    assert "LLM unreachable" in (status.message or "")


def test_count_tokens_returns_int():
    config = _make_config()
    provider = LitellmProvider(config)

    with patch(
        "vektra_core.providers.litellm_provider.litellm.token_counter", return_value=42
    ):
        result = provider.count_tokens("hello world", model="ollama/llama3")

    assert result == 42


def test_count_tokens_fallback_on_error():
    config = _make_config()
    provider = LitellmProvider(config)

    with patch(
        "vektra_core.providers.litellm_provider.litellm.token_counter",
        side_effect=Exception("model not found"),
    ):
        result = provider.count_tokens("hello world", model="unknown/model")

    # Fallback: len("hello world") // 4 = 11 // 4 = 2
    assert result >= 1


def test_model_name_property():
    config = _make_config(**{"VEKTRA_LLM_PROVIDER": "openai/gpt-4o"})
    provider = LitellmProvider(config)
    assert provider.model_name == "openai/gpt-4o"
