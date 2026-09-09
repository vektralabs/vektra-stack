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

import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import structlog
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
    Query,
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
    ERR_INGEST_005,
    ErrorCategory,
    ErrorResponse,
    http_status_for,
)
from vektra_shared.types import HIDDEN_SOURCE_KEY

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Per-document metadata (FEAT-026)
# ---------------------------------------------------------------------------

# One JSON field rather than one form field per attribute: the caller is an
# automation pipeline that already needs course_id/module_id (documented in
# ChunkMetadata, never passable until now), and a new attribute must not mean
# a new API field every time.
METADATA_MAX_BYTES = 4096
METADATA_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Flat and scalar on purpose: the value ends up in a vector store payload that
# has to stay filterable, and `bool` before `int` matters nowhere here because
# isinstance covers both, but nesting would.
METADATA_SCALAR_TYPES = (str, int, float, bool)


def _metadata_error(message: str, remediation: str) -> HTTPException:
    err = ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_INGEST_005,
        message=message,
        remediation=remediation,
    )
    return HTTPException(status_code=http_status_for(err), detail=err.to_envelope())


def _parse_metadata_form(raw: str | None) -> dict[str, Any] | None:
    """Validate the ``metadata`` form field into chunk metadata (FEAT-026).

    Returns None when the field is absent, which is not the same as an empty
    object: absent means "attach nothing", `{}` means the caller sent an empty
    document metadata set. Both end up attaching nothing, but only the second
    is a caller decision, and the distinction keeps the audit log honest.

    Rejects rather than coerces. `hidden_from_students` in particular must be a
    JSON boolean: the string "false" is truthy in Python, so a coercing parser
    would hide every source in a namespace on a pipeline's quoting mistake, and
    the string "true" would work by accident, which is worse — it would teach
    the caller that quoting does not matter until the day it does.
    """
    if raw is None:
        return None

    if len(raw.encode("utf-8")) > METADATA_MAX_BYTES:
        raise _metadata_error(
            f"metadata exceeds {METADATA_MAX_BYTES} bytes.",
            "Send only the attributes needed for filtering and visibility.",
        )

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _metadata_error(
            f"metadata is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno}).",
            'Send a JSON object, for example {"hidden_from_students": true}.',
        ) from exc

    if not isinstance(parsed, dict):
        raise _metadata_error(
            f"metadata must be a JSON object, got {type(parsed).__name__}.",
            'Send a JSON object, for example {"course_id": "INF-2026"}.',
        )

    for key, value in parsed.items():
        # fullmatch, not match: `$` also matches before a trailing newline, so
        # `match` accepts the key "course_id\n" and lets it through to the
        # vector store payload (verified against a running stack).
        if not METADATA_KEY_PATTERN.fullmatch(key):
            raise _metadata_error(
                f"metadata key {key!r} is not allowed.",
                "Keys must match [a-z][a-z0-9_]{0,63}.",
            )
        if not isinstance(value, METADATA_SCALAR_TYPES):
            raise _metadata_error(
                f"metadata value for {key!r} must be a string, number or boolean, "
                f"got {type(value).__name__}.",
                "Flatten nested values; the vector store payload holds scalars.",
            )

    hidden = parsed.get(HIDDEN_SOURCE_KEY)
    if hidden is not None and not isinstance(hidden, bool):
        raise _metadata_error(
            f"metadata field {HIDDEN_SOURCE_KEY!r} must be a JSON boolean, "
            f"got {type(hidden).__name__}.",
            f"Send {HIDDEN_SOURCE_KEY}: true or false, unquoted.",
        )

    return parsed


def _resolve_request_id(request: Request) -> UUID:
    """Return ``request.state.request_id`` or synthesize a fallback ``uuid4()``.

    Always returns a UUID so audit logging never silently skips sensitive
    endpoints (NFR-007) when the request-id middleware misbehaves. Emits a
    structlog warning on the fallback path so misconfiguration is observable.
    """
    rid: UUID | None = getattr(request.state, "request_id", None)
    if rid is not None:
        return rid
    fallback = uuid4()
    log.warning("request_id_middleware_missing_fallback", fallback=str(fallback))
    return fallback


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
    status: str  # "pending", "rejected", or "failed"
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
    namespace: str = Query("default"),
    namespace_form: str | None = Form(
        None, description="Target namespace (overrides query param)"
    ),
    metadata: str | None = Form(
        None,
        description=(
            "Flat JSON object merged into every chunk's metadata (FEAT-026). "
            'Reserved key: {"hidden_from_students": true} keeps the content '
            "retrievable but withholds its source from query responses."
        ),
    ),
    session: AsyncSession = Depends(get_session),
    key_info: ApiKeyInfo = Depends(require_scope("ingest")),
) -> Any:
    """Ingest a document.

    Accepts multipart/form-data with:
    - file: the document to ingest (PDF, DOCX, PPTX)
    - namespace: target namespace (query param or form field, default "default")
    - metadata: optional flat JSON object attached to every chunk (FEAT-026)

    Returns:
    - 200 + {document_id, chunk_count, status} for sync path (<= 10MB)
    - 202 + {job_id, status} for async path (> 10MB)
    - 409 for same filename with different content (REQ-033)
    - 413 for file too large (ERR-INGEST-002)
    - 422 for malformed metadata (ERR-INGEST-005)
    """
    # Form field overrides query param when explicitly provided
    if namespace_form is not None:
        namespace = namespace_form

    # Before reading the upload: a malformed metadata field is the caller's
    # mistake and costs nothing to reject early.
    extra_metadata = _parse_metadata_form(metadata)

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
            extra_metadata=extra_metadata,
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
            extra_metadata=extra_metadata,
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
            action="ingest_error",
            log_metadata={
                "namespace": namespace,
                "filename": filename,
                "error_code": error_code,
            },
        )
    else:
        assert result is not None
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
    metadata: str | None = Form(
        None,
        description=(
            "Flat JSON object merged into every chunk of every file in this "
            "request (FEAT-026). Same shape as POST /api/v1/ingest."
        ),
    ),
    session: AsyncSession = Depends(get_session),
    key_info: ApiKeyInfo = Depends(require_scope("ingest")),
) -> JSONResponse:
    """Ingest multiple documents asynchronously.

    Accepts multipart/form-data with multiple file fields named 'files'.
    All files are processed asynchronously (always 202). Files exceeding
    VEKTRA_MAX_FILE_SIZE_MB are reported as 'rejected' in the response
    array without creating a job.

    An optional `metadata` field applies to every file in the request
    (FEAT-026); a 422 rejects the whole batch rather than ingesting some of
    the files without the visibility flag the caller asked for.
    """
    extra_metadata = _parse_metadata_form(metadata)

    if not files:
        raise HTTPException(
            status_code=422,
            detail={"error": {"message": "No files provided"}},
        )

    ingest_config = IngestConfig()
    max_bytes = ingest_config.max_file_size_mb * 1024 * 1024
    registry = getattr(request.app.state, "registry", None)

    items: list[dict[str, Any]] = []

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

        enqueued = await _enqueue_batch_file(
            job_id=job.id,
            file_content=file_content,
            filename=filename,
            namespace=namespace,
            registry=registry,
            background_tasks=background_tasks,
            request=request,
            extra_metadata=extra_metadata,
        )

        items.append(
            BatchIngestItemResponse(
                job_id=job.id,
                filename=filename,
                status="pending" if enqueued else "failed",
                error="Failed to enqueue job" if not enqueued else None,
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

    # Commit soft-deletes before external cleanup to avoid inconsistency:
    # if commit fails, no vectors have been deleted yet.
    await session.commit()

    # Best-effort vector cleanup after DB state is persisted
    if registry is not None and deleted:
        try:
            vector_store = registry.get("vector_store", "default")
        except ValueError:
            vector_store = None
        if vector_store is not None:
            for doc_id_str in deleted:
                try:
                    await vector_store.delete(body.namespace, [doc_id_str])
                except Exception as exc:
                    log.warning(
                        "batch_delete_chunks_failed",
                        document_id=doc_id_str,
                        error=str(exc),
                    )

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
) -> list[dict[str, Any]]:
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

    req = ExtractionRequest(
        content=file_content, content_type=content_type, filename=filename
    )
    elements = []
    async for chunk in await extractor.extract(req):
        elements.append(
            {
                "text": chunk.text,
                "element_type": chunk.element_type.value,
                "content_format": chunk.content_format,
                "metadata": chunk.metadata,
            }
        )

    return elements


@router.post("/api/v1/ingest/chunk", status_code=200)
async def chunk_only(
    file: UploadFile,
    _key: ApiKeyInfo = Depends(require_scope("ingest")),
) -> list[dict[str, Any]]:
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

    req = ExtractionRequest(
        content=file_content, content_type=content_type, filename=filename
    )
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
        chunks.append(
            {
                "text": chunk.text,
                "element_type": chunk.element_type.value,
                "content_format": chunk.content_format,
                "metadata": chunk.metadata,
            }
        )

    return chunks


@router.post("/api/v1/ingest/embed", status_code=200)
async def embed_only(
    request: Request,
    file: UploadFile,
    _key: ApiKeyInfo = Depends(require_scope("ingest")),
) -> list[dict[str, Any]]:
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

    req = ExtractionRequest(
        content=file_content, content_type=content_type, filename=filename
    )
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
    if registry is None:
        raise HTTPException(
            status_code=500,
            detail={"error": {"message": "Embedding provider not configured"}},
        )
    embedding_provider = registry.get("embedding", "default")
    texts = [c.text for c in all_chunks]
    embeddings = await embedding_provider.embed_documents(texts)

    if len(embeddings) != len(all_chunks):
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "message": (
                        f"Embedding count ({len(embeddings)}) does not match "
                        f"chunk count ({len(all_chunks)})"
                    ),
                }
            },
        )

    results = []
    for i, (chunk, embedding) in enumerate(zip(all_chunks, embeddings)):
        results.append(
            {
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
            }
        )

    return results


@router.get("/api/v1/ingest/jobs/{job_id}/status", response_model=JobStatusResponse)
async def get_job_status(
    job_id: UUID,
    namespace: str = "default",
    session: AsyncSession = Depends(get_session),
    key_info: ApiKeyInfo = Depends(require_scope("ingest")),
) -> JobStatusResponse:
    """Return current status of an async ingest job (REQ-014, NFR-010)."""
    from vektra_ingest.models import IngestJobOrm

    # Enforce namespace binding for scoped keys
    effective_ns = key_info.namespace_id or namespace

    result = await session.execute(
        select(IngestJobOrm).where(
            IngestJobOrm.id == job_id,
            IngestJobOrm.namespace_id == effective_ns,
        )
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
    extra_metadata: dict[str, Any] | None = None,
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
                extra_metadata,
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
            extra_metadata=extra_metadata,
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
    extra_metadata: dict[str, Any] | None = None,
) -> bool:
    """Enqueue a single file from a batch ingest request.

    Returns True if the job was successfully enqueued, False on failure.
    """
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
                extra_metadata,
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
            return False
    else:
        background_tasks.add_task(
            _run_task_in_background,
            job_id=str(job_id),
            namespace_id=namespace,
            filename=filename,
            file_bytes=file_content,
            registry=registry,
            extra_metadata=extra_metadata,
        )
    return True


async def _run_task_in_background(
    *,
    job_id: str,
    namespace_id: str,
    filename: str,
    file_bytes: bytes,
    registry: Any,
    extra_metadata: dict[str, Any] | None = None,
) -> None:
    """Background fallback for async ingest when arq is not configured."""
    from vektra_ingest.jobs import ingest_document_task

    ctx = {"registry": registry}
    await ingest_document_task(
        ctx, job_id, namespace_id, filename, file_bytes, extra_metadata
    )


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

    request_id = _resolve_request_id(request)

    background_tasks.add_task(
        log_event,
        key_id=key_info.key_id,
        endpoint=request.url.path,
        method=request.method,
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

    request_id = _resolve_request_id(request)

    try:
        await log_event(
            key_id=key_info.key_id,
            endpoint=request.url.path,
            method=request.method,
            status_code=status_code,
            request_id=request_id,
            action=action,
            log_metadata=log_metadata,
        )
    except Exception as exc:
        log.warning("audit_log_direct_failed", error=str(exc))
