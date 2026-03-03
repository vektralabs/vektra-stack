"""IngestPipeline: core document ingestion logic (REQ-002, BR-003, BR-005, ARCH-009).

Handles:
- SHA-256 deduplication (REQ-033, REQ-034)
- Filename alias tracking (BR-005)
- Content type detection (REQ-058, ARCH-042)
- DocumentExtractor dispatch (ARCH-009)
- FixedSizeChunking (ARCH-037)
- Embedding via shared EmbeddingProvider (from ProviderRegistry)
- Storage via VectorStoreProvider (from ProviderRegistry, session-managed adapter)
- source_document and ingest_job lifecycle management
- BR-003: no orphan chunks on failure (soft-delete compensating action)

Audit logging is the caller's responsibility (API layer writes audit entries
so it can include key_id and request_id from the request context).
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vektra_ingest.chunking import DualStrategyChunking, FixedSizeChunking
from vektra_ingest.detection import detect_content_type
from vektra_ingest.exceptions import IngestConflictError, IngestError
from vektra_ingest.extractors.markdown import MarkdownExtractor
from vektra_ingest.extractors.pdf import PdfplumberExtractor
from vektra_ingest.extractors.powerpoint import PowerPointExtractor
from vektra_ingest.extractors.word import WordExtractor
from vektra_ingest.models import SourceDocumentOrm
from vektra_shared.config import IngestConfig
from vektra_shared.errors import ERR_INGEST_001, ERR_INGEST_004
from vektra_shared.types import ChunkEmbedding, ExtractionRequest

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class IngestResult:
    """Outcome of a run_ingest() call.

    status values:
    - "indexed": document was successfully extracted, embedded, and stored.
    - "exists": exact duplicate (same hash + same filename). No work done.
    - "alias": same content, different filename. Alias added to existing doc.
    - (failures raise IngestError or IngestConflictError; not represented here)
    """

    status: str
    document_id: UUID | None = None
    chunk_count: int | None = None
    alias_count: int | None = None  # only set when status="alias"
    version: int = 1  # document version (REQ-056)
    supersedes_id: UUID | None = None  # previous version's document_id
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Extractor registry
# ---------------------------------------------------------------------------


def _build_extractor_registry() -> dict[str, Any]:
    """Build content_type → extractor mapping for all supported types.

    When VEKTRA_DOCUMENT_EXTRACTOR=unstructured and the package is installed,
    UnstructuredExtractor handles application/pdf instead of PdfplumberExtractor.
    """
    config = IngestConfig()

    registry: dict[str, Any] = {}
    extractors: list[Any] = [
        PdfplumberExtractor(),
        WordExtractor(),
        PowerPointExtractor(),
        MarkdownExtractor(),
    ]
    for extractor in extractors:
        for mime_type in extractor.supported_types():
            registry[mime_type] = extractor

    # Override PDF extractor when Unstructured is configured
    if config.document_extractor == "unstructured":
        try:
            from vektra_ingest.extractors.unstructured import UnstructuredExtractor

            unstructured_ext = UnstructuredExtractor()
            for mime_type in unstructured_ext.supported_types():
                registry[mime_type] = unstructured_ext
            log.info("extractor_registry", pdf_extractor="unstructured")
        except ImportError:
            log.warning(
                "extractor_registry_fallback",
                reason="unstructured package not installed, falling back to pdfplumber",
            )

    return registry


_EXTRACTORS: dict[str, Any] | None = None


def _get_extractor(content_type: str) -> Any:
    global _EXTRACTORS
    if _EXTRACTORS is None:
        _EXTRACTORS = _build_extractor_registry()
    return _EXTRACTORS.get(content_type)


# ---------------------------------------------------------------------------
# Core ingestion function
# ---------------------------------------------------------------------------


async def run_ingest(
    *,
    file_content: bytes,
    filename: str,
    namespace: str,
    session: AsyncSession,
    registry: Any,  # ProviderRegistry - typed loosely to avoid circular import
    on_phase: Callable[[str, int | None], Awaitable[None]] | None = None,
) -> IngestResult:
    """Execute the full document ingestion pipeline.

    Steps:
    1. SHA-256 hash for deduplication
    2. Duplicate detection (exists / alias / conflict)
    3. Magic bytes content type detection
    4. DocumentExtractor dispatch + extraction
    5. FixedSizeChunking
    6. Embedding via EmbeddingProvider
    7. Storage via VectorStoreProvider
    8. source_document.chunk_count update

    Args:
        file_content: Raw file bytes.
        filename: Original filename (used for dedup and alias tracking).
        namespace: Target namespace ID.
        session: AsyncSession for source_documents + ingest_jobs writes.
        registry: ProviderRegistry providing "embedding" and "vector_store".
        on_phase: Optional progress callback (phase_name, percentage).

    Raises:
        IngestConflictError: Concurrent re-ingest TOCTOU race (→ 409).
        IngestError: Unsupported type, scanned PDF, or storage failure.
    """
    content_hash = hashlib.sha256(file_content).hexdigest()

    # ------------------------------------------------------------------
    # Step 1: Check for exact duplicate or alias (same content_hash)
    # ------------------------------------------------------------------
    result = await session.execute(
        select(SourceDocumentOrm).where(
            SourceDocumentOrm.namespace_id == namespace,
            SourceDocumentOrm.content_hash == content_hash,
            SourceDocumentOrm.deleted_at.is_(None),
        )
    )
    existing = result.scalar_one_or_none()

    if existing is not None:
        if existing.filename == filename:
            # Exact duplicate: same content, same filename
            log.info(
                "ingest_dedup_exact",
                document_id=str(existing.id),
                namespace=namespace,
            )
            return IngestResult(
                status="exists",
                document_id=existing.id,
                version=existing.version,
                supersedes_id=existing.supersedes_id,
            )

        # Same content, different filename → add alias (BR-005)
        aliases: list[str] = list(existing.filename_aliases or [])
        if filename not in aliases:
            aliases.append(filename)
            await session.execute(
                update(SourceDocumentOrm)
                .where(SourceDocumentOrm.id == existing.id)
                .values(filename_aliases=aliases, updated_at=datetime.now(UTC))
            )
            await session.commit()
        log.info(
            "ingest_dedup_alias",
            document_id=str(existing.id),
            namespace=namespace,
            alias=filename,
        )
        return IngestResult(
            status="alias",
            document_id=existing.id,
            alias_count=len(aliases),
            version=existing.version,
            supersedes_id=existing.supersedes_id,
        )

    # ------------------------------------------------------------------
    # Step 2: Check for filename match (different content, same name)
    #
    # Phase 2 versioning: instead of 409 Conflict, create a new version.
    # Soft-delete old document (reason="superseded") and proceed with
    # ingestion. Old chunks are hard-deleted only after the new version
    # is successfully stored (Step 7b). IngestConflictError is still
    # raised for TOCTOU race conditions (unique partial index / TECH-004).
    # ------------------------------------------------------------------
    new_version = 1
    supersedes_id: UUID | None = None
    old_version: int | None = None

    result = await session.execute(
        select(SourceDocumentOrm).where(
            SourceDocumentOrm.namespace_id == namespace,
            SourceDocumentOrm.filename == filename,
            SourceDocumentOrm.deleted_at.is_(None),
        )
    )
    existing_by_name = result.scalar_one_or_none()
    if existing_by_name is not None:
        new_version = existing_by_name.version + 1
        supersedes_id = existing_by_name.id
        old_version = existing_by_name.version

        log.info(
            "ingest_version_increment",
            old_document_id=str(existing_by_name.id),
            old_version=existing_by_name.version,
            new_version=new_version,
            namespace=namespace,
            filename=filename,
        )

        # Soft-delete the old document
        await session.execute(
            update(SourceDocumentOrm)
            .where(SourceDocumentOrm.id == existing_by_name.id)
            .values(
                deleted_at=datetime.now(UTC),
                deletion_reason="superseded",
            )
        )
        await session.commit()
        # Old chunks remain in vector store until new ingest succeeds.
        # They won't appear in search (JOIN excludes soft-deleted docs).

    # ------------------------------------------------------------------
    # Steps 3-7: Detect → Extract → Chunk → Embed → Store
    #
    # Wrapped in a single try/except so that any failure after the
    # superseded document was soft-deleted (Step 2) triggers
    # _restore_superseded_document to undo the soft-delete.
    # ------------------------------------------------------------------
    doc_id: UUID | None = None
    try:
        # Step 3: Detect content type
        content_type = detect_content_type(file_content, filename)

        # Step 4: Dispatch to DocumentExtractor
        extractor = _get_extractor(content_type)
        if extractor is None:
            raise IngestError(
                error_code=ERR_INGEST_001,
                message=f"Unsupported content type: '{content_type}'. "
                f"Supported: PDF, DOCX, PPTX, Markdown.",
            )

        # Step 5: Create source_document record
        doc = SourceDocumentOrm(
            namespace_id=namespace,
            filename=filename,
            content_hash=content_hash,
            content_type=content_type,
            file_size_bytes=len(file_content),
            version=new_version,
            supersedes_id=supersedes_id,
        )
        session.add(doc)
        await session.flush()  # assigns doc.id without committing
        doc_id = doc.id

        # Commit the source_document so the VectorStoreProvider (which uses
        # its own session) can reference it via FK (document_chunks.document_id).
        await session.commit()
        if on_phase is not None:
            await on_phase("extracting", None)

        extraction_req = ExtractionRequest(
            content=file_content,
            content_type=content_type,
            filename=filename,
        )
        elements_iter = await extractor.extract(extraction_req)

        if on_phase is not None:
            await on_phase("chunking", None)

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
        chunks_iter = await chunker.chunk(elements_iter)

        # Collect all chunks
        all_chunks = []
        async for chunk in chunks_iter:
            all_chunks.append(chunk)

        if not all_chunks:
            raise IngestError(
                error_code=ERR_INGEST_001,
                message="No extractable text found in document.",
            )

        if on_phase is not None:
            await on_phase("embedding", 50)

        # Embed
        embedding_provider = registry.get("embedding", "default")
        texts = [c.text for c in all_chunks]
        embeddings = await embedding_provider.embed_documents(texts)

        if len(embeddings) != len(all_chunks):
            raise IngestError(
                error_code=ERR_INGEST_004,
                message=(
                    f"Embedding count mismatch: got {len(embeddings)} embeddings "
                    f"for {len(all_chunks)} chunks."
                ),
            )

        # Build ChunkEmbedding objects with document_id in metadata
        chunk_embeddings = [
            ChunkEmbedding(
                chunk_id=f"{doc_id}_{i}",
                text=chunk.text,
                dense=embedding,
                metadata={
                    **chunk.metadata,
                    "document_id": str(doc_id),
                    "content_type": content_type,
                    "element_type": chunk.element_type.value,
                    "position": i,
                },
            )
            for i, (chunk, embedding) in enumerate(zip(all_chunks, embeddings))
        ]

        # Store via VectorStoreProvider (manages its own session internally)
        vector_store = registry.get("vector_store", "default")
        chunk_ids = await vector_store.store(namespace, chunk_embeddings)

    except (IngestError, IngestConflictError) as exc:
        # These are expected errors - clean up if doc was already persisted
        if doc_id is not None:
            await _cleanup_document(doc_id)
        if supersedes_id is not None:
            await _restore_superseded_document(supersedes_id)
        _emit_failed_event(registry, doc_id, namespace, filename, exc)
        raise
    except Exception as exc:
        log.error(
            "ingest_storage_failed",
            document_id=str(doc_id),
            namespace=namespace,
            error=str(exc),
        )
        if doc_id is not None:
            await _cleanup_document(doc_id)
        if supersedes_id is not None:
            await _restore_superseded_document(supersedes_id)
        ingest_err = IngestError(
            error_code=ERR_INGEST_004,
            message=f"Storage failed: {exc}",
        )
        _emit_failed_event(registry, doc_id, namespace, filename, ingest_err)
        raise ingest_err from exc

    # ------------------------------------------------------------------
    # Step 7b: Finalize superseded document (only after successful store)
    # ------------------------------------------------------------------
    if supersedes_id is not None:
        # Hard-delete old chunks (best-effort, old doc already soft-deleted)
        try:
            await vector_store.delete(namespace, [str(supersedes_id)])
        except Exception as exc:
            log.warning(
                "ingest_old_chunks_delete_failed",
                old_document_id=str(supersedes_id),
                error=str(exc),
            )

        # Emit document.superseded event (best-effort)
        try:
            events = registry.get("events", "default")
            await events.emit(
                "document.superseded",
                {
                    "document_id": str(supersedes_id),
                    "namespace": namespace,
                    "filename": filename,
                    "old_version": old_version,
                    "new_version": new_version,
                },
            )
        except Exception:
            pass  # emitter not registered or delivery failed

    # ------------------------------------------------------------------
    # Step 8: Update chunk_count
    # ------------------------------------------------------------------
    try:
        await session.execute(
            update(SourceDocumentOrm)
            .where(SourceDocumentOrm.id == doc_id)
            .values(chunk_count=len(chunk_ids), updated_at=datetime.now(UTC))
        )
        await session.commit()
    except Exception as exc:
        # Non-fatal: document is indexed; chunk_count is a convenience field
        log.warning(
            "ingest_chunk_count_update_failed",
            document_id=str(doc_id),
            error=str(exc),
        )

    # Emit document.indexed event (best-effort)
    try:
        events = registry.get("events", "default")
        await events.emit(
            "document.indexed",
            {
                "document_id": str(doc_id),
                "namespace": namespace,
                "chunk_count": len(chunk_ids),
                "filename": filename,
            },
        )
    except Exception:
        pass  # emitter not registered or delivery failed

    log.info(
        "ingest_complete",
        document_id=str(doc_id),
        namespace=namespace,
        filename=filename,
        chunk_count=len(chunk_ids),
        content_type=content_type,
    )

    return IngestResult(
        status="indexed",
        document_id=doc_id,
        chunk_count=len(chunk_ids),
        version=new_version,
        supersedes_id=supersedes_id,
    )


async def _cleanup_document(doc_id: UUID) -> None:
    """Soft-delete a source_document after a failed ingestion.

    Opens its own session so callers don't need to manage session state
    after an exception.
    """
    import vektra_shared.db as _shared_db

    if _shared_db._session_factory is None:
        log.error("cleanup_failed_no_session_factory", document_id=str(doc_id))
        return

    try:
        async with _shared_db._session_factory() as session:
            await session.execute(
                update(SourceDocumentOrm)
                .where(SourceDocumentOrm.id == doc_id)
                .values(
                    deleted_at=datetime.now(UTC),
                    deletion_reason="user_request",
                )
            )
            await session.commit()
    except Exception as exc:
        log.error(
            "cleanup_document_failed",
            document_id=str(doc_id),
            error=str(exc),
        )


async def _restore_superseded_document(old_doc_id: UUID) -> None:
    """Restore a superseded document after new version ingest failed.

    Clears the soft-delete so the old version becomes active again.
    Uses its own session (same pattern as _cleanup_document).
    """
    import vektra_shared.db as _shared_db

    if _shared_db._session_factory is None:
        log.error("restore_failed_no_session_factory", document_id=str(old_doc_id))
        return

    try:
        async with _shared_db._session_factory() as session:
            await session.execute(
                update(SourceDocumentOrm)
                .where(SourceDocumentOrm.id == old_doc_id)
                .values(deleted_at=None, deletion_reason=None)
            )
            await session.commit()
        log.info("superseded_document_restored", document_id=str(old_doc_id))
    except Exception as exc:
        log.error(
            "restore_superseded_failed",
            document_id=str(old_doc_id),
            error=str(exc),
        )


def _emit_failed_event(
    registry: Any,
    doc_id: UUID | None,
    namespace: str,
    filename: str,
    error: Exception,
) -> None:
    """Best-effort emit document.failed event (fire-and-forget)."""
    try:
        events = registry.get("events", "default")
        import asyncio

        error_code = getattr(error, "error_code", "ERR-INGEST-004")
        error_message = getattr(error, "message", str(error))

        asyncio.get_running_loop().create_task(
            events.emit(
                "document.failed",
                {
                    "document_id": str(doc_id) if doc_id else None,
                    "namespace": namespace,
                    "filename": filename,
                    "error_code": error_code,
                    "error_message": error_message,
                },
            )
        )
    except Exception:
        pass  # emitter not registered or delivery failed
