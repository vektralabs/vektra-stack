"""vektra-ingest FastAPI router (REQ-014, REQ-029, REQ-033, REQ-040).

Routes:
  POST /api/v1/ingest                    - Upload and ingest a document
  GET  /api/v1/ingest/jobs/{id}/status   - Poll async job status

Auth: 'ingest' or 'admin' scope required.

Sync/async threshold: files <= 10MB are processed synchronously (returns 200).
Files > 10MB are enqueued as arq jobs (returns 202 with job_id).
Files > VEKTRA_MAX_FILE_SIZE_MB are rejected with ERR-INGEST-002.
"""

from __future__ import annotations

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
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vektra_ingest.exceptions import IngestConflictError, IngestError
from vektra_ingest.pipeline import run_ingest
from vektra_shared.auth import ApiKeyInfo
from vektra_shared.config import IngestConfig
from vektra_shared.db import get_session
from vektra_shared.errors import (
    ERR_INGEST_002,
    ErrorCategory,
    ErrorResponse,
    auth_insufficient_scope,
    auth_invalid_token,
    http_status_for,
)

log = structlog.get_logger(__name__)

router = APIRouter()
_bearer = HTTPBearer(auto_error=False)

# Sync/async file size threshold (REQ-029)
_SYNC_THRESHOLD_BYTES = 10 * 1024 * 1024  # 10 MB


# ---------------------------------------------------------------------------
# Auth dependency: ingest OR admin scope
# ---------------------------------------------------------------------------


async def _require_ingest_scope(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> ApiKeyInfo:
    """Dependency: accepts keys with 'ingest' or 'admin' scope."""
    if credentials is None:
        err = auth_invalid_token()
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())

    token = credentials.credentials
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        raise HTTPException(status_code=500, detail="ProviderRegistry not initialized")

    try:
        key_store = registry.get("key_store", "default")
    except ValueError:
        raise HTTPException(status_code=500, detail="Key store not configured")

    info = await key_store.lookup_by_token(token)
    if info is None:
        err = auth_invalid_token()
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())

    if not (info.has_scope("ingest") or info.has_scope("admin")):
        err = auth_insufficient_scope("ingest")
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())

    request.state.key_id = info.key_id
    return info


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
    key_info: ApiKeyInfo = Depends(_require_ingest_scope),
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
    try:
        result = await run_ingest(
            file_content=file_content,
            filename=filename,
            namespace=namespace,
            session=session,
            registry=registry,
        )
    except IngestConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "ERR-INGEST-001",
                    "message": str(exc),
                }
            },
        )
    except IngestError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": exc.error_code,
                    "message": exc.message,
                }
            },
        )

    # Audit log (fire-and-forget, requires AuditMiddleware or explicit call)
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

    log.info(
        "ingest_sync_complete",
        document_id=str(result.document_id),
        status=result.status,
        namespace=namespace,
    )

    return IngestResponse(
        document_id=result.document_id,
        chunk_count=result.chunk_count,
        status=result.status,
    )


@router.get("/api/v1/ingest/jobs/{job_id}/status", response_model=JobStatusResponse)
async def get_job_status(
    job_id: UUID,
    session: AsyncSession = Depends(get_session),
    _key: ApiKeyInfo = Depends(_require_ingest_scope),
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
    from arq import ArqRedis  # type: ignore[import-untyped]

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
        await arq_pool.enqueue_job(
            "ingest_document_task",
            str(job.id),
            namespace,
            filename,
            file_content,
        )
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
    log_metadata: dict,
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
