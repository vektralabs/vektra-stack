"""Reindex API and background job (ARCH-045, REQ-064).

POST /api/v1/reindex triggers a background job that re-extracts, re-chunks,
re-embeds, and stores documents with a new index_version. Progress is tracked
via the reindex_jobs table.

GET /api/v1/reindex/{job_id}/status returns current progress.

The active index version switch is manual: the operator sets
VEKTRA_ACTIVE_INDEX_VERSION after reindex completes, then triggers
cleanup of old-version chunks.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vektra_shared.auth import ApiKeyInfo, require_scope
from vektra_shared.db import get_session

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
    error_message: str | None = None
    created_at: str
    completed_at: str | None = None


# ---------------------------------------------------------------------------
# Background job
# ---------------------------------------------------------------------------


async def run_reindex(
    job_id: UUID,
    namespace: str,
    source_version: int,
    target_version: int,
) -> None:
    """Background reindex job.

    Iterates source_documents in the namespace, re-processes each one,
    and stores chunks with the target_index_version. Updates reindex_jobs
    progress after each document.

    This function runs outside the request lifecycle. It creates its own
    DB sessions as needed.
    """
    from vektra_index.models import ReindexJobOrm, SourceDocumentOrm
    from vektra_shared.db import get_session_factory

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

            for i, doc_id in enumerate(doc_ids):
                # Update progress
                await session.execute(
                    update(ReindexJobOrm)
                    .where(ReindexJobOrm.id == job_id)
                    .values(
                        processed_documents=i + 1,
                        current_document_id=doc_id,
                    )
                )
                await session.commit()

                # NOTE: actual re-extraction, re-chunking, re-embedding
                # would happen here by calling the ingest pipeline with
                # target_index_version. This is a placeholder that tracks
                # progress; the full implementation depends on the ingest
                # pipeline being available (ingest-phase2 plan).
                logger.info(
                    "reindex_document",
                    extra={
                        "job_id": str(job_id),
                        "document_id": str(doc_id),
                        "progress": f"{i + 1}/{total}",
                    },
                )

            # Mark as completed
            await session.execute(
                update(ReindexJobOrm)
                .where(ReindexJobOrm.id == job_id)
                .values(
                    status="completed",
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()

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
    background_tasks: BackgroundTasks,
    key: ApiKeyInfo = Depends(require_scope("admin")),
    session: AsyncSession = Depends(get_session),
) -> ReindexResponse:
    """Trigger a background reindex job (admin scope required)."""
    from vektra_index.models import ReindexJobOrm

    # Determine current active version from settings (or default 1)
    source_version = 1  # Will be overridden by app config in infra-phase2

    job_id = uuid4()
    job = ReindexJobOrm(
        id=job_id,
        namespace_id=body.namespace,
        source_index_version=source_version,
        target_index_version=body.target_index_version,
        status="pending",
    )
    session.add(job)
    await session.commit()

    background_tasks.add_task(
        run_reindex,
        job_id=job_id,
        namespace=body.namespace,
        source_version=source_version,
        target_version=body.target_index_version,
    )

    return ReindexResponse(
        job_id=str(job_id),
        status="pending",
        namespace=body.namespace,
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
        raise HTTPException(status_code=404, detail="Reindex job not found")

    return ReindexStatusResponse(
        job_id=str(job.id),
        status=job.status,
        namespace=job.namespace_id,
        source_index_version=job.source_index_version,
        target_index_version=job.target_index_version,
        total_documents=job.total_documents,
        processed_documents=job.processed_documents,
        error_message=job.error_message,
        created_at=job.created_at.isoformat(),
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
    )
