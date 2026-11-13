"""Value types of the discussion runner (plan assess-llm-runner, task A).

Every type is a frozen dataclass and content-free where it can be: a ``ProviderCallRecord`` or a
``TurnUsage`` carries timings, counts and classes, never text. ``TurnResult.text`` is the delivered
text, i.e. what the post-check released, and ``content_hash`` is its SHA-256 (plan 10 re-hashes the
transcript against it).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, get_args
from uuid import UUID, uuid4

from vektra_shared.types import QueryTrace

Role = Literal["student", "assistant"]
CallOutcome = Literal["ok", "error", "first_token_timeout", "cancelled"]
ContestStatus = Literal["proposed", "confirmed", "corrected"]


class RagNotAvailableError(RuntimeError):
    """A discussion asked for retrieval: RAG is OFF in v1 and the runner fails closed (NFR-015)."""


#: why a provider call ended in an outage: a CLOSED set of codes, safe to log and to audit. Derived
#: from the outcome, the exception's type names and its HTTP status, never from its message.
OutageCause = Literal[
    "first_token_timeout",  # no text within VEKTRA_ASSESS_OUTAGE_TIMEOUT_S of the open
    "stream_stall",  # text came, then no chunk within VEKTRA_ASSESS_OUTAGE_TIMEOUT_S
    "empty_stream",  # the stream ended before any text, for no stated reason
    # the stream ended with finish_reason "length" before any text: a reasoning model spent
    # VEKTRA_ASSESS_LLM_MAX_TOKENS on reasoning. A configuration problem, not a provider outage
    "max_tokens_exhausted",
    "connect_timeout",  # the HTTP client's timeouts (httpx), below the provider library
    "read_timeout",
    "write_timeout",
    "pool_timeout",
    "timeout",  # a timeout whose kind the exception chain does not say
    "connection_error",
    # the credentials are refused (401/403: AuthenticationError, PermissionDeniedError), or none
    # is configured at all: litellm then raises AuthenticationError with a synthetic status 500.
    # Decided by the exception TYPE, never by the status litellm attaches
    "auth_error",
    # the provider refused the request (NotFound, BadRequest, UnprocessableEntity,
    # ContentPolicyViolation, RateLimit 429, or any other answered 4xx): wrong model, rejected
    # parameter or content, rate limit
    "http_4xx",
    "http_5xx",
    "budget_exceeded",  # the provider library's own spend limit (litellm), no HTTP answer
    "provider_error",  # any other provider exception
    # NOT a failure: a completed answer cut at max_tokens (finish_reason "length"). Logged as
    # a warning; the caller's own checks decide whether the text is usable
    "max_tokens_truncated",
]
OUTAGE_CAUSES: frozenset[str] = frozenset(get_args(OutageCause))

#: causes that are the provider (or its library) ANSWERING, or the configuration, not the
#: provider being unavailable: a discussion turn takes the refusal path for them and the session
#: never enters OUTAGE (REQ-086). A 429 is ``http_4xx`` and stays a refusal, as before.
REFUSAL_CAUSES: frozenset[str] = frozenset(
    {"auth_error", "http_4xx", "budget_exceeded", "max_tokens_exhausted"}
)

#: the closed remediation code of a spent max_tokens (docs/reference/configuration.md,
#: "Reasoning models (assess)")
MAX_TOKENS_REMEDIATION = "raise_llm_max_tokens_or_lower_reasoning_effort"
_MAX_TOKENS_CAUSES = frozenset({"max_tokens_exhausted", "max_tokens_truncated"})


def remediation_code(cause: str) -> str | None:
    return MAX_TOKENS_REMEDIATION if cause in _MAX_TOKENS_CAUSES else None


#: which budget a cause means expired: Vektra's own outage timeout, or the provider client's
_BUDGET: dict[str, str] = {
    "first_token_timeout": "outage_timeout",
    "stream_stall": "outage_timeout",
    "connect_timeout": "provider_timeout",
    "read_timeout": "provider_timeout",
    "write_timeout": "provider_timeout",
    "pool_timeout": "provider_timeout",
    "timeout": "provider_timeout",
    "max_tokens_exhausted": "max_tokens",
}


class ProviderOutageError(RuntimeError):
    """The provider failed, or sent no first token in time (REQ-086). Carries the call record.

    ``cause`` is a closed code (``OutageCause``), ``attempt`` the Part B generation it ended
    (1 or 2; None for a discussion turn). Neither carries any text of the call.
    """

    def __init__(self, record: ProviderCallRecord, attempt: int | None = None) -> None:
        super().__init__(record.outcome)
        self.record = record
        self.attempt = attempt

    @property
    def cause(self) -> str:
        if self.record.cause is not None and self.record.cause in OUTAGE_CAUSES:
            return self.record.cause
        if self.record.outcome == "first_token_timeout":
            return "first_token_timeout"
        return "provider_error"

    @property
    def is_refusal(self) -> bool:
        """The provider answered (or the configuration is wrong): not an outage (REQ-086)."""
        return self.cause in REFUSAL_CAUSES

    @property
    def expired_budget(self) -> str | None:
        """``outage_timeout`` (Vektra's), ``provider_timeout`` (the HTTP client's),
        ``max_tokens`` (the completion budget, spent before any text) or None."""
        return _BUDGET.get(self.cause)

    def log_fields(self, max_tokens: int | None) -> dict[str, Any]:
        """The content-free fields every failed-call log line carries (pre-flight and turns),
        all under allowlisted keys: codes, a type name, a status, timings, the budget."""
        rec = self.record
        first_token_ms = (
            None
            if rec.first_token_at is None
            else int((rec.first_token_at - rec.started_at).total_seconds() * 1000)
        )
        return {
            "outage_cause": self.cause,
            "error_type": rec.error_class,
            "http_status": rec.status_code,
            "outcome": rec.outcome,
            "expired_budget": self.expired_budget,
            "attempt": self.attempt,
            "duration_ms": rec.duration_ms,  # the failed call, open to failure
            "first_token_ms": first_token_ms,
            "max_tokens": max_tokens,
            "remediation_code": remediation_code(self.cause),
        }


class PartBRejectedError(RuntimeError):
    """Part B broke a Part B rule twice (REQ-094). ``reason`` is a content-free code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PriorTurn:
    """One earlier message of the discussion, as delivered (never the raw model output)."""

    role: Role
    text: str


@dataclass(frozen=True)
class StudentTurn:
    text: str
    turn_index: int
    history: tuple[PriorTurn, ...] = ()
    ui_language: str = "it"
    #: the turn's id, also the QueryTrace ``response_id``; plan 8 passes the persisted one
    turn_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True)
class TurnUsage:
    """Token counts from ``LLMProvider.count_tokens`` (the stream reports none): estimates."""

    prompt_tokens: int
    completion_tokens: int
    estimated: bool = True
    cost: float | None = None  # null without an operator price table (on-prem models)


@dataclass(frozen=True)
class ProviderCallRecord:
    started_at: datetime
    first_token_at: datetime | None
    ended_at: datetime
    outcome: CallOutcome
    error_class: str | None = None  # exception type name only, never its message
    cause: str | None = None  # an ``OutageCause`` code when the call failed
    status_code: int | None = (
        None  # the provider's HTTP status, when the exception had one
    )

    @property
    def duration_ms(self) -> int:
        return int((self.ended_at - self.started_at).total_seconds() * 1000)


@dataclass(frozen=True)
class ContestEntry:
    """One ``[[PUNTO_CONTESTATO ...]]`` marker whose label resolved in the bundle."""

    label: str
    status: ContestStatus


@dataclass(frozen=True)
class ReleasedSegment:
    """A unit of text that passed every post-check stage and may reach the student."""

    text: str
    marked: bool = False  # a replacement or a removal happened inside it


@dataclass(frozen=True)
class TurnResult:
    text: str
    marks: tuple[str, ...]  # post-check stage codes that replaced or removed something
    behaviour_class: str | None
    contest_entries: tuple[ContestEntry, ...]
    language: str
    usage: TurnUsage
    call_records: tuple[ProviderCallRecord, ...]
    replaced_count: int
    content_hash: str
    # cancelled by the caller: ``text`` is only what had been released
    interrupted: bool = False
    #: the content-free trace written for this turn (None when trace storage is off)
    trace: QueryTrace | None = None


class TraceWriter(Protocol):
    """Where the runner's content-free QueryTrace goes; vektra_app adapts it (no analytics import)."""

    async def write(self, trace: QueryTrace, namespace: str) -> None: ...


class AuditSink(Protocol):
    """Content-free audit rows (event name, codes, counts): the worker and API hooks match it."""

    async def __call__(self, event: str, data: Mapping[str, Any]) -> None: ...


class RunnerMetrics(Protocol):
    """Production-tier counters (ADR-0019 tier 3): content-free label values only.

    vektra_app supplies a Prometheus implementation in the API; the runner never imports a
    metrics library and works without one.
    """

    def replacement(self, stage: str, count: int) -> None: ...
    def behaviour_class(self, code: str) -> None: ...
    def call_outcome(self, outcome: str) -> None: ...
    def part_b_rejected(self, code: str) -> None: ...
