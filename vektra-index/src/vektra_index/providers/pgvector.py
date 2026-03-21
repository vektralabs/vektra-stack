"""PgvectorProvider: VectorStoreProvider backed by PostgreSQL + pgvector.

Phase 2: DENSE + SPARSE + HYBRID search modes.
- store(): bulk INSERT with sparse_vector JSONB when present (ARCH-052).
- search(): cosine similarity (DENSE), sparse dot-product (SPARSE),
  RRF fusion (HYBRID) with combined JSONB filter in single SQL (REQ-063).
- delete(): SELECT COUNT then DELETE document_chunks in one transaction (B-1/B-3).
- Full-store contract: text stored with embedding, search returns text_snippet
  without secondary lookup (ARCH-051).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from datetime import UTC
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Float, delete, func, literal_column, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from vektra_shared.types import (
    ChunkEmbedding,
    HealthStatus,
    QueryEmbedding,
    SearchFilters,
    SearchMode,
    SearchResult,
)

logger = logging.getLogger(__name__)

# RRF constant (standard value from the original RRF paper)
_RRF_K = 60


class PgvectorProvider:
    """PostgreSQL/pgvector implementation of the vector store backend.

    NOTE: This class does NOT directly implement VectorStoreProvider Protocol.
    Its methods take (session, ...) for explicit session management, which is
    incompatible with the Protocol's session-free signatures. This is intentional:
    - vektra-index exposes PgvectorProvider exclusively via its REST API
    - vektra-core accesses vektra-index via HTTP (POST /api/v1/search, etc.)
    - The VectorStoreProvider Protocol in vektra_shared is implemented by
      VectorStoreServiceAdapter which wraps this class with managed sessions
    - Session injection keeps PgvectorProvider unit-testable with mock sessions
    """

    def __init__(self, active_index_version: int = 1) -> None:
        self._active_index_version = active_index_version

    async def store(
        self,
        session: AsyncSession,
        namespace: str,
        document_id: UUID,
        chunks: Sequence[ChunkEmbedding],
    ) -> list[str]:
        """Bulk-insert chunks into document_chunks.

        Persists chunk.sparse as JSONB {"indices": [...], "values": [...]}
        when present. Existing chunks with no sparse data keep NULL.

        Returns list of inserted chunk IDs (str, not UUID per ARCH-051).
        """
        if not chunks:
            return []

        from vektra_index.models import DocumentChunkOrm

        inserted_ids: list[str] = []
        for position, chunk in enumerate(chunks):
            chunk_id = uuid4()
            sparse_data = None
            if chunk.sparse is not None:
                sparse_data = {
                    "indices": chunk.sparse.indices,
                    "values": chunk.sparse.values,
                }
            orm_obj = DocumentChunkOrm(
                id=chunk_id,
                document_id=document_id,
                namespace_id=namespace,
                content=chunk.text,
                embedding=chunk.dense,
                sparse_vector=sparse_data,
                chunk_metadata=chunk.metadata,
                position=chunk.metadata.get("position", position),
                index_version=self._active_index_version,
            )
            session.add(orm_obj)
            inserted_ids.append(str(chunk_id))
        await session.flush()

        return inserted_ids

    async def search(
        self,
        session: AsyncSession,
        namespace: str,
        query_embedding: QueryEmbedding,
        top_k: int,
        search_mode: SearchMode = SearchMode.DENSE,
        filters: SearchFilters | None = None,
        raw_filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Vector search with DENSE, SPARSE, or HYBRID mode.

        DENSE: cosine similarity via pgvector <=> operator.
        SPARSE: dot-product similarity over JSONB sparse_vector column.
        HYBRID: RRF fusion of dense and sparse ranks (k=60).

        Filters are applied in the same SQL query (not post-filtered).
        All queries include WHERE index_version = :active_version (REQ-064).
        """
        if search_mode == SearchMode.SPARSE:
            return await self._search_sparse(
                session, namespace, query_embedding, top_k, filters
            )
        if search_mode == SearchMode.HYBRID:
            return await self._search_hybrid(
                session, namespace, query_embedding, top_k, filters
            )
        # Default: DENSE
        return await self._search_dense(
            session, namespace, query_embedding, top_k, filters
        )

    async def _search_dense(
        self,
        session: AsyncSession,
        namespace: str,
        query_embedding: QueryEmbedding,
        top_k: int,
        filters: SearchFilters | None = None,
    ) -> list[SearchResult]:
        """Cosine similarity search (Phase 1 compatible)."""
        from vektra_index.models import DocumentChunkOrm

        distance_expr = DocumentChunkOrm.embedding.cosine_distance(
            query_embedding.dense
        )
        score_expr = (1 - distance_expr).label("score")

        stmt = (
            select(
                DocumentChunkOrm.id,
                DocumentChunkOrm.document_id,
                DocumentChunkOrm.content,
                DocumentChunkOrm.chunk_metadata,
                score_expr,
            )
            .where(
                DocumentChunkOrm.namespace_id == namespace,
                DocumentChunkOrm.index_version == self._active_index_version,
            )
            .order_by(distance_expr)
            .limit(top_k)
        )

        stmt = self._apply_filters(stmt, filters)
        stmt = self._join_source_documents(stmt)

        result = await session.execute(stmt)
        return self._rows_to_results(result.all())

    async def _search_sparse(
        self,
        session: AsyncSession,
        namespace: str,
        query_embedding: QueryEmbedding,
        top_k: int,
        filters: SearchFilters | None = None,
    ) -> list[SearchResult]:
        """Sparse dot-product similarity via JSONB sparse_vector column.

        Computes dot product in SQL: for each chunk, sum the products of values
        at matching indices between the query sparse vector and the stored vector.
        """
        from vektra_index.models import DocumentChunkOrm

        if query_embedding.sparse is None:
            logger.warning("sparse_search_no_sparse_embedding, falling back to DENSE")
            return await self._search_dense(
                session, namespace, query_embedding, top_k, filters
            )

        # Build sparse dot-product using raw SQL on JSONB
        # The sparse_vector column stores {"indices": [...], "values": [...]}
        # We compute sum(sv.value * qv) for matching indices
        q_indices = query_embedding.sparse.indices
        q_values = query_embedding.sparse.values

        # Build a SQL expression for sparse dot product:
        # For each (index, value) in the query, check if that index exists
        # in the stored sparse_vector and multiply by the corresponding value.
        # Use a LATERAL join over the stored vector's parallel arrays.
        sparse_score_sql = text("""
            COALESCE((
                SELECT SUM(sv_val * qv_val)
                FROM (
                    SELECT
                        (sparse_vector->'indices'->>idx)::int AS sv_idx,
                        (sparse_vector->'values'->>idx)::float AS sv_val
                    FROM generate_series(
                        0,
                        jsonb_array_length(sparse_vector->'indices') - 1
                    ) AS idx
                    WHERE sparse_vector IS NOT NULL
                ) stored,
                (
                    SELECT
                        unnest(:q_indices::int[]) AS q_idx,
                        unnest(:q_values::float[]) AS qv_val
                ) query
                WHERE stored.sv_idx = query.q_idx
            ), 0.0)
        """)

        score_col = sparse_score_sql.columns(score=Float).c.score.label("score")

        stmt = (
            select(
                DocumentChunkOrm.id,
                DocumentChunkOrm.document_id,
                DocumentChunkOrm.content,
                DocumentChunkOrm.chunk_metadata,
                score_col,
            )
            .where(
                DocumentChunkOrm.namespace_id == namespace,
                DocumentChunkOrm.index_version == self._active_index_version,
                DocumentChunkOrm.sparse_vector.isnot(None),
            )
            .order_by(literal_column("score").desc())
            .limit(top_k)
        )

        stmt = self._apply_filters(stmt, filters)
        stmt = self._join_source_documents(stmt)

        result = await session.execute(
            stmt,
            {"q_indices": q_indices, "q_values": q_values},
        )
        return self._rows_to_results(result.all())

    async def _search_hybrid(
        self,
        session: AsyncSession,
        namespace: str,
        query_embedding: QueryEmbedding,
        top_k: int,
        filters: SearchFilters | None = None,
    ) -> list[SearchResult]:
        """Hybrid search with Reciprocal Rank Fusion (RRF).

        Runs dense and sparse searches independently, then combines
        using RRF: score = 1/(k + rank_dense) + 1/(k + rank_sparse)
        where k=60. Chunks with no sparse_vector get rank = top_k + 1.
        """
        if query_embedding.sparse is None:
            logger.warning("hybrid_search_no_sparse_embedding, falling back to DENSE")
            return await self._search_dense(
                session, namespace, query_embedding, top_k, filters
            )

        # Run both searches (2x top_k to have enough candidates for fusion)
        fetch_k = min(top_k * 2, 100)
        dense_results = await self._search_dense(
            session, namespace, query_embedding, fetch_k, filters
        )
        sparse_results = await self._search_sparse(
            session, namespace, query_embedding, fetch_k, filters
        )

        # Build rank maps (1-based)
        dense_ranks: dict[str, int] = {}
        for rank, r in enumerate(dense_results, start=1):
            dense_ranks[r.chunk_id] = rank

        sparse_ranks: dict[str, int] = {}
        for rank, r in enumerate(sparse_results, start=1):
            sparse_ranks[r.chunk_id] = rank

        # Collect all unique chunk IDs
        all_chunks: dict[str, SearchResult] = {}
        for r in dense_results:
            all_chunks[r.chunk_id] = r
        for r in sparse_results:
            if r.chunk_id not in all_chunks:
                all_chunks[r.chunk_id] = r

        # RRF scoring
        default_rank = fetch_k + 1
        rrf_scored: list[tuple[float, SearchResult]] = []
        for chunk_id, result in all_chunks.items():
            dr = dense_ranks.get(chunk_id, default_rank)
            sr = sparse_ranks.get(chunk_id, default_rank)
            rrf_score = 1.0 / (_RRF_K + dr) + 1.0 / (_RRF_K + sr)
            rrf_scored.append((rrf_score, result))

        # Sort by RRF score descending, take top_k
        rrf_scored.sort(key=lambda x: x[0], reverse=True)

        return [
            SearchResult(
                chunk_id=r.chunk_id,
                score=rrf_score,
                text_snippet=r.text_snippet,
                document_id=r.document_id,
                document_version=r.document_version,
                metadata=r.metadata,
            )
            for rrf_score, r in rrf_scored[:top_k]
        ]

    @staticmethod
    def _apply_filters(stmt: Any, filters: SearchFilters | None) -> Any:
        """Apply JSONB metadata filters in the same SQL query (REQ-063)."""
        if not filters:
            return stmt

        from vektra_index.models import DocumentChunkOrm

        for key, value in filters.items():
            if isinstance(value, list):
                stmt = stmt.where(
                    DocumentChunkOrm.chunk_metadata[key].as_string().in_(value)
                )
            else:
                stmt = stmt.where(
                    DocumentChunkOrm.chunk_metadata[key].as_string() == str(value)
                )
        return stmt

    @staticmethod
    def _join_source_documents(stmt: Any) -> Any:
        """Join with source_documents for document_version and soft-delete exclusion."""
        from vektra_index.models import DocumentChunkOrm, SourceDocumentOrm

        return (
            stmt.join(
                SourceDocumentOrm,
                DocumentChunkOrm.document_id == SourceDocumentOrm.id,
            )
            .where(SourceDocumentOrm.deleted_at.is_(None))
            .add_columns(SourceDocumentOrm.version.label("document_version"))
        )

    @staticmethod
    def _rows_to_results(rows: Sequence[Any]) -> list[SearchResult]:
        return [
            SearchResult(
                chunk_id=str(row.id),
                score=float(row.score),
                text_snippet=row.content,
                document_id=row.document_id,
                document_version=row.document_version,
                metadata=row.chunk_metadata or {},
            )
            for row in rows
        ]

    async def delete(
        self,
        session: AsyncSession,
        namespace: str,
        document_id: UUID,
    ) -> int:
        """Hard-delete document_chunks, then soft-delete source_document.

        Returns chunks_removed count. Both operations in one transaction
        per BLOCKER B-1/B-3 resolution (ARCH-058 schema notes).
        """
        from datetime import datetime

        from vektra_index.models import DocumentChunkOrm, SourceDocumentOrm

        # 1. Count chunks before deletion (BLOCKER B-3: chunks_removed data source)
        count_result = await session.execute(
            select(func.count()).where(
                DocumentChunkOrm.document_id == document_id,
                DocumentChunkOrm.namespace_id == namespace,
            )
        )
        chunks_count = count_result.scalar_one()

        # 2. Hard-delete document_chunks
        await session.execute(
            delete(DocumentChunkOrm).where(
                DocumentChunkOrm.document_id == document_id,
                DocumentChunkOrm.namespace_id == namespace,
            )
        )

        # 3. Soft-delete source_document
        await session.execute(
            update(SourceDocumentOrm)
            .where(
                SourceDocumentOrm.id == document_id,
                SourceDocumentOrm.namespace_id == namespace,
                SourceDocumentOrm.deleted_at.is_(None),
            )
            .values(
                deleted_at=datetime.now(UTC),
                deletion_reason="user_request",
            )
        )

        await session.flush()
        return chunks_count

    async def health_check(self, session: AsyncSession) -> HealthStatus:
        """Quick connectivity check via SELECT 1 FROM document_chunks."""
        try:
            start = time.monotonic()
            await session.execute(text("SELECT 1"))
            latency_ms = int((time.monotonic() - start) * 1000)
            return HealthStatus(status="healthy", latency_ms=latency_ms)
        except Exception as exc:
            return HealthStatus(status="unhealthy", message=str(exc))

    async def namespace_stats(
        self,
        session: AsyncSession,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Return document and chunk counts for a namespace (or all namespaces).

        Used by GET /stats endpoint (ARCH-051).
        """
        from vektra_index.models import DocumentChunkOrm, SourceDocumentOrm

        doc_stmt = (
            select(func.count())
            .select_from(SourceDocumentOrm)
            .where(SourceDocumentOrm.deleted_at.is_(None))
        )
        chunk_stmt = (
            select(func.count())
            .select_from(DocumentChunkOrm)
            .join(
                SourceDocumentOrm,
                (SourceDocumentOrm.id == DocumentChunkOrm.document_id)
                & (SourceDocumentOrm.namespace_id == DocumentChunkOrm.namespace_id),
            )
            .where(DocumentChunkOrm.index_version == self._active_index_version)
            .where(SourceDocumentOrm.deleted_at.is_(None))
        )

        if namespace:
            doc_stmt = doc_stmt.where(SourceDocumentOrm.namespace_id == namespace)
            chunk_stmt = chunk_stmt.where(DocumentChunkOrm.namespace_id == namespace)

        doc_count = (await session.execute(doc_stmt)).scalar_one()
        chunk_count = (await session.execute(chunk_stmt)).scalar_one()

        return {
            "document_count": doc_count,
            "chunk_count": chunk_count,
            "namespace": namespace or "all",
        }
