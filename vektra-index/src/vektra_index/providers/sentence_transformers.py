"""SentenceTransformersProvider: EmbeddingProvider using sentence-transformers.

Phase 1 implementation (ADR-0013, REQ-052). Uses all-MiniLM-L6-v2 by default
(384 dimensions). The model is loaded once at first call and cached as a
module-level singleton - no double loading between vektra-ingest and vektra-core.

Asymmetric model support: embed_query() is reserved for query-time embedding
(supports "query: " prefix for models like e5-large). embed_documents() is
for indexing. For all-MiniLM-L6-v2, both paths use the same encoding (symmetric).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from vektra_shared.types import HealthStatus

logger = logging.getLogger(__name__)

# Module-level singleton: one model instance for the process lifetime.
# Protects against loading the model twice (once per component).
_model: Any | None = None
_model_name: str | None = None


def _get_model(model_name: str) -> Any:
    """Load (or reuse) the sentence-transformers model."""
    global _model, _model_name
    if _model is None or _model_name != model_name:
        from sentence_transformers import SentenceTransformer

        logger.info("Loading embedding model: %s", model_name)
        _model = SentenceTransformer(model_name)
        _model_name = model_name
        logger.info(
            "Embedding model loaded: %s (%d dims)",
            model_name,
            _model.get_sentence_embedding_dimension(),
        )
    return _model


class SentenceTransformersProvider:
    """EmbeddingProvider backed by sentence-transformers (all-MiniLM-L6-v2).

    Thread-safe: SentenceTransformer inference is stateless (no side effects
    on the model object). The module-level singleton is initialized once.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        self._model_name = model_name

    def _model(self):
        return _get_model(self._model_name)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of document passages for indexing.

        For symmetric models like all-MiniLM-L6-v2, identical to embed_query
        without the query prefix. Supports asymmetric models in Phase 2.
        """
        model = self._model()
        embeddings = model.encode(texts, convert_to_numpy=True)
        return [emb.tolist() for emb in embeddings]

    async def embed_query(self, query: str) -> list[float]:
        """Embed a single query string for retrieval.

        For asymmetric models (e.g., e5-large), prepend 'query: ' prefix.
        For all-MiniLM-L6-v2, no prefix needed.
        """
        model = self._model()
        embedding = model.encode(query, convert_to_numpy=True)
        return embedding.tolist()

    def dimensions(self) -> int:
        """Return the embedding dimensionality."""
        return self._model().get_sentence_embedding_dimension()

    async def health_check(self) -> HealthStatus:
        """Verify the model is loaded and can produce an embedding."""
        try:
            start = time.monotonic()
            await self.embed_query("health check")
            latency_ms = int((time.monotonic() - start) * 1000)
            return HealthStatus(status="healthy", latency_ms=latency_ms)
        except Exception as exc:
            return HealthStatus(status="unhealthy", message=str(exc))
