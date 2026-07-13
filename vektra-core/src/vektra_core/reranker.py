"""Reranker service: thin wrapper around the rerankers library.

Runs cross-encoder or flashrank reranking on vector search results.
CPU-bound inference is offloaded to a thread via asyncio.to_thread().

Score propagation (BUG-015): reranker scores replace the original vector
similarity scores on SearchResult.score. The original score is preserved
in SearchResult.original_score for debugging. Scores are normalized to
[0, 1] via sigmoid when raw logits are detected (cross-encoder providers).
"""

from __future__ import annotations

import asyncio
import dataclasses
import math
from typing import Protocol, runtime_checkable

import httpx
import structlog

from vektra_shared.config import RerankConfig
from vektra_shared.types import SearchResult

log = structlog.get_logger(__name__)

# Map config provider names to rerankers model_type values
_PROVIDER_TO_MODEL_TYPE = {
    "flashrank": "FlashRankRanker",
    "cross-encoder": "cross-encoder",
    "cohere": "APIRanker",
}


def _sigmoid(x: float) -> float:
    """Numerically stable sigmoid for cross-encoder logits."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


@dataclasses.dataclass
class RerankResult:
    """Reranking output with full score visibility (DEBT-014)."""

    top_k: list[SearchResult]
    all_scores: list[
        tuple[str, float, float]
    ]  # (chunk_id, reranker_score, original_score)


@runtime_checkable
class RerankerProtocol(Protocol):
    """Common interface of the in-process and remote reranker services."""

    async def rerank(
        self,
        query: str,
        results: list[SearchResult],
        top_k: int,
    ) -> RerankResult: ...


def _build_rerank_result(
    results: list[SearchResult],
    ordered: list[tuple[int, float]],
    top_k: int,
) -> RerankResult:
    """Build a RerankResult from (candidate_index, raw_score) pairs.

    Detects whether normalization is needed: FlashRank and TEI (with
    raw_scores=false) produce sigmoid scores in [0, 1]; cross-encoder
    logits can be negative or > 1.
    """
    all_raw = [score for _, score in ordered]
    needs_sigmoid = any(s < 0.0 or s > 1.0 for s in all_raw)

    all_scores: list[tuple[str, float, float]] = []
    reranked: list[SearchResult] = []

    for idx, raw in ordered:
        original = results[idx]
        normalized = _sigmoid(raw) if needs_sigmoid else raw
        all_scores.append(
            (original.chunk_id, round(normalized, 4), round(original.score, 4))
        )

        if len(reranked) < top_k:
            reranked.append(
                dataclasses.replace(
                    original,
                    score=normalized,
                    original_score=original.score,
                )
            )

    return RerankResult(top_k=reranked, all_scores=all_scores)


class RerankerService:
    """Wraps the rerankers library for scoring and reordering search results."""

    def __init__(self, *, ranker: object) -> None:
        self._ranker = ranker

    async def rerank(
        self,
        query: str,
        results: list[SearchResult],
        top_k: int,
    ) -> RerankResult:
        """Rerank search results using the cross-encoder model.

        Runs inference in a thread (CPU-bound). Returns top_k results
        sorted by reranker score, plus scores for ALL evaluated candidates.
        """
        if not results:
            return RerankResult(top_k=[], all_scores=[])

        docs = [r.text_snippet for r in results]

        ranked = await asyncio.to_thread(
            self._ranker.rank,  # type: ignore[attr-defined]
            query=query,
            docs=docs,
        )

        ordered = [(item.doc_id, float(item.score)) for item in ranked.results]
        return _build_rerank_result(results, ordered, top_k)


class TEIRerankerService:
    """Reranker backed by a TEI /rerank endpoint (FEAT-024).

    One TEI instance serves one reranker model (e.g. bge-reranker-v2-m3).
    POST /rerank {"query", "texts", "raw_scores": false} returns
    [{"index", "score"}] sorted by score descending, scores in [0, 1].
    """

    def __init__(
        self,
        *,
        url: str,
        api_key: str | None = None,
        timeout_s: float = 30.0,
        _client: httpx.AsyncClient | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = _client or httpx.AsyncClient(
            base_url=url.rstrip("/"), headers=headers, timeout=timeout_s
        )

    async def rerank(
        self,
        query: str,
        results: list[SearchResult],
        top_k: int,
    ) -> RerankResult:
        """Rerank search results via the remote TEI cross-encoder."""
        if not results:
            return RerankResult(top_k=[], all_scores=[])

        resp = await self._client.post(
            "/rerank",
            json={
                "query": query,
                "texts": [r.text_snippet for r in results],
                "raw_scores": False,
            },
        )
        resp.raise_for_status()
        ranked = resp.json()

        ordered = [(int(item["index"]), float(item["score"])) for item in ranked]
        return _build_rerank_result(results, ordered, top_k)


def create_reranker(config: RerankConfig) -> RerankerProtocol | None:
    """Create a reranker service from config. Returns None if unavailable."""
    if not config.enabled:
        log.info("reranker_disabled")
        return None

    if config.provider == "tei":
        log.info("reranker_loaded", provider="tei", url=config.tei_url)
        return TEIRerankerService(url=config.tei_url, api_key=config.tei_api_key)

    model_type = _PROVIDER_TO_MODEL_TYPE.get(config.provider, config.provider)
    model_name = config.model or _default_model_for_provider(config.provider)

    try:
        from rerankers import Reranker  # type: ignore[import-untyped]

        # API-based providers (cohere) need the key passed through;
        # without it the option was dead as wired (FEAT-024).
        kwargs: dict[str, str] = {}
        if config.api_key:
            kwargs["api_key"] = config.api_key
        ranker = Reranker(model_name, model_type=model_type, verbose=0, **kwargs)
        if ranker is None:
            log.warning(
                "reranker_init_failed",
                provider=config.provider,
                model=model_name,
            )
            return None

        log.info(
            "reranker_loaded",
            provider=config.provider,
            model=model_name,
        )
        return RerankerService(ranker=ranker)
    except Exception as exc:
        log.warning("reranker_init_failed", error=str(exc))
        return None


def _default_model_for_provider(provider: str) -> str:
    """Return a sensible default model for each provider."""
    defaults = {
        "flashrank": "ms-marco-MiniLM-L-12-v2",
        "cross-encoder": "BAAI/bge-reranker-v2-m3",
    }
    return defaults.get(provider, provider)
