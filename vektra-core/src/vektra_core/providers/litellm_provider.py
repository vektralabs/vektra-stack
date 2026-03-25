"""LitellmProvider: LLMProvider implementation backed by litellm (ADR-0008).

Supports OpenAI, Anthropic, and Ollama via litellm model strings.
Implements LLMProvider Protocol (vektra_shared.protocols).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

import litellm
import structlog

from vektra_shared.config import LLMConfig
from vektra_shared.types import (
    CompletionChunk,
    CompletionResponse,
    HealthStatus,
    Message,
)

# Suppress litellm's verbose startup output
litellm.suppress_debug_info = True
litellm.drop_params = True  # silently ignore unsupported params per provider

log = structlog.get_logger(__name__)


class LitellmProvider:
    """LLMProvider backed by litellm, supporting multi-provider model strings.

    construct with LLMConfig; inject into SimpleQueryPipeline via constructor.
    """

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._base_kwargs: dict[str, Any] = {}
        if config.api_key:
            self._base_kwargs["api_key"] = config.api_key
        if config.api_base:
            self._base_kwargs["api_base"] = config.api_base
        if config.extra_body:
            self._base_kwargs["extra_body"] = config.extra_body

    @property
    def model_name(self) -> str:
        return self._config.provider

    async def complete(
        self,
        messages: list[Message],
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> CompletionResponse:
        """Non-streaming LLM completion."""
        response = await litellm.acompletion(
            model=model,
            messages=[{"role": m.role, "content": m.content} for m in messages],
            temperature=temperature,
            max_tokens=max_tokens,
            **self._base_kwargs,
            **kwargs,
        )
        choice = response.choices[0]
        usage = response.usage
        return CompletionResponse(
            content=choice.message.content or "",
            model=response.model or model,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            total_tokens=usage.total_tokens if usage else 0,
        )

    async def stream(
        self,
        messages: list[Message],
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[CompletionChunk]:
        """Return an async iterator that streams completion tokens."""
        return self._stream_impl(messages, model, temperature, max_tokens, **kwargs)

    async def _stream_impl(
        self,
        messages: list[Message],
        model: str,
        temperature: float,
        max_tokens: int | None,
        **kwargs: Any,
    ) -> AsyncGenerator[CompletionChunk, None]:
        response = await litellm.acompletion(
            model=model,
            messages=[{"role": m.role, "content": m.content} for m in messages],
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            **self._base_kwargs,
            **kwargs,
        )
        async for chunk in response:
            delta = chunk.choices[0].delta
            content = (delta.content or "") if delta else ""
            finish = chunk.choices[0].finish_reason
            yield CompletionChunk(content=content, done=finish is not None)

    async def health_check(self) -> HealthStatus:
        """Probe the primary model with a short completion (5-second timeout)."""
        start = time.monotonic()
        try:
            await asyncio.wait_for(
                litellm.acompletion(
                    model=self._config.provider,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=5,
                    **self._base_kwargs,
                ),
                timeout=5.0,
            )
            latency_ms = int((time.monotonic() - start) * 1000)
            return HealthStatus(status="healthy", latency_ms=latency_ms)
        except Exception as exc:
            log.warning(
                "llm_health_check_failed", model=self._config.provider, error=str(exc)
            )
            return HealthStatus(status="unhealthy", message=str(exc))

    def count_tokens(self, text: str, model: str) -> int:
        """Approximate token count using litellm's tokenizer (falls back to len//4)."""
        try:
            return int(litellm.token_counter(model=model, text=text))
        except Exception:
            return max(1, len(text) // 4)
