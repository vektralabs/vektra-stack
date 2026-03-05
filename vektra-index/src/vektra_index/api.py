"""vektra-index REST API (REQ-012, ARCH-059).

Phase 1 endpoints:
  POST /api/v1/documents/{id}/chunks  - store embeddings (ingest/admin)
  POST /api/v1/search                 - semantic search (query/admin)
  DELETE /api/v1/documents/{id}       - delete document + chunks (admin)
  GET  /api/v1/stats                  - document/chunk counts (any scope)
  GET  /api/v1/health                 - component health (unauthenticated)
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from vektra_shared.auth import ApiKeyInfo, require_scope
from vektra_shared.config import EmbeddingConfig, VectorStoreConfig
from vektra_shared.db import get_session
from vektra_shared.errors import (
    ERR_INGEST_004,
    ERR_QUERY_004,
    ErrorCategory,
    ErrorResponse,
    http_status_for,
)
from vektra_shared.types import (
    ChunkEmbedding,
    QueryEmbedding,
    SearchFilters,
    SearchMode,
    SparseVector,
)

# Read from env once at import time (immutable for process lifetime)
_VS_CONFIG = VectorStoreConfig()
_EMB_CONFIG = EmbeddingConfig()

router = APIRouter(prefix="/api/v1", tags=["index"])


# ---------------------------------------------------------------------------
# Request / response types
# ---------------------------------------------------------------------------


class StoreChunksRequest(BaseModel):
    """Request body for POST /documents/{id}/chunks."""

    chunks: list[ChunkEmbeddingPayload]
    namespace: str = "default"


class SparseVectorPayload(BaseModel):
    """Sparse vector representation (indices + values)."""

    indices: list[int]
    values: list[float]

    @model_validator(mode="after")
    def _check_lengths(self) -> SparseVectorPayload:
        if len(self.indices) != len(self.values):
            raise ValueError(
                f"indices length ({len(self.indices)}) must equal "
                f"values length ({len(self.values)})"
            )
        return self


class ChunkEmbeddingPayload(BaseModel):
    """A single chunk with its dense embedding (from the ingest pipeline)."""

    chunk_id: str | None = None  # optional; server generates UUID if absent
    text: str
    dense: list[float]
    sparse: SparseVectorPayload | None = None  # Phase 2: BM25/SPLADE
    metadata: dict[str, Any] = Field(default_factory=dict)


class StoreChunksResponse(BaseModel):
    document_id: str
    chunk_ids: list[str]
    chunks_stored: int


class SearchRequest(BaseModel):
    """Request body for POST /search."""

    query: str
    namespace: str = "default"
    top_k: int = Field(5, ge=1, le=100)
    search_mode: SearchMode = SearchMode.DENSE
    filters: dict[str, Any] | None = None


class SearchResponse(BaseModel):
    results: list[SearchResultPayload]
    total: int


class SearchResultPayload(BaseModel):
    chunk_id: str
    document_id: str
    score: float
    text_snippet: str
    document_version: int
    metadata: dict[str, Any]


class DeleteDocumentResponse(BaseModel):
    document_id: str
    chunks_removed: int


class StatsResponse(BaseModel):
    document_count: int
    chunk_count: int
    namespace: str


class HealthResponse(BaseModel):
    status: str
    component: str = "vektra-index"
    latency_ms: int | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/documents/{document_id}/chunks", response_model=StoreChunksResponse)
async def store_chunks(
    document_id: UUID,
    body: StoreChunksRequest,
    key: ApiKeyInfo = Depends(require_scope("ingest")),
    session: AsyncSession = Depends(get_session),
) -> StoreChunksResponse:
    """Store embeddings for a document's chunks.

    Called by vektra-ingest after generating embeddings. Requires 'ingest'
    or 'admin' scope.
    """
    from vektra_index.providers.pgvector import PgvectorProvider

    # Enforce namespace binding for scoped keys (H5)
    effective_ns = key.namespace_id or body.namespace

    # Build ChunkEmbedding objects from the request payload
    chunk_embeddings = [
        ChunkEmbedding(
            chunk_id=item.chunk_id or "",
            text=item.text,
            dense=item.dense,
            sparse=SparseVector(indices=item.sparse.indices, values=item.sparse.values)
            if item.sparse
            else None,
            metadata=item.metadata,
        )
        for item in body.chunks
    ]

    provider = PgvectorProvider(active_index_version=_VS_CONFIG.active_index_version)

    try:
        async with session.begin():
            chunk_ids = await provider.store(
                session=session,
                namespace=effective_ns,
                document_id=document_id,
                chunks=chunk_embeddings,
            )
    except Exception as exc:
        err = ErrorResponse(
            category=ErrorCategory.UPSTREAM,
            code=ERR_INGEST_004,
            message=f"Vector store write failed: {exc}",
            remediation=(
                "Check PostgreSQL connectivity and pgvector extension status. "
                "Run GET /health for component status."
            ),
        )
        raise HTTPException(
            status_code=http_status_for(err), detail=err.to_envelope()
        ) from exc

    return StoreChunksResponse(
        document_id=str(document_id),
        chunk_ids=chunk_ids,
        chunks_stored=len(chunk_ids),
    )


@router.post("/search", response_model=SearchResponse)
async def search(
    request: Request,
    body: SearchRequest,
    key: ApiKeyInfo = Depends(require_scope("query")),
    session: AsyncSession = Depends(get_session),
) -> SearchResponse:
    """Semantic search over indexed chunks (no LLM synthesis).

    Embeds the query string, then runs cosine similarity search with optional
    JSONB metadata filtering. Returns ranked chunks.
    """
    import logging

    from vektra_index.providers.pgvector import PgvectorProvider
    from vektra_index.providers.sentence_transformers import (
        SentenceTransformersProvider,
    )

    _logger = logging.getLogger(__name__)

    # Enforce namespace binding for scoped keys (H5)
    effective_ns = key.namespace_id or body.namespace

    embedding_provider = SentenceTransformersProvider(
        model_name=_EMB_CONFIG.embedding_model
    )
    pgvector_provider = PgvectorProvider(
        active_index_version=_VS_CONFIG.active_index_version
    )

    # Embed the query (dense)
    try:
        dense_vector = await embedding_provider.embed_query(body.query)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail={"error": {"message": f"Embedding failed: {exc}"}}
        ) from exc

    # Embed the query (sparse) - only when SparseEmbeddingProvider is available
    sparse_vector = None
    effective_mode = body.search_mode

    if body.search_mode in (SearchMode.SPARSE, SearchMode.HYBRID):
        sparse_provider = getattr(request.app.state, "sparse_embedding_provider", None)
        if sparse_provider is not None:
            try:
                sparse_vector = await sparse_provider.embed_query(body.query)
            except Exception as exc:
                _logger.warning(
                    "sparse_embedding_failed, falling back to DENSE: %s", exc
                )
                effective_mode = SearchMode.DENSE
        else:
            _logger.warning(
                "sparse_embedding_not_registered, search_mode=%s falling back to DENSE",
                body.search_mode.value,
            )
            effective_mode = SearchMode.DENSE

    query_embedding = QueryEmbedding(dense=dense_vector, sparse=sparse_vector)

    filters: SearchFilters | None = None
    if body.filters:
        filters = body.filters  # type: ignore[assignment]

    try:
        results = await pgvector_provider.search(
            session=session,
            namespace=effective_ns,
            query_embedding=query_embedding,
            top_k=body.top_k,
            search_mode=effective_mode,
            filters=filters,
        )
    except Exception as exc:
        err = ErrorResponse(
            category=ErrorCategory.UPSTREAM,
            code=ERR_QUERY_004,
            message=f"Vector store read failed: {exc}",
            remediation="Check PostgreSQL connectivity and pgvector extension status.",
        )
        raise HTTPException(
            status_code=http_status_for(err), detail=err.to_envelope()
        ) from exc

    return SearchResponse(
        results=[
            SearchResultPayload(
                chunk_id=r.chunk_id,
                document_id=str(r.document_id),
                score=r.score,
                text_snippet=r.text_snippet,
                document_version=r.document_version,
                metadata=r.metadata,
            )
            for r in results
        ],
        total=len(results),
    )


@router.delete("/documents/{document_id}", response_model=DeleteDocumentResponse)
async def delete_document(
    document_id: UUID,
    namespace: str = Query("default"),
    key: ApiKeyInfo = Depends(require_scope("admin")),
    session: AsyncSession = Depends(get_session),
) -> DeleteDocumentResponse:
    """Delete all chunks for a document and soft-delete the document.

    Implements BLOCKER B-1/B-3 resolution: SELECT COUNT then DELETE chunks
    in one transaction, then soft-delete source_document.
    """
    from vektra_index.providers.pgvector import PgvectorProvider

    provider = PgvectorProvider(active_index_version=_VS_CONFIG.active_index_version)

    async with session.begin():
        chunks_removed = await provider.delete(
            session=session,
            namespace=namespace,
            document_id=document_id,
        )

    return DeleteDocumentResponse(
        document_id=str(document_id),
        chunks_removed=chunks_removed,
    )


@router.get("/stats", response_model=StatsResponse)
async def stats(
    namespace: str | None = Query(None),
    key: ApiKeyInfo = Depends(require_scope(None)),
    session: AsyncSession = Depends(get_session),
) -> StatsResponse:
    """Return document and chunk counts (optionally scoped to a namespace).

    Accepts any valid API key scope (ARCH-059).
    """
    from vektra_index.providers.pgvector import PgvectorProvider

    effective_ns = key.namespace_id or namespace
    if key.namespace_id and namespace and namespace != key.namespace_id:
        raise HTTPException(status_code=403, detail="Namespace scope violation")

    provider = PgvectorProvider(active_index_version=_VS_CONFIG.active_index_version)
    data = await provider.namespace_stats(session=session, namespace=effective_ns)

    return StatsResponse(**data)


@router.get("/health", response_model=HealthResponse)
async def health(
    session: AsyncSession = Depends(get_session),
) -> HealthResponse:
    """Unauthenticated component health check."""
    from vektra_index.providers.pgvector import PgvectorProvider

    provider = PgvectorProvider(active_index_version=_VS_CONFIG.active_index_version)
    status = await provider.health_check(session=session)

    return HealthResponse(
        status=status.status,
        latency_ms=status.latency_ms,
    )
