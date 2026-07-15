"""Reindex API and background job (ARCH-045, REQ-064).

POST /api/v1/reindex triggers a background job that re-embeds all chunks
for a namespace using the current embedding model, storing results under
the target_index_version. Progress is tracked via the reindex_jobs table.

GET /api/v1/reindex/{job_id}/status returns current progress.

The active index version switch is manual: the operator sets
VEKTRA_ACTIVE_INDEX_VERSION after reindex completes and restarts. Only then
does DELETE /api/v1/index-versions/{version} reclaim the old version, which
until that moment is the one serving traffic. That ordering is not a
convention the operator is asked to honour: the store refuses to delete the
version it is reading (DEBT-032).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vektra_shared.auth import ApiKeyInfo, require_scope
from vektra_shared.db import get_session
from vektra_shared.errors import (
    ERR_INDEX_001,
    ERR_INDEX_002,
    ERR_INDEX_003,
    ERR_INDEX_004,
    ActiveIndexVersionError,
    ErrorCategory,
    ErrorResponse,
    http_status_for,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["reindex"])


# ---------------------------------------------------------------------------
# Request / response types
# ---------------------------------------------------------------------------


class ReindexRequest(BaseModel):
    """Request body for POST /reindex."""

    namespace: str = "default"
    target_index_version: int = Field(..., ge=1)


class ReindexResponse(BaseModel):
    job_id: str
    status: str
    namespace: str
    target_index_version: int


class ReindexStatusResponse(BaseModel):
    job_id: str
    status: str
    namespace: str
    source_index_version: int
    target_index_version: int
    total_documents: int
    processed_documents: int
    chunks_reindexed: int
    error_message: str | None = None
    created_at: str
    completed_at: str | None = None


class DeleteIndexVersionResponse(BaseModel):
    namespace: str
    index_version: int
    chunks_removed: int


# ---------------------------------------------------------------------------
# Errors (REQ-010 envelope)
# ---------------------------------------------------------------------------


def _active_index_version_refusal(exc: ActiveIndexVersionError) -> HTTPException:
    """409 for a cleanup aimed at the version being served.

    The one error on this endpoint a client would genuinely branch on, so it
    carries a code rather than a bare string: an operator tool needs to tell
    "wrong version, nothing happened" apart from "the store is down".
    """
    err = ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_INDEX_001,
        message=str(exc),
        remediation=(
            "Switch VEKTRA_ACTIVE_INDEX_VERSION to the new version and restart, "
            "then delete the old one. To check which version is live, see "
            "VEKTRA_ACTIVE_INDEX_VERSION in the running configuration."
        ),
        details={"namespace": exc.namespace, "index_version": exc.index_version},
    )
    return HTTPException(status_code=http_status_for(err), detail=err.to_envelope())


def _invalid_index_version(index_version: int) -> HTTPException:
    """400 for an index version below 1."""
    err = ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_INDEX_002,
        message=f"index_version must be an integer >= 1, got {index_version}.",
        remediation="Pass the index version you want to delete, as an integer >= 1.",
        details={"index_version": index_version},
    )
    return HTTPException(status_code=http_status_for(err), detail=err.to_envelope())


def _reindex_target_conflict(target_version: int, active_version: int) -> HTTPException:
    """400 for a reindex whose target is the version already being served.

    Reindexing a version onto itself would overwrite the live chunks in place
    instead of writing a second copy beside them, defeating the zero-downtime
    switch. A client can branch on this code to bump the target and retry.
    """
    err = ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_INDEX_003,
        message=(
            f"target_index_version {target_version} must differ from the active "
            f"index version {active_version}."
        ),
        remediation=(
            "Pass a target_index_version different from the active version "
            "(VEKTRA_ACTIVE_INDEX_VERSION)."
        ),
        details={
            "target_index_version": target_version,
            "active_index_version": active_version,
        },
    )
    return HTTPException(status_code=http_status_for(err), detail=err.to_envelope())


def _reindex_job_not_found(job_id: UUID) -> HTTPException:
    """404 for a reindex job id that no key-visible job matches."""
    err = ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_INDEX_004,
        message=f"Reindex job '{job_id}' not found.",
        remediation="Verify the job_id returned by POST /api/v1/reindex.",
        details={"job_id": str(job_id)},
    )
    return HTTPException(status_code=http_status_for(err), detail=err.to_envelope())


# ---------------------------------------------------------------------------
# Background job
# ---------------------------------------------------------------------------


def _target_chunk_id(document_id: UUID, position: int, target_version: int) -> str:
    """Deterministic id for a chunk rewritten under the target index version.

    Must differ from the source version's id: providers that keep both versions
    in one store (Qdrant) tell them apart by payload, so reusing the ingest seed
    uuid5(document_id, position) would overwrite the live version instead of
    writing alongside it (ADR-0026). Deterministic, so re-running a reindex is
    idempotent rather than duplicating points.
    """
    return str(uuid5(document_id, f"{position}:v{target_version}"))


async def run_reindex(
    job_id: UUID,
    namespace: str,
    source_version: int,
    target_version: int,
    registry: Any,
) -> None:
    """Background reindex job: re-embed all chunks under a new index version.

    For each document in the namespace:
    1. Read existing chunks (text + metadata) from the active vector store
    2. Re-embed chunk texts using the current EmbeddingProvider
    3. Store new chunks under target_index_version in that same store

    Reads and writes go through the VectorStoreProvider from the registry, so a
    reindex acts on whichever store is active. It previously read chunks from
    Postgres and wrote them back through a hardcoded PgvectorProvider, which in
    Qdrant mode read an empty table, re-embedded nothing, and still reported
    "completed" (BUG-023, ADR-0026).

    The original chunks (source_version) are preserved for zero-downtime
    operation. Once the operator verifies the new index and switches
    VEKTRA_ACTIVE_INDEX_VERSION, the old version is reclaimed with
    DELETE /api/v1/index-versions/{source_version}.

    This function runs outside the request lifecycle. It creates its own
    DB sessions as needed.
    """
    from vektra_index.models import ReindexJobOrm, SourceDocumentOrm
    from vektra_shared.config import VectorStoreConfig
    from vektra_shared.db import get_session_factory
    from vektra_shared.types import ChunkEmbedding

    session_factory = get_session_factory()

    try:
        async with session_factory() as session:
            # Mark job as running
            await session.execute(
                update(ReindexJobOrm)
                .where(ReindexJobOrm.id == job_id)
                .values(status="running")
            )
            await session.commit()

            # Count total documents
            count_result = await session.execute(
                select(func.count())
                .select_from(SourceDocumentOrm)
                .where(
                    SourceDocumentOrm.namespace_id == namespace,
                    SourceDocumentOrm.deleted_at.is_(None),
                )
            )
            total = count_result.scalar_one()

            await session.execute(
                update(ReindexJobOrm)
                .where(ReindexJobOrm.id == job_id)
                .values(total_documents=total)
            )
            await session.commit()

            # Iterate documents
            doc_result = await session.execute(
                select(SourceDocumentOrm.id)
                .where(
                    SourceDocumentOrm.namespace_id == namespace,
                    SourceDocumentOrm.deleted_at.is_(None),
                )
                .order_by(SourceDocumentOrm.created_at)
            )
            doc_ids = [row[0] for row in doc_result.all()]

            # Get providers from registry (required)
            if registry is None:
                raise RuntimeError("ProviderRegistry is required for reindex")
            embedding_provider = registry.get("embedding", "default")
            if embedding_provider is None:
                raise RuntimeError("No embedding provider registered for reindex")
            vector_store = registry.get("vector_store", "default")
            if vector_store is None:
                raise RuntimeError("No vector store registered for reindex")

            # list_chunks() reads the store's active version. Reindexing a
            # different source version would silently read the active one
            # instead, so refuse rather than rewrite the wrong chunks.
            active_version = VectorStoreConfig().active_index_version
            if source_version != active_version:
                raise RuntimeError(
                    f"Cannot reindex from version {source_version}: the vector "
                    f"store reads version {active_version}. Set "
                    f"VEKTRA_ACTIVE_INDEX_VERSION={source_version} first."
                )

            chunks_reindexed = 0

            for i, doc_id in enumerate(doc_ids):
                existing_chunks = await vector_store.list_chunks(namespace, doc_id)

                if existing_chunks:
                    texts = [chunk.text for chunk in existing_chunks]
                    embeddings = await embedding_provider.embed_documents(texts)

                    if len(embeddings) != len(texts):
                        raise RuntimeError(
                            f"Embedding count mismatch: got {len(embeddings)}, "
                            f"expected {len(texts)} for document {doc_id}"
                        )

                    # Chunk ids change with the version, so parent links have to
                    # be remapped or FEAT-017 parent expansion would point at the
                    # source version's chunks.
                    id_map = {
                        chunk.chunk_id: _target_chunk_id(
                            doc_id, chunk.position, target_version
                        )
                        for chunk in existing_chunks
                    }

                    chunk_embeddings = []
                    for chunk, embedding in zip(existing_chunks, embeddings):
                        metadata = dict(chunk.metadata)
                        metadata["document_id"] = str(doc_id)
                        metadata["position"] = chunk.position

                        chunk_embeddings.append(
                            ChunkEmbedding(
                                chunk_id=id_map[chunk.chunk_id],
                                text=chunk.text,
                                dense=embedding,
                                sparse=chunk.sparse,
                                metadata=metadata,
                                parent_id=(
                                    id_map.get(chunk.parent_id)
                                    if chunk.parent_id
                                    else None
                                ),
                            )
                        )

                    stored = await vector_store.store(
                        namespace,
                        chunk_embeddings,
                        index_version=target_version,
                    )
                    chunks_reindexed += len(stored)

                # Update progress
                await session.execute(
                    update(ReindexJobOrm)
                    .where(ReindexJobOrm.id == job_id)
                    .values(
                        processed_documents=i + 1,
                        current_document_id=doc_id,
                        chunks_reindexed=chunks_reindexed,
                    )
                )
                await session.commit()

                logger.info(
                    "reindex_document",
                    extra={
                        "job_id": str(job_id),
                        "document_id": str(doc_id),
                        "progress": f"{i + 1}/{total}",
                        "chunks": len(existing_chunks),
                    },
                )

            # A reindex over a non-empty namespace that wrote nothing has not
            # succeeded, whatever the loop above thinks. Reporting "completed"
            # here is the exact failure BUG-023 was: say so instead.
            if total > 0 and chunks_reindexed == 0:
                raise RuntimeError(
                    f"Reindex stored no chunks for {total} document(s) in "
                    f"namespace '{namespace}'. The vector store returned no "
                    f"chunks at index version {source_version}."
                )

            # Mark as completed
            await session.execute(
                update(ReindexJobOrm)
                .where(ReindexJobOrm.id == job_id)
                .values(
                    status="completed",
                    chunks_reindexed=chunks_reindexed,
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()

            logger.info(
                "reindex_completed",
                extra={
                    "job_id": str(job_id),
                    "documents": total,
                    "chunks_reindexed": chunks_reindexed,
                },
            )

    except Exception as exc:
        logger.error("reindex_failed", extra={"job_id": str(job_id), "error": str(exc)})
        try:
            async with session_factory() as session:
                await session.execute(
                    update(ReindexJobOrm)
                    .where(ReindexJobOrm.id == job_id)
                    .values(
                        status="failed",
                        error_message=str(exc)[:2000],
                        completed_at=datetime.now(UTC),
                    )
                )
                await session.commit()
        except Exception as update_exc:
            logger.error("reindex_status_update_failed: %s", update_exc)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/reindex", response_model=ReindexResponse, status_code=202)
async def trigger_reindex(
    body: ReindexRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    key: ApiKeyInfo = Depends(require_scope("admin")),
    session: AsyncSession = Depends(get_session),
) -> ReindexResponse:
    """Trigger a background reindex job (admin scope required)."""
    from vektra_index.models import ReindexJobOrm
    from vektra_shared.config import VectorStoreConfig

    # Enforce namespace binding: namespace-bound keys override body.namespace
    namespace = key.namespace_id or body.namespace

    # Read current active index version from config
    vs_config = VectorStoreConfig()
    source_version = vs_config.active_index_version

    if body.target_index_version == source_version:
        raise _reindex_target_conflict(body.target_index_version, source_version)

    job_id = uuid4()
    job = ReindexJobOrm(
        id=job_id,
        namespace_id=namespace,
        source_index_version=source_version,
        target_index_version=body.target_index_version,
        status="pending",
    )
    session.add(job)
    await session.commit()

    # Pass ProviderRegistry so run_reindex can access embedding provider
    registry = getattr(request.app.state, "registry", None)

    background_tasks.add_task(
        run_reindex,
        job_id=job_id,
        namespace=namespace,
        source_version=source_version,
        target_version=body.target_index_version,
        registry=registry,
    )

    return ReindexResponse(
        job_id=str(job_id),
        status="pending",
        namespace=namespace,
        target_index_version=body.target_index_version,
    )


@router.get("/reindex/{job_id}/status", response_model=ReindexStatusResponse)
async def reindex_status(
    job_id: UUID,
    key: ApiKeyInfo = Depends(require_scope("admin")),
    session: AsyncSession = Depends(get_session),
) -> ReindexStatusResponse:
    """Get the status of a reindex job."""
    from vektra_index.models import ReindexJobOrm

    filters = [ReindexJobOrm.id == job_id]
    if key.namespace_id is not None:
        filters.append(ReindexJobOrm.namespace_id == key.namespace_id)
    result = await session.execute(select(ReindexJobOrm).where(*filters))
    job = result.scalar_one_or_none()
    if job is None:
        raise _reindex_job_not_found(job_id)

    return ReindexStatusResponse(
        job_id=str(job.id),
        status=job.status,
        namespace=job.namespace_id,
        source_index_version=job.source_index_version,
        target_index_version=job.target_index_version,
        total_documents=job.total_documents,
        processed_documents=job.processed_documents,
        chunks_reindexed=job.chunks_reindexed,
        error_message=job.error_message,
        created_at=job.created_at.isoformat(),
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
    )


@router.delete(
    "/index-versions/{index_version}", response_model=DeleteIndexVersionResponse
)
async def delete_index_version(
    index_version: int,
    request: Request,
    namespace: str | None = Query(None),
    key: ApiKeyInfo = Depends(require_scope("admin")),
) -> DeleteIndexVersionResponse:
    """Reclaim the storage of a superseded index version (REQ-064, DEBT-032).

    The last step of the reindex lifecycle, and the only irreversible one. Run
    it once the new version has been switched in and verified: reindex leaves
    both versions in the store, and nothing else removes the loser.

    Refuses to delete the version the store is currently reading, with 409. The
    refusal lives in the provider, not here, so it also covers callers that are
    not this endpoint. Idempotent: a second call returns chunks_removed=0, which
    is how the operator confirms the old version is really gone.
    """
    from vektra_index.api import _namespace_scope_violation

    if index_version < 1:
        raise _invalid_index_version(index_version)

    vector_store = request.app.state.registry.get("vector_store", "default")

    # Namespace binding (H5), as on DELETE /documents/{id}: refuse a mismatch
    # rather than silently retargeting it. This endpoint deletes by filter, so a
    # silent override would delete a whole version of a namespace the caller
    # never named.
    if key.namespace_id and namespace and namespace != key.namespace_id:
        raise _namespace_scope_violation()
    effective_ns = key.namespace_id or namespace or "default"

    try:
        chunks_removed = await vector_store.delete_index_version(
            effective_ns, index_version
        )
    except ActiveIndexVersionError as exc:
        raise _active_index_version_refusal(exc) from exc

    logger.info(
        "index_version_deleted",
        extra={
            "namespace": effective_ns,
            "index_version": index_version,
            "chunks_removed": chunks_removed,
        },
    )

    return DeleteIndexVersionResponse(
        namespace=effective_ns,
        index_version=index_version,
        chunks_removed=chunks_removed,
    )
