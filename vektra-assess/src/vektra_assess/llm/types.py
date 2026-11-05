"""Value types of the discussion runner (plan assess-llm-runner, task A).

Every type is a frozen dataclass and content-free where it can be: a ``ProviderCallRecord`` or a
``TurnUsage`` carries timings, counts and classes, never text. ``TurnResult.text`` is the delivered
text, i.e. what the post-check released, and ``content_hash`` is its SHA-256 (plan 10 re-hashes the
transcript against it).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from vektra_shared.types import QueryTrace

Role = Literal["student", "assistant"]
CallOutcome = Literal["ok", "error", "first_token_timeout", "cancelled"]
ContestStatus = Literal["proposed", "confirmed", "corrected"]


class RagNotAvailableError(RuntimeError):
    """A discussion asked for retrieval: RAG is OFF in v1 and the runner fails closed (NFR-015)."""


class ProviderOutageError(RuntimeError):
    """The provider failed, or sent no first token in time (REQ-086). Carries the call record."""

    def __init__(self, record: ProviderCallRecord) -> None:
        super().__init__(record.outcome)
        self.record = record


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


class TraceWriter(Protocol):
    """Where the runner's content-free QueryTrace goes; vektra_app adapts it (no analytics import)."""

    async def write(self, trace: QueryTrace, namespace: str) -> None: ...


class AuditSink(Protocol):
    """Content-free audit rows (event name, codes, counts): the worker and API hooks match it."""

    async def __call__(self, event: str, data: Mapping[str, Any]) -> None: ...
