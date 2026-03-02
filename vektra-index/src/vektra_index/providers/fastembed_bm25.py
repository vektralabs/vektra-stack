"""FastEmbedBM25Provider: SparseEmbeddingProvider using fastembed (ARCH-053).

BM25 sparse embeddings via fastembed's Qdrant/bm25 model. Lightweight:
tokenization + term frequency computation only, no GPU required.

When paired with Qdrant, IDF is computed server-side automatically.
For pgvector hybrid search, the raw TF vectors are used directly
(IDF weighting is implicit in the dot-product scoring).

fastembed is an optional dependency. If not installed, importing this
module raises ImportError at construction time with a clear message.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from vektra_shared.types import SparseVector

logger = logging.getLogger(__name__)

# Module-level singleton for the fastembed model (same pattern as
# SentenceTransformersProvider). One model instance for the process.
_sparse_model: Any | None = None
_sparse_model_name: str | None = None


def _get_sparse_model(model_name: str) -> Any:
    """Load (or reuse) the fastembed sparse model."""
    global _sparse_model, _sparse_model_name
    if _sparse_model is None or _sparse_model_name != model_name:
        try:
            from fastembed import SparseTextEmbedding
        except ImportError as exc:
            raise ImportError(
                "fastembed is required for SparseEmbeddingProvider. "
                "Install it with: pip install 'vektra-index[sparse]' "
                "or: pip install 'fastembed>=0.4'"
            ) from exc

        logger.info("Loading sparse embedding model: %s", model_name)
        _sparse_model = SparseTextEmbedding(model_name=model_name)
        _sparse_model_name = model_name
        logger.info("Sparse embedding model loaded: %s", model_name)
    return _sparse_model


def _to_sparse_vector(embedding: Any) -> SparseVector:
    """Convert a fastembed SparseEmbedding to our SparseVector dataclass."""
    return SparseVector(
        indices=embedding.indices.tolist(),
        values=embedding.values.tolist(),
    )


class FastEmbedBM25Provider:
    """SparseEmbeddingProvider backed by fastembed BM25.

    Uses asyncio.to_thread() for inference (same pattern as
    SentenceTransformersProvider) since fastembed is synchronous.
    """

    def __init__(self, model_name: str = "Qdrant/bm25") -> None:
        self._model_name = model_name
        # Eagerly validate that fastembed is importable
        _get_sparse_model(model_name)

    def _model(self) -> Any:
        return _get_sparse_model(self._model_name)

    async def embed_documents(self, texts: list[str]) -> list[SparseVector]:
        """Embed a batch of document passages for indexing."""
        model = self._model()
        embeddings = await asyncio.to_thread(
            lambda: list(model.embed(texts))
        )
        return [_to_sparse_vector(e) for e in embeddings]

    async def embed_query(self, text: str) -> SparseVector:
        """Embed a single query string for retrieval."""
        model = self._model()
        embeddings = await asyncio.to_thread(
            lambda: list(model.query_embed(text))
        )
        return _to_sparse_vector(embeddings[0])

    def vocab_size(self) -> int | None:
        """Return the vocabulary size, or None if not available."""
        # fastembed BM25 models don't expose vocab size directly
        return None
