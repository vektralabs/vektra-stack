"""vektra-index REST API (REQ-012, ARCH-059).

Phase 1 endpoints:
  POST /api/v1/documents/{id}/chunks  - store embeddings (ingest/admin)
  POST /api/v1/search                 - semantic search (query/admin)
  DELETE /api/v1/documents/{id}       - delete document + chunks (admin)
  GET  /api/v1/stats                  - document/chunk counts (any scope)
  GET  /api/v1/health                 - component health (unauthenticated)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vektra_shared.auth import ApiKeyInfo, require_scope
from vektra_shared.db import get_session
from vektra_shared.errors import (
    ERR_AUTH_003,
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


class StoredChunkPayload(BaseModel):
    """A stored chunk as held by the active vector store (no embedding)."""

    chunk_id: str
    text: str
    position: int
    parent_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ListChunksResponse(BaseModel):
    document_id: str
    namespace: str
    chunks: list[StoredChunkPayload]
    total: int


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
    request: Request,
    key: ApiKeyInfo = Depends(require_scope("ingest")),
) -> StoreChunksResponse:
    """Store embeddings for a document's chunks.

    Called by vektra-ingest after generating embeddings. Requires 'ingest'
    or 'admin' scope. Writes to the active vector store: a hardcoded pgvector
    here wrote to the inactive store in Qdrant mode (BUG-023, ADR-0026).
    """
    vector_store = request.app.state.registry.get("vector_store", "default")

    # Enforce namespace binding for scoped keys (H5)
    effective_ns = key.namespace_id or body.namespace

    # Build ChunkEmbedding objects from the request payload. The store resolves
    # the document from chunk metadata, so it must carry the path parameter.
    chunk_embeddings = [
        ChunkEmbedding(
            chunk_id=item.chunk_id or "",
            text=item.text,
            dense=item.dense,
            sparse=SparseVector(indices=item.sparse.indices, values=item.sparse.values)
            if item.sparse
            else None,
            metadata={**item.metadata, "document_id": str(document_id)},
        )
        for item in body.chunks
    ]

    try:
        chunk_ids = await vector_store.store(effective_ns, chunk_embeddings)
    except Exception as exc:
        err = ErrorResponse(
            category=ErrorCategory.UPSTREAM,
            code=ERR_INGEST_004,
            message=f"Vector store write failed: {exc}",
            remediation=(
                "Check vector store connectivity and status. "
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


def _namespace_scope_violation() -> HTTPException:
    """403 for a namespace-bound key reaching outside its namespace (H5)."""
    err = ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_AUTH_003,
        message="Namespace scope violation",
        remediation=(
            "This API key is bound to a single namespace. Omit the namespace "
            "parameter, or set it to the namespace the key is bound to."
        ),
    )
    return HTTPException(status_code=http_status_for(err), detail=err.to_envelope())


@router.get("/documents/{document_id}/chunks", response_model=ListChunksResponse)
async def list_document_chunks(
    document_id: UUID,
    request: Request,
    namespace: str | None = Query(None),
    key: ApiKeyInfo = Depends(require_scope(None)),
) -> ListChunksResponse:
    """List a document's stored chunks at the active index version.

    Reads from the active vector store, which is the only source of truth for
    chunk text (ADR-0026). Accepts any valid API key scope.
    """
    vector_store = request.app.state.registry.get("vector_store", "default")

    # namespace defaults to None, not "default": a namespace-bound key that omits
    # the parameter must fall through to its own namespace, not collide with the
    # literal "default" and be rejected as a scope violation.
    if key.namespace_id and namespace and namespace != key.namespace_id:
        raise _namespace_scope_violation()
    effective_ns = key.namespace_id or namespace or "default"

    chunks = await vector_store.list_chunks(effective_ns, document_id)

    return ListChunksResponse(
        document_id=str(document_id),
        namespace=effective_ns,
        chunks=[
            StoredChunkPayload(
                chunk_id=chunk.chunk_id,
                text=chunk.text,
                position=chunk.position,
                parent_id=chunk.parent_id,
                metadata=chunk.metadata,
            )
            for chunk in chunks
        ],
        total=len(chunks),
    )


@router.post("/search", response_model=SearchResponse)
async def search(
    request: Request,
    body: SearchRequest,
    key: ApiKeyInfo = Depends(require_scope("query")),
) -> SearchResponse:
    """Semantic search over indexed chunks (no LLM synthesis).

    Embeds the query string, then runs similarity search on the active
    vector store provider with optional JSONB metadata filtering.
    Returns ranked chunks.
    """
    import logging

    _logger = logging.getLogger(__name__)

    # Providers come from the registry: search must hit the active vector
    # store (Qdrant when configured), not a hardcoded pgvector (BUG-021).
    registry = request.app.state.registry

    # Enforce namespace binding for scoped keys (H5)
    effective_ns = key.namespace_id or body.namespace

    embedding_provider = registry.get("embedding", "default")
    vector_store = registry.get("vector_store", "default")

    # Embed the query (dense) - skip for SPARSE-only mode (NP24)
    dense_vector: list[float] = []
    effective_mode = body.search_mode

    if body.search_mode in (SearchMode.DENSE, SearchMode.HYBRID):
        try:
            dense_vector = await embedding_provider.embed_query(body.query)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={"error": {"message": f"Embedding failed: {exc}"}},
            ) from exc

    # Embed the query (sparse) - only when SparseEmbeddingProvider is available
    sparse_vector = None

    if body.search_mode in (SearchMode.SPARSE, SearchMode.HYBRID):
        if registry.has("sparse_embedding", "default"):
            sparse_provider = registry.get("sparse_embedding", "default")
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

    # If sparse fallback to DENSE, compute dense embedding now (NP24)
    if effective_mode == SearchMode.DENSE and not dense_vector:
        try:
            dense_vector = await embedding_provider.embed_query(body.query)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={"error": {"message": f"Embedding failed: {exc}"}},
            ) from exc

    query_embedding = QueryEmbedding(dense=dense_vector, sparse=sparse_vector)

    filters: SearchFilters | None = None
    if body.filters:
        filters = body.filters  # type: ignore[assignment]

    try:
        results = await vector_store.search(
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
            remediation=(
                "Check vector store connectivity and status. "
                "Run GET /health for component status."
            ),
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
    request: Request,
    namespace: str | None = Query(None),
    key: ApiKeyInfo = Depends(require_scope("admin")),
    session: AsyncSession = Depends(get_session),
) -> DeleteDocumentResponse:
    """Remove a document's chunks from the active vector store, then soft-delete
    the document record.

    The chunks go through the provider, so the deletion reaches whichever store
    holds them: this used to delete Postgres rows only, leaving the document
    retrievable from Qdrant after a 200 (BUG-023, ADR-0026).

    Chunk removal is not transactional with the soft delete (an external vector
    store cannot join the Postgres transaction). Chunks are removed first: the
    failure mode is then a document still marked live with no chunks, which a
    retry fixes, rather than a deleted document whose content still answers.
    """
    from vektra_index.models import SourceDocumentOrm

    vector_store = request.app.state.registry.get("vector_store", "default")

    # Namespace binding (H5), which this endpoint never enforced: a namespace-bound
    # admin key could name any namespace and have it honoured. That was survivable
    # only while the delete was a no-op against the active store; now that it
    # actually removes the chunks, it would be a cross-namespace deletion.
    if key.namespace_id and namespace and namespace != key.namespace_id:
        raise _namespace_scope_violation()
    effective_ns = key.namespace_id or namespace or "default"

    chunks_removed = await vector_store.delete(effective_ns, [str(document_id)])

    async with session.begin():
        await session.execute(
            update(SourceDocumentOrm)
            .where(
                SourceDocumentOrm.id == document_id,
                SourceDocumentOrm.namespace_id == effective_ns,
                SourceDocumentOrm.deleted_at.is_(None),
            )
            .values(
                deleted_at=datetime.now(UTC),
                deletion_reason="user_request",
            )
        )

    return DeleteDocumentResponse(
        document_id=str(document_id),
        chunks_removed=chunks_removed,
    )


@router.get("/stats", response_model=StatsResponse)
async def stats(
    request: Request,
    namespace: str | None = Query(None),
    key: ApiKeyInfo = Depends(require_scope(None)),
    session: AsyncSession = Depends(get_session),
) -> StatsResponse:
    """Return document and chunk counts (optionally scoped to a namespace).

    Documents are counted in Postgres (document-level bookkeeping); chunks are
    counted in the active vector store, which owns them. Counting chunks in
    Postgres reported 0 for every namespace in Qdrant mode (BUG-023, ADR-0026).

    Accepts any valid API key scope (ARCH-059).
    """
    from vektra_index.models import SourceDocumentOrm

    vector_store = request.app.state.registry.get("vector_store", "default")

    if key.namespace_id and namespace and namespace != key.namespace_id:
        raise _namespace_scope_violation()
    effective_ns = key.namespace_id or namespace

    doc_stmt = (
        select(func.count())
        .select_from(SourceDocumentOrm)
        .where(SourceDocumentOrm.deleted_at.is_(None))
    )
    if effective_ns:
        doc_stmt = doc_stmt.where(SourceDocumentOrm.namespace_id == effective_ns)

    document_count = (await session.execute(doc_stmt)).scalar_one()
    chunk_count = await vector_store.count_chunks(effective_ns)

    return StatsResponse(
        document_count=document_count,
        chunk_count=chunk_count,
        namespace=effective_ns or "all",
    )


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """Unauthenticated component health check.

    Checks the active vector store: a hardcoded pgvector check reported the
    index healthy while Qdrant, the store actually backing it, was unreachable.
    """
    vector_store = request.app.state.registry.get("vector_store", "default")
    status = await vector_store.health_check()

    return HealthResponse(
        status=status.status,
        latency_ms=status.latency_ms,
    )
