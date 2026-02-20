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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vektra_ingest.chunking import FixedSizeChunking
from vektra_ingest.detection import detect_content_type
from vektra_ingest.exceptions import IngestConflictError, IngestError
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
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Extractor registry
# ---------------------------------------------------------------------------


def _build_extractor_registry() -> dict[str, Any]:
    """Build content_type → extractor mapping for all supported types."""
    registry: dict[str, Any] = {}
    extractors: list[Any] = [
        PdfplumberExtractor(),
        WordExtractor(),
        PowerPointExtractor(),
    ]
    for extractor in extractors:
        for mime_type in extractor.supported_types():
            registry[mime_type] = extractor
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

    Raises:
        IngestConflictError: Same filename, different content hash (→ 409).
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
            return IngestResult(status="exists", document_id=existing.id)

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
        )

    # ------------------------------------------------------------------
    # Step 2: Check for filename conflict (different content, same name)
    # ------------------------------------------------------------------
    result = await session.execute(
        select(SourceDocumentOrm).where(
            SourceDocumentOrm.namespace_id == namespace,
            SourceDocumentOrm.filename == filename,
            SourceDocumentOrm.deleted_at.is_(None),
        )
    )
    name_conflict = result.scalar_one_or_none()
    if name_conflict is not None:
        raise IngestConflictError(filename=filename, namespace=namespace)

    # ------------------------------------------------------------------
    # Step 3: Detect content type
    # ------------------------------------------------------------------
    content_type = detect_content_type(file_content, filename)

    # ------------------------------------------------------------------
    # Step 4: Dispatch to DocumentExtractor
    # ------------------------------------------------------------------
    extractor = _get_extractor(content_type)
    if extractor is None:
        raise IngestError(
            error_code=ERR_INGEST_001,
            message=f"Unsupported content type: '{content_type}'. "
            f"Supported: PDF, DOCX, PPTX.",
        )

    # ------------------------------------------------------------------
    # Step 5: Create source_document record (flush for UUID, don't commit yet)
    # ------------------------------------------------------------------
    doc = SourceDocumentOrm(
        namespace_id=namespace,
        filename=filename,
        content_hash=content_hash,
        content_type=content_type,
        file_size_bytes=len(file_content),
    )
    session.add(doc)
    await session.flush()  # assigns doc.id without committing
    doc_id: UUID = doc.id

    # Commit the source_document so the VectorStoreProvider (which uses its
    # own session) can reference it via FK (document_chunks.document_id).
    await session.commit()

    # ------------------------------------------------------------------
    # Steps 6-7: Extract → Chunk → Embed → Store
    # ------------------------------------------------------------------
    try:
        extraction_req = ExtractionRequest(
            content=file_content,
            content_type=content_type,
            filename=filename,
        )
        elements_iter = await extractor.extract(extraction_req)

        ingest_config = IngestConfig()
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

    except (IngestError, IngestConflictError):
        # These are expected errors - still need to clean up the source_document
        await _cleanup_document(doc_id)
        raise
    except Exception as exc:
        log.error(
            "ingest_storage_failed",
            document_id=str(doc_id),
            namespace=namespace,
            error=str(exc),
        )
        await _cleanup_document(doc_id)
        raise IngestError(
            error_code=ERR_INGEST_004,
            message=f"Storage failed: {exc}",
        ) from exc

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
    except ValueError:
        pass  # events emitter not registered

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
