"""vektra-ingest FastAPI router (REQ-014, REQ-029, REQ-033, REQ-040).

Routes:
  POST /api/v1/ingest                    - Upload and ingest a document
  POST /api/v1/ingest/batch              - Upload and ingest multiple documents (always async)
  GET  /api/v1/ingest/jobs/{id}/status   - Poll async job status

Auth: 'ingest' or 'admin' scope required.

Sync/async threshold: files <= 10MB are processed synchronously (returns 200).
Files > 10MB are enqueued as arq jobs (returns 202 with job_id).
Files > VEKTRA_MAX_FILE_SIZE_MB are rejected with ERR-INGEST-002.
Batch endpoint: all files are processed asynchronously (always 202).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vektra_ingest.exceptions import IngestConflictError, IngestError
from vektra_ingest.models import SourceDocumentOrm
from vektra_ingest.pipeline import run_ingest
from vektra_shared.auth import ApiKeyInfo, require_scope
from vektra_shared.config import IngestConfig
from vektra_shared.db import get_session
from vektra_shared.errors import (
    ERR_INGEST_001,
    ERR_INGEST_002,
    ErrorCategory,
    ErrorResponse,
    http_status_for,
)

log = structlog.get_logger(__name__)

router = APIRouter()

# Sync/async file size threshold (REQ-029)
_SYNC_THRESHOLD_BYTES = 10 * 1024 * 1024  # 10 MB

# Auth: require_scope("ingest") accepts ingest and admin keys (ARCH-059
# admin-as-superscope), and includes rate limiting integration.


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class IngestResponse(BaseModel):
    document_id: UUID
    chunk_count: int | None
    status: str


class AsyncIngestResponse(BaseModel):
    job_id: UUID
    status: str = "pending"


class BatchIngestItemResponse(BaseModel):
    job_id: UUID | None = None
    filename: str
    status: str  # "pending" or "rejected"
    error: str | None = None


class BatchDeleteRequest(BaseModel):
    document_ids: list[UUID]
    namespace: str = "default"


class BatchDeleteResponse(BaseModel):
    deleted: list[str]
    not_found: list[str]


class JobStatusResponse(BaseModel):
    id: UUID
    status: str
    phase: str | None = None
    document_id: UUID | None = None
    chunk_count: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    percentage: int | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/api/v1/ingest", response_model=None, status_code=200)
async def ingest(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile,
    namespace: str = "default",
    session: AsyncSession = Depends(get_session),
    key_info: ApiKeyInfo = Depends(require_scope("ingest")),
) -> Any:
    """Ingest a document.

    Accepts multipart/form-data with:
    - file: the document to ingest (PDF, DOCX, PPTX)
    - namespace: target namespace (query param, default "default")

    Returns:
    - 200 + {document_id, chunk_count, status} for sync path (<= 10MB)
    - 202 + {job_id, status} for async path (> 10MB)
    - 409 for same filename with different content (REQ-033)
    - 413 for file too large (ERR-INGEST-002)
    """
    ingest_config = IngestConfig()
    max_bytes = ingest_config.max_file_size_mb * 1024 * 1024

    # Read file content
    file_content = await file.read()
    file_size = len(file_content)
    filename = file.filename or "upload"

    # Size validation (REQ-029, ERR-INGEST-002)
    if file_size > max_bytes:
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code=ERR_INGEST_002,
            message=f"File size {file_size} bytes exceeds maximum {max_bytes} bytes.",
            remediation=(
                f"Reduce the file size to below {ingest_config.max_file_size_mb}MB, "
                "or split the document into smaller parts."
            ),
        )
        raise HTTPException(
            status_code=http_status_for(err),
            detail=err.to_envelope(),
        )

    # Async path: > 10MB → enqueue arq job (REQ-029: returns 202)
    if file_size > _SYNC_THRESHOLD_BYTES:
        async_response = await _enqueue_ingest_job(
            file_content=file_content,
            filename=filename,
            namespace=namespace,
            session=session,
            background_tasks=background_tasks,
            key_info=key_info,
            request=request,
        )
        return JSONResponse(
            content=async_response.model_dump(mode="json"),
            status_code=202,
        )

    registry = getattr(request.app.state, "registry", None)

    # Sync path: <= 10MB → process immediately
    # DEBT-007: audit log on both success and error paths
    result = None
    error: IngestConflictError | IngestError | None = None
    try:
        result = await run_ingest(
            file_content=file_content,
            filename=filename,
            namespace=namespace,
            session=session,
            registry=registry,
        )
    except IngestConflictError as exc:
        error = exc
    except IngestError as exc:
        error = exc

    # Write audit log for all outcomes (DEBT-007)
    if error is not None:
        status_code = 409 if isinstance(error, IngestConflictError) else 422
        error_code = (
            ERR_INGEST_001
            if isinstance(error, IngestConflictError)
            else error.error_code
        )
        # Direct await for error paths (BackgroundTasks don't run after HTTPException)
        await _write_audit_log_direct(
            key_info=key_info,
            request=request,
            status_code=status_code,
            action=f"ingest_error",
            log_metadata={
                "namespace": namespace,
                "filename": filename,
                "error_code": error_code,
            },
        )
    else:
        _write_audit_log(
            background_tasks=background_tasks,
            key_info=key_info,
            request=request,
            status_code=200,
            action=f"ingest_{result.status}",
            log_metadata={
                "document_id": str(result.document_id) if result.document_id else None,
                "namespace": namespace,
                "filename": filename,
                "chunk_count": result.chunk_count,
            },
        )

    # Raise after audit log is written
    if isinstance(error, IngestConflictError):
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code=ERR_INGEST_001,
            message=str(error),
            remediation="Delete the existing document or use a different filename.",
        )
        raise HTTPException(status_code=409, detail=err.to_envelope())
    if isinstance(error, IngestError):
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code=error.error_code,
            message=error.message,
            remediation="Fix the document or use a supported format, then retry.",
        )
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())

    assert result is not None

    log.info(
        "ingest_sync_complete",
        document_id=str(result.document_id),
        status=result.status,
        namespace=namespace,
    )

    assert result.document_id is not None, "Sync ingest must produce a document_id"
    return IngestResponse(
        document_id=result.document_id,
        chunk_count=result.chunk_count,
        status=result.status,
    )


@router.post("/api/v1/ingest/batch", status_code=202)
async def batch_ingest(
    request: Request,
    background_tasks: BackgroundTasks,
    files: list[UploadFile],
    namespace: str = "default",
    session: AsyncSession = Depends(get_session),
    key_info: ApiKeyInfo = Depends(require_scope("ingest")),
) -> JSONResponse:
    """Ingest multiple documents asynchronously.

    Accepts multipart/form-data with multiple file fields named 'files'.
    All files are processed asynchronously (always 202). Files exceeding
    VEKTRA_MAX_FILE_SIZE_MB are reported as 'rejected' in the response
    array without creating a job.
    """
    if not files:
        raise HTTPException(
            status_code=422,
            detail={"error": {"message": "No files provided"}},
        )

    ingest_config = IngestConfig()
    max_bytes = ingest_config.max_file_size_mb * 1024 * 1024
    registry = getattr(request.app.state, "registry", None)

    items: list[dict] = []

    for upload in files:
        file_content = await upload.read()
        file_size = len(file_content)
        filename = upload.filename or "upload"

        if file_size > max_bytes:
            items.append(
                BatchIngestItemResponse(
                    filename=filename,
                    status="rejected",
                    error=(
                        f"File size {file_size} bytes exceeds maximum "
                        f"{max_bytes} bytes."
                    ),
                ).model_dump(mode="json")
            )
            continue

        # Create IngestJobOrm and enqueue
        from vektra_ingest.models import IngestJobOrm

        job = IngestJobOrm(
            namespace_id=namespace,
            filename=filename,
            file_size_bytes=file_size,
            status="pending",
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)

        await _enqueue_batch_file(
            job_id=job.id,
            file_content=file_content,
            filename=filename,
            namespace=namespace,
            registry=registry,
            background_tasks=background_tasks,
            request=request,
        )

        items.append(
            BatchIngestItemResponse(
                job_id=job.id,
                filename=filename,
                status="pending",
            ).model_dump(mode="json")
        )

    _write_audit_log(
        background_tasks=background_tasks,
        key_info=key_info,
        request=request,
        status_code=202,
        action="ingest_batch",
        log_metadata={
            "namespace": namespace,
            "file_count": len(files),
            "accepted_count": sum(1 for i in items if i["status"] == "pending"),
        },
    )

    log.info(
        "ingest_batch_accepted",
        namespace=namespace,
        file_count=len(files),
        accepted_count=sum(1 for i in items if i["status"] == "pending"),
    )

    return JSONResponse(content=items, status_code=202)


@router.delete("/api/v1/documents/batch", response_model=BatchDeleteResponse)
async def batch_delete(
    request: Request,
    background_tasks: BackgroundTasks,
    body: BatchDeleteRequest,
    session: AsyncSession = Depends(get_session),
    key_info: ApiKeyInfo = Depends(require_scope("admin")),
) -> BatchDeleteResponse:
    """Delete multiple documents by ID (requires admin scope).

    Soft-deletes each document with deletion_reason='user_request'.
    Hard-deletes chunks via VectorStoreProvider.
    Returns lists of deleted and not-found document IDs.
    """
    registry = getattr(request.app.state, "registry", None)
    deleted: list[str] = []
    not_found: list[str] = []

    for doc_id in body.document_ids:
        result = await session.execute(
            select(SourceDocumentOrm).where(
                SourceDocumentOrm.id == doc_id,
                SourceDocumentOrm.namespace_id == body.namespace,
                SourceDocumentOrm.deleted_at.is_(None),
            )
        )
        doc = result.scalar_one_or_none()

        if doc is None:
            not_found.append(str(doc_id))
            continue

        # Soft-delete the document
        await session.execute(
            update(SourceDocumentOrm)
            .where(SourceDocumentOrm.id == doc_id)
            .values(
                deleted_at=datetime.now(UTC),
                deletion_reason="user_request",
            )
        )
        deleted.append(str(doc_id))

        # Hard-delete chunks (best-effort)
        if registry is not None:
            try:
                vector_store = registry.get("vector_store", "default")
                await vector_store.delete(body.namespace, [str(doc_id)])
            except Exception as exc:
                log.warning(
                    "batch_delete_chunks_failed",
                    document_id=str(doc_id),
                    error=str(exc),
                )

    await session.commit()

    _write_audit_log(
        background_tasks=background_tasks,
        key_info=key_info,
        request=request,
        status_code=200,
        action="batch_delete",
        log_metadata={
            "namespace": body.namespace,
            "deleted_count": len(deleted),
            "not_found_count": len(not_found),
        },
    )

    log.info(
        "batch_delete_complete",
        namespace=body.namespace,
        deleted_count=len(deleted),
        not_found_count=len(not_found),
    )

    return BatchDeleteResponse(deleted=deleted, not_found=not_found)


# ---------------------------------------------------------------------------
# Granular ingest APIs (EX-010)
# ---------------------------------------------------------------------------


@router.post("/api/v1/ingest/extract", status_code=200)
async def extract_only(
    file: UploadFile,
    _key: ApiKeyInfo = Depends(require_scope("ingest")),
) -> list[dict]:
    """Extract document elements without chunking or embedding.

    Returns JSON array of DocumentChunks (text, element_type, metadata).
    Synchronous only, subject to 10MB limit.
    """
    file_content = await file.read()
    filename = file.filename or "upload"

    if len(file_content) > _SYNC_THRESHOLD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"error": {"message": "File too large for granular API (max 10MB)"}},
        )

    from vektra_ingest.detection import detect_content_type
    from vektra_ingest.pipeline import _get_extractor

    content_type = detect_content_type(file_content, filename)
    extractor = _get_extractor(content_type)
    if extractor is None:
        raise HTTPException(
            status_code=422,
            detail={"error": {"message": f"Unsupported content type: {content_type}"}},
        )

    from vektra_shared.types import ExtractionRequest

    req = ExtractionRequest(content=file_content, content_type=content_type, filename=filename)
    elements = []
    async for chunk in await extractor.extract(req):
        elements.append({
            "text": chunk.text,
            "element_type": chunk.element_type.value,
            "content_format": chunk.content_format,
            "metadata": chunk.metadata,
        })

    return elements


@router.post("/api/v1/ingest/chunk", status_code=200)
async def chunk_only(
    file: UploadFile,
    _key: ApiKeyInfo = Depends(require_scope("ingest")),
) -> list[dict]:
    """Extract and chunk a document without embedding or storage.

    Returns JSON array of chunked DocumentChunks.
    Synchronous only, subject to 10MB limit.
    """
    file_content = await file.read()
    filename = file.filename or "upload"

    if len(file_content) > _SYNC_THRESHOLD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"error": {"message": "File too large for granular API (max 10MB)"}},
        )

    from vektra_ingest.chunking import DualStrategyChunking, FixedSizeChunking
    from vektra_ingest.detection import detect_content_type
    from vektra_ingest.pipeline import _get_extractor

    content_type = detect_content_type(file_content, filename)
    extractor = _get_extractor(content_type)
    if extractor is None:
        raise HTTPException(
            status_code=422,
            detail={"error": {"message": f"Unsupported content type: {content_type}"}},
        )

    from vektra_shared.types import ExtractionRequest

    req = ExtractionRequest(content=file_content, content_type=content_type, filename=filename)
    elements_iter = await extractor.extract(req)

    ingest_config = IngestConfig()
    chunker: FixedSizeChunking | DualStrategyChunking
    if ingest_config.chunking_strategy == "dual":
        chunker = DualStrategyChunking(
            text_chunk_size=ingest_config.chunk_size,
            text_chunk_overlap=ingest_config.chunk_overlap,
            parent_chunk_size=ingest_config.chunk_size * 3,
        )
    else:
        chunker = FixedSizeChunking(
            chunk_size=ingest_config.chunk_size,
            chunk_overlap=ingest_config.chunk_overlap,
        )

    chunks = []
    async for chunk in await chunker.chunk(elements_iter):
        chunks.append({
            "text": chunk.text,
            "element_type": chunk.element_type.value,
            "content_format": chunk.content_format,
            "metadata": chunk.metadata,
        })

    return chunks


@router.post("/api/v1/ingest/embed", status_code=200)
async def embed_only(
    request: Request,
    file: UploadFile,
    _key: ApiKeyInfo = Depends(require_scope("ingest")),
) -> list[dict]:
    """Extract, chunk, and embed a document without storage.

    Returns JSON array of ChunkEmbeddings (text, dense vector, metadata).
    Synchronous only, subject to 10MB limit.
    """
    file_content = await file.read()
    filename = file.filename or "upload"

    if len(file_content) > _SYNC_THRESHOLD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"error": {"message": "File too large for granular API (max 10MB)"}},
        )

    from vektra_ingest.chunking import DualStrategyChunking, FixedSizeChunking
    from vektra_ingest.detection import detect_content_type
    from vektra_ingest.pipeline import _get_extractor

    content_type = detect_content_type(file_content, filename)
    extractor = _get_extractor(content_type)
    if extractor is None:
        raise HTTPException(
            status_code=422,
            detail={"error": {"message": f"Unsupported content type: {content_type}"}},
        )

    from vektra_shared.types import ExtractionRequest

    req = ExtractionRequest(content=file_content, content_type=content_type, filename=filename)
    elements_iter = await extractor.extract(req)

    ingest_config = IngestConfig()
    chunker: FixedSizeChunking | DualStrategyChunking
    if ingest_config.chunking_strategy == "dual":
        chunker = DualStrategyChunking(
            text_chunk_size=ingest_config.chunk_size,
            text_chunk_overlap=ingest_config.chunk_overlap,
            parent_chunk_size=ingest_config.chunk_size * 3,
        )
    else:
        chunker = FixedSizeChunking(
            chunk_size=ingest_config.chunk_size,
            chunk_overlap=ingest_config.chunk_overlap,
        )

    all_chunks = []
    async for chunk in await chunker.chunk(elements_iter):
        all_chunks.append(chunk)

    if not all_chunks:
        return []

    registry = getattr(request.app.state, "registry", None)
    embedding_provider = registry.get("embedding", "default")
    texts = [c.text for c in all_chunks]
    embeddings = await embedding_provider.embed_documents(texts)

    results = []
    for i, (chunk, embedding) in enumerate(zip(all_chunks, embeddings)):
        results.append({
            "text": chunk.text,
            "dense": embedding,
            "element_type": chunk.element_type.value,
            "content_format": chunk.content_format,
            "metadata": {
                **chunk.metadata,
                "content_type": content_type,
                "element_type": chunk.element_type.value,
                "position": i,
            },
        })

    return results


@router.get("/api/v1/ingest/jobs/{job_id}/status", response_model=JobStatusResponse)
async def get_job_status(
    job_id: UUID,
    session: AsyncSession = Depends(get_session),
    _key: ApiKeyInfo = Depends(require_scope("ingest")),
) -> JobStatusResponse:
    """Return current status of an async ingest job (REQ-014, NFR-010)."""
    from vektra_ingest.models import IngestJobOrm

    result = await session.execute(
        select(IngestJobOrm).where(IngestJobOrm.id == job_id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(
            status_code=404, detail={"error": {"message": "Job not found"}}
        )

    return JobStatusResponse(
        id=job.id,
        status=job.status,
        phase=job.phase,
        document_id=job.document_id,
        chunk_count=job.chunk_count,
        error_code=job.error_code,
        error_message=job.error_message,
        percentage=job.percentage,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _enqueue_ingest_job(
    *,
    file_content: bytes,
    filename: str,
    namespace: str,
    session: AsyncSession,
    background_tasks: BackgroundTasks,
    key_info: ApiKeyInfo,
    request: Request,
) -> AsyncIngestResponse:
    """Create an IngestJobOrm and enqueue an arq task. Returns 202."""
    from arq import ArqRedis

    from vektra_ingest.models import IngestJobOrm

    job = IngestJobOrm(
        namespace_id=namespace,
        filename=filename,
        file_size_bytes=len(file_content),
        status="pending",
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    # Try to enqueue via arq (registry may have arq_pool)
    registry = getattr(request.app.state, "registry", None)
    arq_pool: ArqRedis | None = None
    if registry is not None:
        try:
            arq_pool = registry.get("arq_pool", "default")
        except (ValueError, AttributeError):
            pass

    if arq_pool is not None:
        try:
            await arq_pool.enqueue_job(
                "ingest_document_task",
                str(job.id),
                namespace,
                filename,
                file_content,
            )
        except Exception as exc:
            log.error("enqueue_failed", job_id=str(job.id), error=str(exc))
            from vektra_ingest.jobs import _update_job

            await _update_job(
                job.id,
                status="failed",
                error_code="ERR-INGEST-004",
                error_message=f"Failed to enqueue job: {exc}",
            )
            raise HTTPException(
                status_code=500,
                detail={"error": {"message": "Failed to enqueue ingest job"}},
            ) from exc
    else:
        # Fallback: run in background (no arq in test/dev)
        background_tasks.add_task(
            _run_task_in_background,
            job_id=str(job.id),
            namespace_id=namespace,
            filename=filename,
            file_bytes=file_content,
            registry=registry,
        )

    _write_audit_log(
        background_tasks=background_tasks,
        key_info=key_info,
        request=request,
        status_code=202,
        action="ingest_enqueued",
        log_metadata={
            "job_id": str(job.id),
            "namespace": namespace,
            "filename": filename,
        },
    )

    log.info(
        "ingest_async_enqueued",
        job_id=str(job.id),
        namespace=namespace,
        filename=filename,
    )

    return AsyncIngestResponse(job_id=job.id)


async def _enqueue_batch_file(
    *,
    job_id: UUID,
    file_content: bytes,
    filename: str,
    namespace: str,
    registry: Any,
    background_tasks: BackgroundTasks,
    request: Request,
) -> None:
    """Enqueue a single file from a batch ingest request."""
    from arq import ArqRedis

    arq_pool: ArqRedis | None = None
    if registry is not None:
        try:
            arq_pool = registry.get("arq_pool", "default")
        except (ValueError, AttributeError):
            pass

    if arq_pool is not None:
        try:
            await arq_pool.enqueue_job(
                "ingest_document_task",
                str(job_id),
                namespace,
                filename,
                file_content,
            )
        except Exception as exc:
            log.error("batch_enqueue_failed", job_id=str(job_id), error=str(exc))
            from vektra_ingest.jobs import _update_job

            await _update_job(
                job_id,
                status="failed",
                error_code="ERR-INGEST-004",
                error_message=f"Failed to enqueue job: {exc}",
            )
    else:
        background_tasks.add_task(
            _run_task_in_background,
            job_id=str(job_id),
            namespace_id=namespace,
            filename=filename,
            file_bytes=file_content,
            registry=registry,
        )


async def _run_task_in_background(
    *,
    job_id: str,
    namespace_id: str,
    filename: str,
    file_bytes: bytes,
    registry: Any,
) -> None:
    """Background fallback for async ingest when arq is not configured."""
    from vektra_ingest.jobs import ingest_document_task

    ctx = {"registry": registry}
    await ingest_document_task(ctx, job_id, namespace_id, filename, file_bytes)


def _write_audit_log(
    *,
    background_tasks: BackgroundTasks,
    key_info: ApiKeyInfo,
    request: Request,
    status_code: int,
    action: str,
    log_metadata: dict[str, Any],
) -> None:
    """Fire-and-forget audit log write via background task."""
    from vektra_shared.audit import log_event

    request_id = getattr(request.state, "request_id", None)
    if request_id is None:
        return

    background_tasks.add_task(
        log_event,
        key_id=key_info.key_id,
        endpoint="/api/v1/ingest",
        method="POST",
        status_code=status_code,
        request_id=request_id,
        action=action,
        log_metadata=log_metadata,
    )


async def _write_audit_log_direct(
    *,
    key_info: ApiKeyInfo,
    request: Request,
    status_code: int,
    action: str,
    log_metadata: dict[str, Any],
) -> None:
    """Write audit log directly (for error paths where BackgroundTasks don't run)."""
    from vektra_shared.audit import log_event

    request_id = getattr(request.state, "request_id", None)
    if request_id is None:
        return

    try:
        await log_event(
            key_id=key_info.key_id,
            endpoint="/api/v1/ingest",
            method="POST",
            status_code=status_code,
            request_id=request_id,
            action=action,
            log_metadata=log_metadata,
        )
    except Exception as exc:
        log.warning("audit_log_direct_failed", error=str(exc))
