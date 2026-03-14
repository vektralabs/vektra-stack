"""QdrantVectorStoreProvider: VectorStoreProvider backed by Qdrant (ARCH-051).

Implements the VectorStoreProvider Protocol directly (no session needed,
talks to external Qdrant service via gRPC/HTTP).

Collection setup:
  - Dense vectors: named vector "dense", cosine distance.
  - Sparse vectors: named vector "sparse" with IDF modifier.
  - Payload fields: namespace_id, index_version, text, metadata, document_id.

qdrant-client is an optional dependency. If not installed, importing this
module raises ImportError at construction time with a clear message.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

from vektra_shared.types import (
    ChunkEmbedding,
    HealthStatus,
    QueryEmbedding,
    SearchFilters,
    SearchMode,
    SearchResult,
)

logger = logging.getLogger(__name__)

# RRF constant (matches PgvectorProvider)
_RRF_K = 60


def _is_timeout(exc: BaseException) -> bool:
    """Detect timeout-like exceptions from qdrant-client (HTTP or gRPC).

    qdrant-client wraps transport exceptions:
    - HTTP: ResponseHandlingException with httpx.TimeoutException as .source
    - gRPC: grpc.aio.AioRpcError with DEADLINE_EXCEEDED status
    Python's built-in TimeoutError is also caught as a safety net.
    """
    if isinstance(exc, TimeoutError):
        return True
    # HTTP transport: ResponseHandlingException wrapping httpx timeout
    exc_type = type(exc).__name__
    if exc_type == "ResponseHandlingException":
        source = getattr(exc, "source", None)
        return source is not None and "timeout" in type(source).__name__.lower()
    # gRPC transport: AioRpcError with DEADLINE_EXCEEDED
    if exc_type == "AioRpcError":
        code = getattr(exc, "code", lambda: None)()
        return code is not None and str(code) == "StatusCode.DEADLINE_EXCEEDED"
    return False


def _import_qdrant() -> Any:
    """Import qdrant_client with a clear error message if missing."""
    try:
        import qdrant_client

        return qdrant_client
    except ImportError as exc:
        raise ImportError(
            "qdrant-client is required for QdrantVectorStoreProvider. "
            "Install it with: pip install 'vektra-index[qdrant]' "
            "or: pip install 'qdrant-client>=1.12'"
        ) from exc


class QdrantVectorStoreProvider:
    """Qdrant implementation of VectorStoreProvider Protocol.

    Implements the session-free VectorStoreProvider Protocol directly
    since Qdrant is an external service (no SQLAlchemy session needed).
    """

    def __init__(
        self,
        *,
        url: str = "http://localhost:6333",
        api_key: str | None = None,
        collection_name: str = "vektra",
        active_index_version: int = 1,
        dense_dimensions: int = 384,
        _client: Any | None = None,
    ) -> None:
        self._collection_name = collection_name
        self._active_index_version = active_index_version
        self._dense_dimensions = dense_dimensions

        if _client is not None:
            # Allow injection for testing
            self._client = _client
        else:
            qdrant_client = _import_qdrant()
            self._client = qdrant_client.AsyncQdrantClient(
                url=url,
                api_key=api_key,
            )

    async def ensure_collection(self) -> None:
        """Create the collection if it doesn't exist.

        Called during startup validation. Configures named vectors
        for dense and sparse search.
        """
        from qdrant_client import models

        collections = await self._client.get_collections()
        existing = {c.name for c in collections.collections}
        if self._collection_name in existing:
            return

        try:
            await self._client.create_collection(
                collection_name=self._collection_name,
                vectors_config={
                    "dense": models.VectorParams(
                        size=self._dense_dimensions,
                        distance=models.Distance.COSINE,
                    ),
                },
                sparse_vectors_config={
                    "sparse": models.SparseVectorParams(
                        modifier=models.Modifier.IDF,
                    ),
                },
            )
        except Exception:
            # Race: another replica may have created it between our check and create.
            collections = await self._client.get_collections()
            if self._collection_name not in {c.name for c in collections.collections}:
                raise
            return
        logger.info("Created Qdrant collection: %s", self._collection_name)

    async def store(
        self,
        namespace: str,
        chunks: Sequence[ChunkEmbedding],
    ) -> list[str]:
        """Upsert points with named vectors and payload.

        Uses wait=True for synchronous confirmation. On exception after
        partial upsert, executes compensating delete for all point IDs
        in the batch (ARCH-052).
        """
        if not chunks:
            return []

        from qdrant_client import models

        points: list[models.PointStruct] = []
        point_ids: list[str] = []

        for chunk in chunks:
            point_id = chunk.chunk_id or str(uuid4())
            point_ids.append(point_id)

            vectors: dict[str, Any] = {
                "dense": chunk.dense,
            }
            if chunk.sparse is not None:
                vectors["sparse"] = models.SparseVector(
                    indices=chunk.sparse.indices,
                    values=chunk.sparse.values,
                )

            points.append(
                models.PointStruct(
                    id=point_id,
                    vector=vectors,
                    payload={
                        "namespace_id": namespace,
                        "index_version": self._active_index_version,
                        "text": chunk.text,
                        "metadata": chunk.metadata,
                        "document_id": chunk.metadata.get("document_id", ""),
                    },
                )
            )

        try:
            await self._client.upsert(
                collection_name=self._collection_name,
                points=points,
                wait=True,
            )
        except Exception as exc:
            if _is_timeout(exc):
                # On timeout, Qdrant may have already persisted the points.
                # Compensating delete would cause data loss. Log and re-raise
                # so the caller can retry (idempotent via deterministic IDs).
                logger.warning(
                    "qdrant_store_timeout, skipping compensating delete for %d points "
                    "(upsert may have succeeded server-side)",
                    len(point_ids),
                )
                raise

            # Non-timeout error: Qdrant explicitly rejected the batch.
            # Safe to run compensating delete (ARCH-052).
            logger.warning(
                "qdrant_store_failed, executing compensating delete for %d points",
                len(point_ids),
            )
            try:
                await self._client.delete(
                    collection_name=self._collection_name,
                    points_selector=models.PointIdsList(points=point_ids),
                    wait=True,
                )
            except Exception as cleanup_exc:
                logger.error("qdrant_compensating_delete_failed: %s", cleanup_exc)
            raise

        return point_ids

    async def search(
        self,
        namespace: str,
        query_embedding: QueryEmbedding,
        top_k: int,
        search_mode: SearchMode = SearchMode.DENSE,
        filters: SearchFilters | None = None,
        raw_filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Search with DENSE, SPARSE, or HYBRID mode.

        HYBRID uses Qdrant's prefetch API with RRF fusion in a single
        round trip (no client-side fusion).
        """
        base_filter = self._build_filter(namespace, filters, raw_filters)

        if search_mode == SearchMode.HYBRID:
            return await self._search_hybrid(query_embedding, top_k, base_filter)
        if search_mode == SearchMode.SPARSE:
            return await self._search_sparse(query_embedding, top_k, base_filter)
        return await self._search_dense(query_embedding, top_k, base_filter)

    async def _search_dense(
        self,
        query_embedding: QueryEmbedding,
        top_k: int,
        base_filter: Any,
    ) -> list[SearchResult]:
        results = await self._client.query_points(
            collection_name=self._collection_name,
            query=query_embedding.dense,
            using="dense",
            limit=top_k,
            query_filter=base_filter,
            with_payload=True,
        )
        return self._points_to_results(results.points)

    async def _search_sparse(
        self,
        query_embedding: QueryEmbedding,
        top_k: int,
        base_filter: Any,
    ) -> list[SearchResult]:
        if query_embedding.sparse is None:
            logger.warning(
                "qdrant_sparse_search_no_sparse_embedding, falling back to DENSE"
            )
            return await self._search_dense(query_embedding, top_k, base_filter)

        from qdrant_client import models

        results = await self._client.query_points(
            collection_name=self._collection_name,
            query=models.SparseVector(
                indices=query_embedding.sparse.indices,
                values=query_embedding.sparse.values,
            ),
            using="sparse",
            limit=top_k,
            query_filter=base_filter,
            with_payload=True,
        )
        return self._points_to_results(results.points)

    async def _search_hybrid(
        self,
        query_embedding: QueryEmbedding,
        top_k: int,
        base_filter: Any,
    ) -> list[SearchResult]:
        """Hybrid search using Qdrant's prefetch + RRF fusion API.

        Single round trip: two prefetch requests (dense and sparse)
        fused server-side via RRF.
        """
        if query_embedding.sparse is None:
            logger.warning(
                "qdrant_hybrid_search_no_sparse_embedding, falling back to DENSE"
            )
            return await self._search_dense(query_embedding, top_k, base_filter)

        from qdrant_client import models

        results = await self._client.query_points(
            collection_name=self._collection_name,
            prefetch=[
                models.Prefetch(
                    query=query_embedding.dense,
                    using="dense",
                    limit=top_k * 2,
                    filter=base_filter,
                ),
                models.Prefetch(
                    query=models.SparseVector(
                        indices=query_embedding.sparse.indices,
                        values=query_embedding.sparse.values,
                    ),
                    using="sparse",
                    limit=top_k * 2,
                    filter=base_filter,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=top_k,
            with_payload=True,
        )
        return self._points_to_results(results.points)

    async def delete(self, namespace: str, ids: list[str]) -> int:
        """Delete points by document_id payload filter.

        ids: list of document_id strings. Uses wait=True for synchronous
        confirmation. Returns count of deleted points.
        """
        from qdrant_client import models

        await self._client.delete(
            collection_name=self._collection_name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="document_id",
                            match=models.MatchAny(any=ids),
                        ),
                        models.FieldCondition(
                            key="namespace_id",
                            match=models.MatchValue(value=namespace),
                        ),
                    ]
                )
            ),
            wait=True,
        )
        # Qdrant delete returns UpdateResult; count not directly available.
        # Return 0 as a convention; the caller should not depend on exact count.
        return 0

    async def health_check(self) -> HealthStatus:
        """Check Qdrant connectivity via get_collections()."""
        try:
            start = time.monotonic()
            await self._client.get_collections()
            latency_ms = int((time.monotonic() - start) * 1000)
            return HealthStatus(status="healthy", latency_ms=latency_ms)
        except Exception as exc:
            return HealthStatus(status="unhealthy", message=str(exc))

    def _build_filter(
        self,
        namespace: str,
        filters: SearchFilters | None = None,
        raw_filters: dict[str, Any] | None = None,
    ) -> Any:
        """Build Qdrant filter with namespace + index_version + metadata filters."""
        from qdrant_client import models

        must_conditions: list[Any] = [
            models.FieldCondition(
                key="namespace_id",
                match=models.MatchValue(value=namespace),
            ),
            models.FieldCondition(
                key="index_version",
                match=models.MatchValue(value=self._active_index_version),
            ),
        ]

        if filters:
            for key, value in filters.items():
                if isinstance(value, list):
                    must_conditions.append(
                        models.FieldCondition(
                            key=f"metadata.{key}",
                            match=models.MatchAny(any=value),
                        )
                    )
                else:
                    must_conditions.append(
                        models.FieldCondition(
                            key=f"metadata.{key}",
                            match=models.MatchValue(value=value),
                        )
                    )

        # raw_filters: pass through as additional Qdrant conditions
        if raw_filters:
            for key, value in raw_filters.items():
                if isinstance(value, dict) and "range" in value:
                    must_conditions.append(
                        models.FieldCondition(
                            key=key,
                            range=models.Range(**value["range"]),
                        )
                    )
                else:
                    must_conditions.append(
                        models.FieldCondition(
                            key=key,
                            match=models.MatchValue(value=value),
                        )
                    )

        return models.Filter(must=must_conditions)

    @staticmethod
    def _points_to_results(points: list[Any]) -> list[SearchResult]:
        """Convert Qdrant ScoredPoint list to SearchResult list."""
        results: list[SearchResult] = []
        for point in points:
            payload = point.payload or {}
            doc_id_str = payload.get("document_id", "")
            try:
                doc_id = UUID(doc_id_str) if doc_id_str else UUID(int=0)
            except (ValueError, AttributeError):
                doc_id = UUID(int=0)

            results.append(
                SearchResult(
                    chunk_id=str(point.id),
                    score=float(point.score) if point.score is not None else 0.0,
                    text_snippet=payload.get("text", ""),
                    document_id=doc_id,
                    document_version=payload.get("metadata", {}).get(
                        "document_version", 1
                    ),
                    metadata=payload.get("metadata", {}),
                )
            )
        return results
