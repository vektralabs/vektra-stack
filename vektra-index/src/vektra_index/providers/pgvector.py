"""PgvectorProvider: VectorStoreProvider backed by PostgreSQL + pgvector.

Phase 1 implementation. SearchMode.DENSE only (ARCH-010).
- store(): bulk INSERT with compensating DELETE on partial failure (ARCH-052).
- search(): cosine similarity with combined JSONB filter in single SQL (REQ-063).
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

from sqlalchemy import delete, func, select, text, update
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


class PgvectorProvider:
    """PostgreSQL/pgvector implementation of the vector store backend.

    NOTE: This class does NOT directly implement VectorStoreProvider Protocol.
    Its methods take (session, ...) for explicit session management, which is
    incompatible with the Protocol's session-free signatures. This is intentional:
    - vektra-index exposes PgvectorProvider exclusively via its REST API
    - vektra-core accesses vektra-index via HTTP (POST /api/v1/search, etc.)
    - The VectorStoreProvider Protocol in vektra_shared is implemented by an
      HTTP client in vektra-core (component-core plan, Wave 3)
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

        All objects are added and flushed in a single batch. If flush fails,
        no rows have been written; the caller's transaction rollback handles
        cleanup (single-batch atomicity makes a compensating DELETE redundant).

        Returns list of inserted chunk IDs (str, not UUID per ARCH-051).
        """
        if not chunks:
            return []

        from vektra_index.models import DocumentChunkOrm

        inserted_ids: list[str] = []
        for position, chunk in enumerate(chunks):
            chunk_id = uuid4()
            orm_obj = DocumentChunkOrm(
                id=chunk_id,
                document_id=document_id,
                namespace_id=namespace,
                content=chunk.text,
                embedding=chunk.dense,
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
        """Cosine similarity search with JSONB metadata filtering.

        Phase 1: search_mode must be DENSE (sparse and hybrid ignored).
        Filters are applied in the same SQL query (not post-filtered).
        All queries include WHERE index_version = :active_version (REQ-064).
        raw_filters ignored in Phase 1.
        """
        from vektra_index.models import DocumentChunkOrm, SourceDocumentOrm

        # Build the base query
        # Use pgvector's cosine distance operator: <=>
        # Score is 1 - distance (cosine similarity)
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

        # Apply JSONB metadata filters in the same SQL query (REQ-063)
        if filters:
            for key, value in filters.items():
                if isinstance(value, list):
                    # Match any value in the list
                    stmt = stmt.where(
                        DocumentChunkOrm.chunk_metadata[key].as_string().in_(value)
                    )
                else:
                    stmt = stmt.where(
                        DocumentChunkOrm.chunk_metadata[key].as_string() == str(value)
                    )

        # Join with source_documents to get document_version and exclude soft-deleted
        stmt = (
            stmt.join(
                SourceDocumentOrm,
                DocumentChunkOrm.document_id == SourceDocumentOrm.id,
            )
            .where(SourceDocumentOrm.deleted_at.is_(None))
            .add_columns(SourceDocumentOrm.version.label("document_version"))
        )

        result = await session.execute(stmt)
        rows = result.all()

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
            .where(DocumentChunkOrm.index_version == self._active_index_version)
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
