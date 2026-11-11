"""Recording test doubles for the provider Protocols (plan assess-context, task group A).

These are **test assets**, in the dev-only ``vektra-testkit`` distribution (plan task 3.1) so that
every package's suite (and the eval adapter) can use the same doubles while the production wheel
and image carry none of them. They must never be registered in a ``ProviderRegistry``: a
production registry refuses any ``Recording*`` instance, and the startup steps assert that no
registered provider is one.

What they are for: BR-011 / NFR-017 say excluded material must be *absent* from model input, and
NFR-015 says RAG OFF means zero retrieval calls. Both are proven on bytes and on call counts, not
on self-reported status fields:

- ``RecordingLLMProvider`` keeps the exact request bytes of every call (canonical JSON of the
  messages and the parameters) and plays scripted responses;
- ``RecordingVectorStoreProvider`` records every call of the full Protocol and, built with
  ``forbid_calls=True``, raises on any use;
- ``RecordingSafeguardHook`` records the three trust-boundary calls and allows everything;
- ``find_canaries`` / ``assert_no_canaries`` search any mix of str, bytes and nested containers.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from vektra_shared.types import (
    ChunkEmbedding,
    CompletionChunk,
    CompletionResponse,
    HealthStatus,
    Message,
    QueryEmbedding,
    SafeguardContext,
    SafeguardResult,
    SearchFilters,
    SearchMode,
    SearchResult,
    StoredChunk,
)

RECORDER_CLASS_PREFIX = "Recording"
#: class attribute every test double carries; ``ProviderRegistry`` refuses any instance whose class
#: has it, subclasses included (one predicate for both checks: ``is_recorder``)
TEST_DOUBLE_MARKER = "__vektra_test_double__"


class _TestDouble:
    __vektra_test_double__ = True


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_jsonable(v) for v in value]
        return sorted(items, key=repr) if isinstance(value, (set, frozenset)) else items
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def canonical_bytes(payload: Any) -> bytes:
    """Deterministic UTF-8 JSON (sorted keys, no whitespace, no ASCII escaping)."""
    return json.dumps(
        _jsonable(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


# --- LLM ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordedLLMCall:
    method: str  # "complete" | "stream" | "count_tokens"
    request_bytes: bytes


class ScriptExhausted(AssertionError):
    """More calls than scripted responses: the test under-specified its script."""


class RecordingLLMProvider(_TestDouble):
    """``LLMProvider`` that records the exact request bytes and plays scripted responses."""

    def __init__(
        self,
        responses: Sequence[str] = (),
        *,
        model: str = "recording-model",
        repeat_last: bool = False,
    ) -> None:
        self._responses = list(responses)
        self._model = model
        self._repeat_last = repeat_last
        self._served = 0
        self.calls: list[RecordedLLMCall] = []

    # the bytes a real provider would put on the wire, in a stable form
    @staticmethod
    def request_bytes(
        messages: list[Message],
        model: str,
        temperature: float,
        max_tokens: int | None,
        kwargs: Mapping[str, Any],
    ) -> bytes:
        return canonical_bytes(
            {
                "messages": [{"role": m.role, "content": m.content} for m in messages],
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "params": dict(kwargs),
            }
        )

    def _next(self) -> str:
        if self._served < len(self._responses):
            out = self._responses[self._served]
        elif self._repeat_last and self._responses:
            out = self._responses[-1]
        else:
            raise ScriptExhausted(
                f"call {self._served + 1} but only {len(self._responses)} response(s) scripted"
            )
        self._served += 1
        return out

    @property
    def all_request_bytes(self) -> list[bytes]:
        return [c.request_bytes for c in self.calls if c.method != "count_tokens"]

    async def complete(
        self,
        messages: list[Message],
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> CompletionResponse:
        self.calls.append(
            RecordedLLMCall(
                "complete",
                self.request_bytes(messages, model, temperature, max_tokens, kwargs),
            )
        )
        text = self._next()
        prompt = sum(len(m.content.split()) for m in messages)
        completion = len(text.split())
        return CompletionResponse(
            content=text,
            model=model or self._model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
        )

    async def stream(
        self,
        messages: list[Message],
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[CompletionChunk]:
        # recorded when the stream is opened, as a real provider sends the request then
        self.calls.append(
            RecordedLLMCall(
                "stream",
                self.request_bytes(messages, model, temperature, max_tokens, kwargs),
            )
        )
        text = self._next()

        async def chunks() -> AsyncIterator[CompletionChunk]:
            words = text.split(" ")
            for i, word in enumerate(words):
                yield CompletionChunk(
                    content=word if i == 0 else " " + word, model=model
                )
            yield CompletionChunk(content="", done=True, model=model)

        return chunks()

    async def health_check(self) -> HealthStatus:
        return HealthStatus(status="healthy", message="recording")

    def count_tokens(self, text: str, model: str) -> int:
        self.calls.append(
            RecordedLLMCall(
                "count_tokens", canonical_bytes({"text": text, "model": model})
            )
        )
        return len(text.split())


# --- vector store ------------------------------------------------------------------------------


class ForbiddenProviderCall(AssertionError):
    """A provider built with ``forbid_calls=True`` was used (NFR-015: RAG OFF means 0 calls)."""


@dataclass(frozen=True)
class RecordedCall:
    method: str
    args: Mapping[str, Any] = field(default_factory=dict)


class RecordingVectorStoreProvider(_TestDouble):
    """Full ``VectorStoreProvider`` that records every call; ``forbid_calls`` makes any use raise.

    The call is recorded BEFORE the raise, so a caller that swallows the exception is still
    caught by ``calls`` being non-empty.
    """

    def __init__(
        self, *, forbid_calls: bool = False, active_index_version: int = 1
    ) -> None:
        self.forbid_calls = forbid_calls
        self.active_index_version = active_index_version
        self.calls: list[RecordedCall] = []

    def _record(self, method: str, **args: Any) -> None:
        self.calls.append(RecordedCall(method, args))
        if self.forbid_calls:
            raise ForbiddenProviderCall(
                f"vector store {method}() called with forbid_calls=True"
            )

    async def store(
        self,
        namespace: str,
        chunks: Sequence[ChunkEmbedding],
        index_version: int | None = None,
    ) -> list[str]:
        self._record(
            "store", namespace=namespace, n=len(chunks), index_version=index_version
        )
        return [f"chunk-{i}" for i in range(len(chunks))]

    async def search(
        self,
        namespace: str,
        query_embedding: QueryEmbedding,
        top_k: int,
        search_mode: SearchMode = SearchMode.DENSE,
        filters: SearchFilters | None = None,
        raw_filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        self._record(
            "search", namespace=namespace, top_k=top_k, search_mode=search_mode
        )
        return []

    async def retrieve(
        self, namespace: str, chunk_ids: list[str]
    ) -> list[SearchResult]:
        self._record("retrieve", namespace=namespace, n=len(chunk_ids))
        return []

    async def list_chunks(self, namespace: str, document_id: UUID) -> list[StoredChunk]:
        self._record("list_chunks", namespace=namespace, document_id=str(document_id))
        return []

    async def count_chunks(self, namespace: str | None = None) -> int:
        self._record("count_chunks", namespace=namespace)
        return 0

    async def delete(self, namespace: str, ids: list[str]) -> int:
        self._record("delete", namespace=namespace, n=len(ids))
        return 0

    async def delete_index_version(self, namespace: str, index_version: int) -> int:
        self._record(
            "delete_index_version", namespace=namespace, index_version=index_version
        )
        if index_version == self.active_index_version:
            from vektra_shared.errors import ActiveIndexVersionError

            raise ActiveIndexVersionError(namespace, index_version)
        return 0

    async def health_check(self) -> HealthStatus:
        # health is not a retrieval call; recording it would make RAG OFF assertions noisy
        return HealthStatus(status="healthy", message="recording")


# --- safeguards --------------------------------------------------------------------------------


class RecordingSafeguardHook(_TestDouble):
    """``SafeguardHook`` that records the three trust-boundary calls and allows everything."""

    def __init__(self) -> None:
        self.calls: list[RecordedCall] = []

    async def pre_query(
        self, query_ref: str, context: SafeguardContext
    ) -> SafeguardResult:
        self.calls.append(RecordedCall("pre_query", {"query_ref": query_ref}))
        return SafeguardResult()

    async def post_retrieval(
        self, query_ref: str, results: list[SearchResult], context: SafeguardContext
    ) -> SafeguardResult:
        self.calls.append(
            RecordedCall("post_retrieval", {"query_ref": query_ref, "n": len(results)})
        )
        return SafeguardResult()

    async def pre_response(
        self, response_ref: str, context: SafeguardContext
    ) -> SafeguardResult:
        self.calls.append(RecordedCall("pre_response", {"response_ref": response_ref}))
        return SafeguardResult()


# --- canary search -----------------------------------------------------------------------------


def _texts(blob: Any) -> Iterable[str]:
    if blob is None:
        return
    if isinstance(blob, str):
        yield blob
    elif isinstance(blob, (bytes, bytearray, memoryview)):
        yield bytes(blob).decode("utf-8", errors="replace")
    elif dataclasses.is_dataclass(blob) and not isinstance(blob, type):
        for f in dataclasses.fields(blob):
            yield from _texts(getattr(blob, f.name))
    elif isinstance(blob, Mapping):
        for k, v in blob.items():
            yield from _texts(k)
            yield from _texts(v)
    elif isinstance(blob, Iterable):
        for item in blob:
            yield from _texts(item)
    elif hasattr(
        blob, "model_dump"
    ):  # a Pydantic model, without importing pydantic here
        yield from _texts(blob.model_dump(mode="json"))
    else:
        yield str(blob)


def find_canaries(blobs: Any, canaries: Iterable[str]) -> dict[str, int]:
    """Occurrences of each canary across every string reachable from ``blobs`` (0s included).

    A numeric canary (a mark such as ``7.31411``) counts only as a whole number: not preceded or
    followed by another digit, nor preceded by a decimal point, nor followed by an exponent
    (``7.31411e-06`` is another value). Otherwise any float that happens to contain those digits
    (a latency of ``17.3141123`` in a trace) reads as a leak. The pattern is the escaped canary
    between fixed lookarounds, so it cannot backtrack.
    """
    wanted = list(dict.fromkeys(canaries))
    counts = dict.fromkeys(wanted, 0)
    numeric = {
        c: re.compile(rf"(?<![\d.]){re.escape(c)}(?!\d|[eE][+-]?\d)")
        for c in wanted
        if _NUMERIC.fullmatch(c)
    }
    for text in _texts(blobs):
        for c in wanted:
            rx = numeric.get(c)
            counts[c] += len(rx.findall(text)) if rx else text.count(c)
    return counts


_NUMERIC = re.compile(r"\d+(?:\.\d+)?")


def assert_no_canaries(
    blobs: Any, canaries: Iterable[str], *, sink: str = "sink"
) -> None:
    """Fail on any canary in ``blobs``. The message names the canaries, never the content."""
    found = {c: n for c, n in find_canaries(blobs, canaries).items() if n}
    if found:
        raise AssertionError(
            f"{sink}: {sum(found.values())} canary hit(s): {sorted(found)}"
        )


def is_recorder(obj: object) -> bool:
    """True for an instance of a test double (the startup registry guard)."""
    return bool(getattr(type(obj), TEST_DOUBLE_MARKER, False))
