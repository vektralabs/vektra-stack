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

from vektra_shared.errors import ActiveIndexVersionError
from vektra_shared.types import (
    ChunkEmbedding,
    HealthStatus,
    QueryEmbedding,
    SearchFilters,
    SearchMode,
    SearchResult,
    SparseVector,
    StoredChunk,
)

logger = logging.getLogger(__name__)

# RRF constant (matches PgvectorProvider)
_RRF_K = 60

# Page size for scroll() in list_chunks: a document's chunks are read in full,
# so this bounds memory per round trip, not the result.
_SCROLL_PAGE_SIZE = 256


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
        return code is not None and "DEADLINE_EXCEEDED" in str(code)
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
        for dense and sparse search. If the collection already exists,
        verifies its dense vector size matches the active embedding model
        (FEAT-024): a silent mismatch would fail on every upsert/search
        with an opaque Qdrant error, so fail fast with a clear message.
        """
        from qdrant_client import models

        collections = await self._client.get_collections()
        existing = {c.name for c in collections.collections}
        if self._collection_name in existing:
            info = await self._client.get_collection(self._collection_name)
            vectors = info.config.params.vectors
            dense = (
                vectors.get("dense")
                if isinstance(vectors, dict)
                else getattr(vectors, "dense", None)
            )
            existing_size = getattr(dense, "size", None)
            if existing_size is not None and existing_size != self._dense_dimensions:
                raise ValueError(
                    f"Qdrant collection '{self._collection_name}' has dense "
                    f"vectors of size {existing_size}, but the active embedding "
                    f"model produces {self._dense_dimensions} dimensions. "
                    "Changing the embedding model requires re-ingesting into a "
                    "new collection (set VEKTRA_QDRANT_COLLECTION) or deleting "
                    "the existing one."
                )
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
            logger.debug(
                "Qdrant collection %s already exists (race resolved)",
                self._collection_name,
            )
            return
        logger.info("Created Qdrant collection: %s", self._collection_name)

    async def store(
        self,
        namespace: str,
        chunks: Sequence[ChunkEmbedding],
        index_version: int | None = None,
    ) -> list[str]:
        """Upsert points with named vectors and payload.

        Uses wait=True for synchronous confirmation. On exception after
        partial upsert, executes compensating delete for all point IDs
        in the batch (ARCH-052).

        index_version defaults to the active version. Reindex passes the target
        version: both versions share this collection and are told apart by the
        index_version payload field, so the caller must also supply chunk ids
        distinct from the source version's, or the upsert would overwrite it
        instead of writing alongside it (ADR-0026).
        """
        if not chunks:
            return []

        from qdrant_client import models

        target_version = (
            index_version if index_version is not None else self._active_index_version
        )
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
                        "index_version": target_version,
                        "text": chunk.text,
                        "metadata": chunk.metadata,
                        "document_id": chunk.metadata.get("document_id", ""),
                        "parent_id": chunk.parent_id,
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

    async def retrieve(
        self,
        namespace: str,
        chunk_ids: list[str],
    ) -> list[SearchResult]:
        """Fetch points by id (no vector search). Used for parent chunk
        expansion (FEAT-017); score is 0.0 by convention.

        Qdrant retrieve() takes no filter, so namespace and index version are
        enforced on the payload afterwards: without the version check a chunk
        from a stale index version would still be retrievable, which search
        (which does filter on it) would never return.
        """
        valid_ids: list[str] = []
        for cid in chunk_ids:
            try:
                UUID(cid)
            except ValueError:
                logger.warning("qdrant_retrieve_invalid_chunk_id: %s", cid)
                continue
            valid_ids.append(cid)
        if not valid_ids:
            return []

        records = await self._client.retrieve(
            collection_name=self._collection_name,
            ids=valid_ids,
            with_payload=True,
        )
        matching = [
            r
            for r in records
            if (r.payload or {}).get("namespace_id") == namespace
            and (r.payload or {}).get("index_version") == self._active_index_version
        ]
        return self._points_to_results(matching)

    async def list_chunks(
        self,
        namespace: str,
        document_id: UUID,
    ) -> list[StoredChunk]:
        """All chunks of a document at the active index version, by position.

        Scrolls the collection: unlike retrieve(), the caller does not know the
        point ids. Parent chunks are included (they are stored content and must
        be re-embedded on reindex), unlike search, which filters them out.
        """
        from qdrant_client import models

        scroll_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="document_id",
                    match=models.MatchValue(value=str(document_id)),
                ),
                models.FieldCondition(
                    key="namespace_id",
                    match=models.MatchValue(value=namespace),
                ),
                models.FieldCondition(
                    key="index_version",
                    match=models.MatchValue(value=self._active_index_version),
                ),
            ]
        )

        chunks: list[StoredChunk] = []
        offset: Any = None
        while True:
            # Sparse vectors are carried over verbatim on reindex (BM25 does not
            # depend on the embedding model), so fetch them; the dense vector is
            # recomputed and deliberately not fetched.
            records, offset = await self._client.scroll(
                collection_name=self._collection_name,
                scroll_filter=scroll_filter,
                limit=_SCROLL_PAGE_SIZE,
                offset=offset,
                with_payload=True,
                with_vectors=["sparse"],
            )
            for record in records:
                payload = record.payload or {}
                metadata = payload.get("metadata", {}) or {}
                chunks.append(
                    StoredChunk(
                        chunk_id=str(record.id),
                        text=payload.get("text", ""),
                        metadata=metadata,
                        position=metadata.get("position", 0),
                        parent_id=payload.get("parent_id"),
                        sparse=self._extract_sparse(record),
                    )
                )
            if offset is None:
                break

        chunks.sort(key=lambda c: c.position)
        return chunks

    @staticmethod
    def _extract_sparse(record: Any) -> SparseVector | None:
        """Pull the "sparse" named vector off a scrolled record, if present."""
        vectors = getattr(record, "vector", None)
        if not isinstance(vectors, dict):
            return None
        sparse = vectors.get("sparse")
        if sparse is None:
            return None
        indices = getattr(sparse, "indices", None)
        values = getattr(sparse, "values", None)
        if indices is None or values is None:
            return None
        return SparseVector(indices=list(indices), values=list(values))

    async def count_chunks(self, namespace: str | None = None) -> int:
        """Points at the active index version, optionally scoped to a namespace."""
        from qdrant_client import models

        must: list[Any] = [
            models.FieldCondition(
                key="index_version",
                match=models.MatchValue(value=self._active_index_version),
            )
        ]
        if namespace:
            must.append(
                models.FieldCondition(
                    key="namespace_id",
                    match=models.MatchValue(value=namespace),
                )
            )

        result = await self._client.count(
            collection_name=self._collection_name,
            count_filter=models.Filter(must=must),
            exact=True,
        )
        return int(result.count)

    async def delete(self, namespace: str, ids: list[str]) -> int:
        """Delete a document's points, across every index version.

        ids: list of document_id strings. Counts before deleting so the caller
        gets a truthful chunks_removed: Qdrant's UpdateResult does not carry it,
        and returning a made-up 0 is how DELETE came to report that it had
        removed nothing while the points were still there (BUG-023).
        """
        from qdrant_client import models

        selector = models.Filter(
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

        count_result = await self._client.count(
            collection_name=self._collection_name,
            count_filter=selector,
            exact=True,
        )

        await self._client.delete(
            collection_name=self._collection_name,
            points_selector=models.FilterSelector(filter=selector),
            wait=True,
        )
        return int(count_result.count)

    async def delete_index_version(self, namespace: str, index_version: int) -> int:
        """Delete a namespace's points at one index version (REQ-064).

        Refuses the active version: both versions live in this one collection
        and are told apart only by payload, so a filter that got the version
        wrong would delete the points that are serving traffic.

        Counts before deleting, as delete() does: Qdrant's UpdateResult does not
        carry a count, and a second call returning 0 is how the operator
        confirms the old version is really gone.
        """
        if index_version == self._active_index_version:
            raise ActiveIndexVersionError(namespace, index_version)

        from qdrant_client import models

        selector = models.Filter(
            must=[
                models.FieldCondition(
                    key="namespace_id",
                    match=models.MatchValue(value=namespace),
                ),
                models.FieldCondition(
                    key="index_version",
                    match=models.MatchValue(value=index_version),
                ),
            ]
        )

        count_result = await self._client.count(
            collection_name=self._collection_name,
            count_filter=selector,
            exact=True,
        )

        await self._client.delete(
            collection_name=self._collection_name,
            points_selector=models.FilterSelector(filter=selector),
            wait=True,
        )
        return int(count_result.count)

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

        # Parent chunks are context material, not retrieval targets (FEAT-017)
        return models.Filter(
            must=must_conditions,
            must_not=[
                models.FieldCondition(
                    key="metadata.chunk_level",
                    match=models.MatchValue(value="parent"),
                )
            ],
        )

    @staticmethod
    def _points_to_results(points: list[Any]) -> list[SearchResult]:
        """Convert Qdrant ScoredPoint/Record list to SearchResult list.

        Record objects (from retrieve()) have no score attribute: score
        defaults to 0.0.
        """
        results: list[SearchResult] = []
        for point in points:
            payload = point.payload or {}
            doc_id_str = payload.get("document_id", "")
            try:
                doc_id = UUID(doc_id_str) if doc_id_str else UUID(int=0)
            except (ValueError, AttributeError):
                doc_id = UUID(int=0)

            score = getattr(point, "score", None)
            results.append(
                SearchResult(
                    chunk_id=str(point.id),
                    score=float(score) if score is not None else 0.0,
                    text_snippet=payload.get("text", ""),
                    document_id=doc_id,
                    document_version=payload.get("metadata", {}).get(
                        "document_version", 1
                    ),
                    metadata=payload.get("metadata", {}),
                    parent_id=payload.get("parent_id"),
                )
            )
        return results
