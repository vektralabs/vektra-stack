"""arq background tasks for asynchronous document ingestion (ADR-0006, ARCH-005).

The ingest_document_task arq task:
1. Updates the ingest_job to 'processing'
2. Calls run_ingest() with file_bytes
3. Updates job to 'indexed' or 'failed' based on outcome

arq task context (ctx) is populated by the worker startup:
- ctx["registry"]: ProviderRegistry with embedding + vector_store providers

The session factory comes from vektra_shared.db (_session_factory), initialized
by the worker startup before tasks run (NFR-005 durability: PostgreSQL-backed jobs).

Worker settings (get_worker_settings) are used by the vektra-worker entrypoint.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import delete, func, select, update

import vektra_shared.db as _shared_db
from vektra_ingest.exceptions import IngestConflictError, IngestError
from vektra_ingest.models import IngestJobOrm, SourceDocumentOrm
from vektra_ingest.pipeline import run_ingest

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Status update helper
# ---------------------------------------------------------------------------


async def _update_job(
    job_id: UUID,
    *,
    status: str,
    phase: str | None = None,
    document_id: UUID | None = None,
    chunk_count: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    percentage: int | None = None,
) -> None:
    """Update ingest_job status fields in the database."""
    if _shared_db._session_factory is None:
        log.error("job_update_no_session_factory", job_id=str(job_id))
        return

    values: dict[str, Any] = {"status": status}
    if phase is not None:
        values["phase"] = phase
    if document_id is not None:
        values["document_id"] = document_id
    if chunk_count is not None:
        values["chunk_count"] = chunk_count
    if error_code is not None:
        values["error_code"] = error_code
    if error_message is not None:
        values["error_message"] = error_message[:2000] if error_message else None
    if percentage is not None:
        values["percentage"] = percentage

    if status == "processing":
        # Only set started_at on the first processing update (when NULL)
        values["started_at"] = func.coalesce(IngestJobOrm.started_at, datetime.now(UTC))
    elif status in ("indexed", "failed"):
        values["completed_at"] = datetime.now(UTC)
        if "phase" not in values:
            values["phase"] = None  # clear stale phase on terminal status

    try:
        async with _shared_db._session_factory() as session:
            await session.execute(
                update(IngestJobOrm).where(IngestJobOrm.id == job_id).values(**values)
            )
            await session.commit()
    except Exception as exc:
        log.error("job_update_failed", job_id=str(job_id), error=str(exc))


# ---------------------------------------------------------------------------
# arq task
# ---------------------------------------------------------------------------


async def ingest_document_task(
    ctx: dict[str, Any],
    job_id: str,
    namespace_id: str,
    filename: str,
    file_bytes: bytes,
) -> None:
    """arq task: perform async document ingestion.

    Args:
        ctx: arq context dict; ctx["registry"] must be a ProviderRegistry.
        job_id: UUID string of the IngestJobOrm record.
        namespace_id: Target namespace.
        filename: Original filename.
        file_bytes: Raw file content.
    """
    job_uuid = UUID(job_id)
    registry = ctx.get("registry")

    log.info(
        "ingest_task_started",
        job_id=job_id,
        namespace=namespace_id,
        filename=filename,
        file_size=len(file_bytes),
    )

    # Mark as processing
    await _update_job(job_uuid, status="processing", phase="extracting")

    if _shared_db._session_factory is None:
        await _update_job(
            job_uuid,
            status="failed",
            error_code="ERR-INGEST-004",
            error_message="Database not initialized",
        )
        return

    async def _on_phase(phase: str, percentage: int | None) -> None:
        await _update_job(
            job_uuid, status="processing", phase=phase, percentage=percentage
        )

    try:
        async with _shared_db._session_factory() as session:
            result = await run_ingest(
                file_content=file_bytes,
                filename=filename,
                namespace=namespace_id,
                session=session,
                registry=registry,
                on_phase=_on_phase,
            )

        await _update_job(
            job_uuid,
            status="indexed",
            document_id=result.document_id,
            chunk_count=result.chunk_count,
            percentage=100,
        )
        log.info(
            "ingest_task_complete",
            job_id=job_id,
            status=result.status,
            document_id=str(result.document_id) if result.document_id else None,
        )

    except IngestConflictError as exc:
        await _update_job(
            job_uuid,
            status="failed",
            error_code="ERR-INGEST-001",
            error_message=str(exc),
        )
        log.warning("ingest_task_conflict", job_id=job_id, error=str(exc))

    except IngestError as exc:
        await _update_job(
            job_uuid,
            status="failed",
            error_code=exc.error_code,
            error_message=exc.message,
        )
        log.warning("ingest_task_error", job_id=job_id, error_code=exc.error_code)

    except Exception as exc:
        await _update_job(
            job_uuid,
            status="failed",
            error_code="ERR-INGEST-004",
            error_message=str(exc),
        )
        log.error("ingest_task_unexpected_error", job_id=job_id, error=str(exc))


# ---------------------------------------------------------------------------
# Cleanup job (REQ-057)
# ---------------------------------------------------------------------------


async def cleanup_soft_deleted_task(ctx: dict[str, Any]) -> None:
    """arq cron task: hard-delete soft-deleted documents past retention period.

    Reads VEKTRA_RETENTION_DAYS from config. When None, no cleanup is performed.
    For each expired document: hard-delete the row (CASCADE deletes document_chunks).
    """
    from vektra_shared.config import ObservabilityConfig

    config = ObservabilityConfig()
    if config.retention_days is None:
        log.debug("cleanup_skipped", reason="VEKTRA_RETENTION_DAYS not set")
        return

    cutoff = datetime.now(UTC) - timedelta(days=config.retention_days)

    if _shared_db._session_factory is None:
        log.error("cleanup_no_session_factory")
        return

    try:
        async with _shared_db._session_factory() as session:
            # Find expired soft-deleted documents
            result = await session.execute(
                select(SourceDocumentOrm.id).where(
                    SourceDocumentOrm.deleted_at.is_not(None),
                    SourceDocumentOrm.deleted_at < cutoff,
                )
            )
            expired_ids = [row[0] for row in result.all()]

            if not expired_ids:
                log.debug("cleanup_nothing_to_purge")
                return

            # Hard-delete (CASCADE handles document_chunks)
            await session.execute(
                delete(SourceDocumentOrm).where(SourceDocumentOrm.id.in_(expired_ids))
            )
            await session.commit()

        log.info("cleanup_purged", count=len(expired_ids))

    except Exception as exc:
        log.error("cleanup_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Worker settings factory
# ---------------------------------------------------------------------------


def get_worker_settings(registry: Any) -> Any:
    """Return arq WorkerSettings for the vektra-worker process.

    Args:
        registry: ProviderRegistry populated by worker startup.

    The worker process (CMD_TARGET=worker in the same Docker image) calls this
    after init_db() and provider registration.
    """
    from arq import cron

    async def on_startup(ctx: dict[str, Any]) -> None:
        ctx["registry"] = registry

    class WorkerSettings:
        functions = [ingest_document_task]
        cron_jobs = [cron(cleanup_soft_deleted_task, hour=3, minute=0)]
        on_startup = on_startup
        redis_settings = None  # uses REDIS_URL env var via arq default

    return WorkerSettings
