"""``llm.probe.refusal_is_configuration``: which refusals pause a session (review of #221, 1).

Every ``auth_error`` is the configuration. A non-429 4xx pauses only when the turn-shaped probe is
refused for the configuration too: a context-length or content-policy 400 refuses that request
only, and must keep the plain refusal (no OUTAGE, no ``service_misconfigured``). A 429, the
library's spend limit and a spent ``max_tokens`` never reach the probe.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from vektra_assess.llm.probe import refusal_is_configuration
from vektra_assess.llm.types import ProviderCallRecord, ProviderOutageError


def refused(cause: str, status: int | None) -> ProviderOutageError:
    now = datetime.now(UTC)
    return ProviderOutageError(
        ProviderCallRecord(now, None, now, "error", "X", cause, status)
    )


class _Resp:
    def __init__(self, status: int) -> None:
        self.status_code = status


def _provider_error(name: str, status: int) -> Exception:
    exc = type(name, (Exception,), {})("refused")
    exc.response = _Resp(status)  # type: ignore[attr-defined]
    return exc


class Probe:
    def __init__(self, outcome: Exception | None = None) -> None:
        self.outcome = outcome
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        if self.outcome is not None:
            raise self.outcome
        return True


def decide(exc: ProviderOutageError, probe: Probe | None) -> bool:
    return asyncio.run(refusal_is_configuration(exc, probe))


def test_an_auth_error_is_the_configuration_without_a_probe() -> None:
    probe = Probe()
    assert decide(refused("auth_error", 401), probe) is True
    assert decide(refused("auth_error", None), None) is True
    assert probe.calls == 0


@pytest.mark.parametrize(
    ("cause", "status"),
    [
        ("http_4xx", 429),  # transient rate limit
        ("budget_exceeded", None),  # the library's own spend limit
        ("max_tokens_exhausted", None),  # reasoning spent the budget
        ("max_tokens_truncated", None),
        ("http_5xx", 503),
        ("connection_error", None),
    ],
)
def test_other_failures_never_reach_the_probe(cause: str, status: int | None) -> None:
    probe = Probe(_provider_error("NotFoundError", 404))
    assert decide(refused(cause, status), probe) is False
    assert probe.calls == 0


def test_a_request_specific_400_is_not_the_configuration() -> None:
    """The probe is answered: the 400 was that request's (context length, content policy)."""
    probe = Probe()
    assert decide(refused("http_4xx", 400), probe) is False
    assert probe.calls == 1


@pytest.mark.parametrize(
    "probe_error",
    [
        _provider_error("NotFoundError", 404),  # the model does not exist
        _provider_error("BadRequestError", 400),  # a rejected parameter
        _provider_error("AuthenticationError", 401),
    ],
)
def test_a_4xx_the_probe_gets_too_is_the_configuration(probe_error: Exception) -> None:
    probe = Probe(probe_error)
    assert decide(refused("http_4xx", 404), probe) is True
    assert probe.calls == 1


@pytest.mark.parametrize(
    "probe_error",
    [
        _provider_error("RateLimitError", 429),
        _provider_error("ServiceUnavailableError", 503),
        TimeoutError(),
        ConnectionError("down"),
    ],
)
def test_a_probe_failing_otherwise_does_not_confirm(probe_error: Exception) -> None:
    assert decide(refused("http_4xx", 400), Probe(probe_error)) is False


def test_without_a_probe_a_4xx_cannot_be_confirmed() -> None:
    assert decide(refused("http_4xx", 404), None) is False
