"""The turn-shaped provider probe, shared by the worker's outage tick and the API's refusal check.

``make_llm_probe`` is the one probe shape (moved here from ``worker.outage_probe``, which
re-exports it, so the API process may use it too: ``api`` must not import ``worker``).

``refusal_is_configuration`` tells a refusal of the DEPLOYMENT's configuration from one of a single
request (live E2E follow-up, review of #221). Every ``auth_error`` is the configuration (the key).
A non-429 4xx is ambiguous: a wrong model or a rejected parameter refuses every call, but a
context-length or content-policy 400 refuses only that request. So the probe, shaped like a turn
(the same model, parameters and extra body, one fixed word as the message), is called once: refused
for the configuration too, it is the configuration; answered, timed out or failing otherwise, the
refusal belongs to the request and the caller keeps the plain refusal path (no pause).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from vektra_assess.llm.types import ProviderOutageError, is_config_refusal

Probe = Callable[[], Awaitable[bool]]


def make_llm_probe(
    llm: Any,
    model: str,
    *,
    timeout_s: float,
    max_tokens: int,
    temperature: float,
    seed: int | None,
) -> Probe:
    """A probe call shaped like a discussion turn's (``DiscussionTurnRunner._open_stream``): the
    same provider method (``stream``), model, ``max_tokens``, ``temperature`` and ``seed``, and
    ``llm`` is the discussion provider itself, so its extra body (``reasoning_effort``, ...)
    reaches the probe as it reaches a turn. A provider that refuses those parameters refuses
    the probe too; only the messages differ (one fixed word, no session content).

    True as soon as the stream yields its first text, or when it completes without any (any
    finish reason); the stream is closed there, so the probe never reads a whole answer.
    ``timeout_s`` covers the open and the first text, like the turn's first-token deadline.
    """
    from vektra_shared.types import Message

    extra: dict[str, object] = {} if seed is None else {"seed": seed}

    async def probe() -> bool:
        async with asyncio.timeout(timeout_s):
            stream = await llm.stream(
                [Message(role="user", content="ping")],
                model,
                temperature=temperature,
                max_tokens=max_tokens,
                **extra,
            )
            it = stream.__aiter__()
            try:
                async for chunk in it:
                    if chunk.content.strip():
                        break
            finally:
                close = getattr(it, "aclose", None)
                if close is not None:
                    await close()
        return True

    return probe


async def refusal_is_configuration(
    exc: ProviderOutageError, probe: Probe | None
) -> bool:
    """Whether a refused call is the deployment's configuration (pause the session, say
    ``service_misconfigured``) rather than that one request (the plain refusal).

    Not a configuration refusal at all (a 429, ``budget_exceeded``, ``max_tokens_exhausted``,
    an outage): False, the probe is not called. ``auth_error``: True, no probe needed. A
    non-429 4xx: True only when the probe is refused for the configuration as well; without a
    probe it cannot be confirmed, so False (fail towards the old behaviour, never a pause)."""
    if not exc.is_config_refusal:
        return False
    if exc.cause == "auth_error":
        return True
    if probe is None:
        return False
    from vektra_assess.llm.runner import outage_cause

    try:
        await probe()
    except Exception as probe_exc:  # an answer about the provider, never a crash
        cause, status = outage_cause(probe_exc)
        return is_config_refusal(cause, status)
    return False
