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


class RerankerService:
    """Wraps the rerankers library for scoring and reordering search results."""

    def __init__(self, *, ranker: object) -> None:
        self._ranker = ranker

    async def rerank(
        self,
        query: str,
        results: list[SearchResult],
        top_k: int,
    ) -> list[SearchResult]:
        """Rerank search results using the cross-encoder model.

        Runs inference in a thread (CPU-bound). Returns top_k results
        sorted by reranker score.
        """
        if not results:
            return []

        docs = [r.text_snippet for r in results]

        ranked = await asyncio.to_thread(
            self._ranker.rank,  # type: ignore[attr-defined]
            query=query,
            docs=docs,
        )

        # Extract scores and detect whether normalization is needed.
        # FlashRank produces sigmoid scores in [0, 1]; cross-encoder
        # produces raw logits that can be negative or > 1.
        top_items = ranked.results[:top_k]
        raw_scores = [float(item.score) for item in top_items]
        needs_sigmoid = any(s < 0.0 or s > 1.0 for s in raw_scores)

        reranked: list[SearchResult] = []
        for item, raw_score in zip(top_items, raw_scores):
            original = results[item.doc_id]
            normalized = _sigmoid(raw_score) if needs_sigmoid else raw_score
            reranked.append(
                dataclasses.replace(
                    original,
                    score=normalized,
                    original_score=original.score,
                )
            )

        return reranked


def create_reranker(config: RerankConfig) -> RerankerService | None:
    """Create a RerankerService from config. Returns None if unavailable."""
    if not config.enabled:
        log.info("reranker_disabled")
        return None

    model_type = _PROVIDER_TO_MODEL_TYPE.get(config.provider, config.provider)
    model_name = config.model or _default_model_for_provider(config.provider)

    try:
        from rerankers import Reranker  # type: ignore[import-untyped]

        ranker = Reranker(model_name, model_type=model_type, verbose=0)
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
        "cross-encoder": "cross-encoder/ms-marco-MiniLM-L-6-v2",
    }
    return defaults.get(provider, provider)
