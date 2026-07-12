"""VectorStoreServiceAdapter: session-managed wrapper around PgvectorProvider.

Implements the session-free VectorStoreProvider Protocol by managing its own
AsyncSession internally. Registered by infra-app-entrypoint into ProviderRegistry
under category="vector_store", name="default".

Ingestion callers (vektra_ingest) must include "document_id" in each
ChunkEmbedding's metadata dict. The adapter reads this to pass to PgvectorProvider.

Search and delete callers (vektra_core, management) do not need to set metadata.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from vektra_index.providers.pgvector import PgvectorProvider

import structlog

from vektra_shared.types import (
    ChunkEmbedding,
    HealthStatus,
    QueryEmbedding,
    SearchFilters,
    SearchMode,
    SearchResult,
)

log = structlog.get_logger(__name__)


class VectorStoreServiceAdapter:
    """Adapts PgvectorProvider to the session-free VectorStoreProvider Protocol.

    All methods create their own AsyncSession from the module-level factory
    (set by vektra_shared.db.init_db at startup), commit or rollback, and
    return results without leaking session state to callers.
    """

    def __init__(self, active_index_version: int = 1) -> None:
        self._active_index_version = active_index_version

    def _get_pgvector(self) -> PgvectorProvider:
        from vektra_index.providers.pgvector import PgvectorProvider

        return PgvectorProvider(active_index_version=self._active_index_version)

    def _get_session_factory(self) -> async_sessionmaker[AsyncSession]:
        from vektra_shared.db import get_session_factory

        return get_session_factory()

    async def store(
        self,
        namespace: str,
        chunks: Sequence[ChunkEmbedding],
    ) -> list[str]:
        """Store chunks for a document.

        Callers must set metadata["document_id"] on each chunk (as str UUID).
        All chunks must belong to the same document_id.
        """
        if not chunks:
            return []

        doc_id_str = chunks[0].metadata.get("document_id")
        if not doc_id_str:
            raise ValueError(
                "VectorStoreServiceAdapter.store() requires 'document_id' in "
                "chunk metadata. Set chunk.metadata['document_id'] = str(doc_uuid)."
            )
        for i, ch in enumerate(chunks[1:], start=1):
            cid = ch.metadata.get("document_id")
            if cid != doc_id_str:
                raise ValueError(
                    f"All chunks must share the same document_id. "
                    f"Chunk 0 has '{doc_id_str}', chunk {i} has '{cid}'."
                )
        document_id = UUID(doc_id_str)

        factory = self._get_session_factory()
        pgvector = self._get_pgvector()

        async with factory() as session:
            try:
                ids = await pgvector.store(session, namespace, document_id, chunks)
                await session.commit()
                return ids
            except Exception:
                await session.rollback()
                raise

    async def search(
        self,
        namespace: str,
        query_embedding: QueryEmbedding,
        top_k: int,
        search_mode: SearchMode = SearchMode.DENSE,
        filters: SearchFilters | None = None,
        raw_filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        factory = self._get_session_factory()
        pgvector = self._get_pgvector()

        async with factory() as session:
            return await pgvector.search(
                session,
                namespace,
                query_embedding,
                top_k,
                search_mode=search_mode,
                filters=filters,
                raw_filters=raw_filters,
            )

    async def retrieve(
        self,
        namespace: str,
        chunk_ids: list[str],
    ) -> list[SearchResult]:
        factory = self._get_session_factory()
        pgvector = self._get_pgvector()

        async with factory() as session:
            return await pgvector.retrieve(session, namespace, chunk_ids)

    async def delete(self, namespace: str, ids: list[str]) -> int:
        """Delete all chunks for each document_id in ids.

        ids is a list of document UUIDs (as strings), not chunk IDs.
        """
        factory = self._get_session_factory()
        pgvector = self._get_pgvector()

        total = 0
        async with factory() as session:
            try:
                for id_str in ids:
                    doc_id = UUID(id_str)
                    total += await pgvector.delete(session, namespace, doc_id)
                await session.commit()
            except Exception:
                await session.rollback()
                raise
        return total

    async def health_check(self) -> HealthStatus:
        factory = self._get_session_factory()
        pgvector = self._get_pgvector()

        try:
            start = time.monotonic()
            async with factory() as session:
                status = await pgvector.health_check(session)
            status.latency_ms = int((time.monotonic() - start) * 1000)
            return status
        except Exception as exc:
            log.warning("vector_store_health_check_failed", error=str(exc))
            return HealthStatus(status="unhealthy", message=str(exc))
