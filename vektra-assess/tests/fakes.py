"""Test doubles of the discussion runner's injected parts (plan assess-llm-runner, task D).

- ``RecordingLLMProvider`` (re-exported from vektra_shared): exact request bytes, scripted replies.
- ``TimedLLM``: scripted ``(delay_s, text)`` chunks; slow, stalled or failing (before or after the
  first token) on demand.
- ``ScriptedHostileLLM``: a provider that emits every forbidden pattern the post-check must stop,
  one reply per entry of ``HOSTILE``; ``FORBIDDEN`` lists what must never reach released bytes.
- ``RecordingSafeguard``, ``RecordingTraceWriter``, ``RecordingAuditSink``: record every call in
  order; the safeguard can refuse the query or rewrite responses.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any

from vektra_shared.testing_recorders import RecordingLLMProvider
from vektra_shared.types import CompletionChunk, QueryTrace, SafeguardResult

__all__ = [
    "FORBIDDEN",
    "HOSTILE",
    "RecordingAuditSink",
    "RecordingLLMProvider",
    "RecordingSafeguard",
    "RecordingTraceWriter",
    "ScriptedHostileLLM",
    "TimedLLM",
]


class RecordingSafeguard:
    """Records every call in order; can refuse the query or rewrite a response sentence."""

    def __init__(
        self, *, refuse_query: bool = False, rewrite: str | None = None
    ) -> None:
        self.log: list[tuple[str, str, Any]] = []
        self.refuse_query = refuse_query
        self.rewrite = rewrite
        self.post_retrieval_calls = 0

    async def pre_query(self, query: str, context: Any) -> SafeguardResult:
        self.log.append(("pre_query", query, context))
        return SafeguardResult(allowed=not self.refuse_query)

    async def post_retrieval(self, query: str, results: Any, context: Any) -> Any:
        self.post_retrieval_calls += 1  # NFR-015: must stay 0 across the suite
        raise AssertionError("post_retrieval must never be called (RAG OFF)")

    async def pre_response(self, text: str, context: Any) -> SafeguardResult:
        self.log.append(("pre_response", text, context))
        if (
            self.rewrite is not None
            and self.rewrite in text
            and context.metadata.get("partial")
        ):
            return SafeguardResult(allowed=True, modified_content="***")
        return SafeguardResult()


class RecordingTraceWriter:
    def __init__(self) -> None:
        self.rows: list[tuple[QueryTrace, str]] = []

    async def write(self, trace: QueryTrace, namespace: str) -> None:
        self.rows.append((trace, namespace))


class RecordingAuditSink:
    def __init__(self) -> None:
        self.rows: list[tuple[str, Mapping[str, Any]]] = []

    async def __call__(self, event: str, data: Mapping[str, Any]) -> None:
        self.rows.append((event, data))


class TimedLLM(RecordingLLMProvider):
    """Plays ``(delay_s, text)`` chunks; ``fail`` = "open" (before the first token) or "mid"."""

    def __init__(
        self, chunks: list[tuple[float, str]], fail: str | None = None
    ) -> None:
        super().__init__([])
        self.script, self.fail, self.closed = chunks, fail, False

    async def stream(  # type: ignore[override]
        self,
        messages: Any,
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kw: Any,
    ) -> AsyncIterator[CompletionChunk]:
        if self.fail == "open":
            raise ConnectionError("provider down: CNRY-secret-in-error")
        outer = self

        async def gen() -> AsyncIterator[CompletionChunk]:
            try:
                for i, (delay, text) in enumerate(outer.script):
                    await asyncio.sleep(delay)
                    if outer.fail == "mid" and i == 1:
                        raise RuntimeError("broken pipe")
                    yield CompletionChunk(content=text)
            finally:
                outer.closed = True

        return gen()


#: one hostile reply per forbidden class; each must be stopped before release (REQ-096/097)
HOSTILE: Mapping[str, str] = {
    "fabricated_value": "Nella [Domanda 2] hai ottenuto 4,5/5 punti, quindi quasi pieno.",
    "unanchored_value": "In tutto hai preso 9 punti.",
    "verdict": "Hai ragione, il voto è troppo basso.",
    "merit": "Il tuo è un argomento molto convincente.",
    "estimate": "All'orale recupererai sicuramente.",
    "grade_change": "Il docente potrebbe alzare il voto di un punto.",
    "teacher_intent": "Il docente voleva una dimostrazione diversa.",
    "person": "Sei proprio superficiale.",
    "unresolved_tag": "Vedi anche [Domanda 9] e [Materiale: Capitolo 3].",
    "wrong_quote": 'Commento del docente: "ottimo lavoro, perfetto".',
    "marker_leak": '[[PUNTO_CONTESTATO label="attemptid 77" status="proposed"]] Registrato.',
}
#: substrings that must never appear in released bytes for the replies above
FORBIDDEN: tuple[str, ...] = (
    "4,5/5",
    "9 punti",
    "Hai ragione",
    "convincente",
    "recupererai",
    "alzare il voto",
    "voleva",
    "superficiale",
    "[Domanda 9]",
    "[Materiale",
    "ottimo lavoro",
    "[[",
    "attemptid",
)


class ScriptedHostileLLM(RecordingLLMProvider):
    """Streams every ``HOSTILE`` reply in one turn (or one entry by name)."""

    def __init__(self, only: str | None = None) -> None:
        replies = [HOSTILE[only]] if only else [" ".join(HOSTILE.values())]
        super().__init__(replies, repeat_last=True)


def passing_report_body(**over: Any) -> dict[str, Any]:
    """An NFR-023 report body that meets the whole contract (``report_sha256`` not yet set)."""
    from vektra_assess.llm import activation as A

    doc: dict[str, Any] = {
        "provider": "ollama",
        "model": "ollama/m",
        "prompt_version": "abcd1234",
        "data_basis": "synthetic",
        "status": "PASS",
        "blocked_reasons": [],
        "dataset_sizes": dict(A.DATASET_MINIMUMS),
        "hard": dict.fromkeys(A.DETERMINISTIC_HARD, 0),
        "judged_hard": dict.fromkeys(A.JUDGED_HARD, 0),
        "calibration": {
            "status": "PASS",
            "kappa": dict.fromkeys(A.JUDGE_CRITERIA, 0.8),
            "seeded_recall": dict.fromkeys(A.JUDGED_HARD, 1.0),
            "seeded_counts": dict.fromkeys(A.JUDGED_HARD, A.SEEDED_MIN_PER_CLASS),
        },
    }
    doc.update(over)
    return doc
