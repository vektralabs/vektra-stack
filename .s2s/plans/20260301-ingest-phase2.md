# Implementation Plan: vektra-ingest - OCR, dual chunking, versioning, batch ops

**ID**: 20260301-ingest-phase2
**Status**: in_progress
**Branch**: feat/phase2-wave1
**Created**: 2026-03-01T14:30:09Z
**Updated**: 2026-03-01T14:30:09Z

## Traceability

**Source**: ingest-phase2
**Source Type**: architecture

## Provides / Requires

**Provides**:
- OCR support for scanned PDFs (consumers: infra-phase2)
- DualStrategyChunking with table preservation and parent-child hierarchy (consumers: infra-phase2)
- Document re-ingestion with version increment (consumers: infra-phase2)
- Batch ingest and delete operations (consumers: infra-phase2)
- Markdown file ingestion (consumers: infra-phase2)
- Webhook event emission for ingest outcomes (consumers: infra-phase2)
- Ingest phase tracking (consumers: infra-phase2)

**Requires**:
- 20260301-shared-protocols-phase2.md: DualStrategyChunking Protocol additions, WebhookEventEmitter implementation
- 20260301-database-phase2.md: unique partial index on source_documents (TECH-004), any schema changes for versioning

## References

### Requirements
- REQ-002: Document ingestion from files @.s2s/requirements.md
- REQ-016: Chunking parameters @.s2s/requirements.md
- REQ-033: Filename dedup with 409 conflict @.s2s/requirements.md
- REQ-054: ChunkingStrategy Protocol @.s2s/requirements.md
- REQ-056: Document versioning in data model @.s2s/requirements.md
- REQ-057: Soft delete for documents @.s2s/requirements.md
- REQ-061: EventEmitter interface @.s2s/requirements.md

### Architecture
- ARCH-037: ChunkingStrategy Protocol (DualStrategyChunking) @.s2s/architecture.md
- ARCH-042: Content type detection via magic bytes @.s2s/architecture.md
- ARCH-044: Chunk metadata with domain-specific fields @.s2s/architecture.md
- ARCH-045: Zero-downtime reindex via index_version @.s2s/architecture.md

### Decisions
- ADR-0006: Background tasks with arq @.s2s/decisions/ADR-0006-background-tasks-arq.md
- ADR-0022: SQLAlchemy 2.0 async with asyncpg @.s2s/decisions/ADR-0022-orm-sqlalchemy-async.md

### Dependencies
- 20260301-shared-protocols-phase2.md
- 20260301-database-phase2.md

## Overview

This plan extends vektra-ingest with OCR support, advanced chunking, document versioning, batch operations, and event emission. It is the largest plan in Wave 1, touching the extraction layer, chunking strategy, pipeline logic, API surface, and background job system.

OCR for scanned PDFs is added via Unstructured as an optional dependency. When installed, PdfplumberExtractor's scanned PDF detection (ERR-INGEST-003) is replaced by an OCR fallback path. When Unstructured is not installed, the existing behavior is preserved: scanned PDFs are rejected. DualStrategyChunking replaces FixedSizeChunking for VEKTRA_CHUNKING_STRATEGY=dual, handling text and table elements differently: text chunks use token-based splitting with overlap (existing behavior), table chunks are never split, and a two-level parent-child hierarchy groups chunks under document sections.

Document versioning changes the conflict resolution behavior: when a file with the same name but different content is re-ingested, instead of a 409 Conflict, a new version is created. The old version is soft-deleted with reason "superseded". Batch operations accept multiple files in a single request and return an array of job IDs. The pipeline gains phase tracking callbacks so the job status endpoint reports extracting/chunking/embedding phases in real time.

## Design Notes

### OCR via Unstructured

Unstructured is an optional dependency. The integration follows the same import-guard pattern used for qdrant-client in index-hybrid.

```python
class UnstructuredExtractor:
    """DocumentExtractor for OCR and advanced element classification."""

    def supported_types(self) -> set[str]:
        return {"application/pdf"}  # overlaps with PdfplumberExtractor

    async def extract(self, request: ExtractionRequest) -> AsyncIterator[DocumentChunk]:
        from unstructured.partition.pdf import partition_pdf
        elements = await asyncio.to_thread(
            partition_pdf,
            file=io.BytesIO(request.content),
            strategy="auto",  # auto-detect: fast for text PDFs, ocr_only for scanned
        )
        for el in elements:
            yield DocumentChunk(
                text=str(el),
                element_type=_map_element_type(el.category),
                content_format=_detect_format(el),
                metadata={...},
                coordinates=_extract_coordinates(el),
            )
```

Extractor selection: when VEKTRA_DOCUMENT_EXTRACTOR=unstructured and the package is installed, UnstructuredExtractor handles PDFs. PdfplumberExtractor remains the default. Both are registered in the extractor registry; the config value determines which one handles `application/pdf`.

Element type mapping from Unstructured categories to Vektra ElementType:
- Title -> TITLE
- NarrativeText, UncategorizedText -> TEXT
- Table -> TABLE
- ListItem -> LIST
- Header -> HEADER
- Footer -> FOOTER
- FigureCaption -> FIGURE_CAPTION
- Formula -> FORMULA
- PageBreak -> PAGE_BREAK
- Image -> IMAGE

### DualStrategyChunking

```python
class DualStrategyChunking:
    def __init__(
        self,
        text_chunk_size: int = 1000,
        text_chunk_overlap: int = 200,
        parent_chunk_size: int = 3000,
    ) -> None: ...

    async def chunk(
        self, elements: AsyncIterator[DocumentChunk],
    ) -> AsyncIterator[DocumentChunk]: ...
```

Behavior by element type:
- TEXT, TITLE, LIST, HEADER, FOOTER, FIGURE_CAPTION, FORMULA: accumulated into text buffer, split with overlap (same as FixedSizeChunking). Each chunk gets a parent_id linking it to a parent chunk.
- TABLE: never split. Each table element becomes exactly one chunk. If a table exceeds `text_chunk_size`, it is stored as-is with content_format="html".
- PAGE_BREAK, IMAGE: not chunked (metadata-only markers, skipped).

Parent-child hierarchy (2 levels):
- Level 0 (parent): every `parent_chunk_size` tokens, emit a parent DocumentChunk with the full accumulated text. parent_id = None.
- Level 1 (child): the normal fixed-size chunks with overlap. parent_id = ID of the enclosing parent chunk.

Parent chunks are stored alongside child chunks but are not directly returned in search results (search filters for parent_id IS NOT NULL by default). They serve as context expansion: when a child chunk matches, the pipeline can optionally retrieve the parent for broader context (core-pipeline-v2).

### Document versioning

Note: `source_documents` already has `version INTEGER NOT NULL DEFAULT 1`, `supersedes_id UUID NULL`, and `deletion_reason VARCHAR(32)` with CHECK constraint including `'superseded'` from Phase 1 migration 0001 (forward-compatible data model, ARCH-040). No schema changes needed for versioning.

When re-ingesting a file with the same filename but different content_hash:
1. Phase 1 behavior (409 Conflict) is replaced.
2. Query existing document: `SELECT id, version FROM source_documents WHERE namespace_id = :ns AND filename = :name AND deleted_at IS NULL`.
3. If found: create new SourceDocumentOrm with `version = existing.version + 1`, `supersedes_id = existing.id`.
4. Soft-delete the old document with `deletion_reason = "superseded"`.
5. Hard-delete old document's chunks (they are re-generable artifacts).
6. Proceed with extraction, chunking, embedding, storage for the new version.

The IngestConflictError is no longer raised for this case. It is still raised if a concurrent request creates a conflicting document in the TOCTOU window (caught by the unique partial index from database-phase2 / TECH-004).

### Batch operations

POST /api/v1/ingest/batch accepts multipart/form-data with multiple file fields. For each file:
1. Validate size.
2. Create an IngestJobOrm with status "pending".
3. Enqueue an arq task (or background task).
4. Return array of `{job_id, filename, status: "pending"}`.

Always returns 202 (all files are processed asynchronously, regardless of size). This simplifies the API contract for batch callers.

DELETE /api/v1/documents/batch accepts JSON body `{"document_ids": [...], "namespace": "default"}`. Deletes each document sequentially in a single transaction. Returns `{"deleted": [...], "not_found": [...]}`.

### Markdown ingestion

MarkdownExtractor handles `text/markdown` and `text/x-markdown` MIME types. Detection: magic bytes for markdown are unreliable, so extension-based detection (`.md`, `.markdown`) is the primary method. Add these extensions to the `_EXT_MAP` in detection.py.

Extraction: split on heading boundaries (`# `, `## `, etc.). Each section becomes a DocumentChunk with element_type=TEXT and content_format="markdown". Metadata includes `heading_level` and `section_title`.

No external dependencies needed; basic regex or line-by-line parsing is sufficient.

### Ingest phase tracking (DEBT-006)

Refactor `run_ingest()` to accept an optional progress callback:

```python
async def run_ingest(
    *,
    file_content: bytes,
    filename: str,
    namespace: str,
    session: AsyncSession,
    registry: Any,
    on_phase: Callable[[str, int | None], Awaitable[None]] | None = None,
) -> IngestResult:
```

The callback is invoked at phase transitions:
- `await on_phase("extracting", None)` before extraction
- `await on_phase("chunking", None)` after extraction, before chunking
- `await on_phase("embedding", 50)` after chunking, before embedding (percentage estimate)

In `ingest_document_task`, pass a callback that calls `_update_job(job_uuid, status="processing", phase=..., percentage=...)`.

### Audit log for error responses (DEBT-007)

The current `_write_audit_log` is only called on success paths (200, 202). For error paths (409, 422), the exception handler raises HTTPException before BackgroundTasks runs.

Fix: wrap the sync ingest path in a try/finally that always writes the audit log:

```python
result = None
error = None
try:
    result = await run_ingest(...)
except IngestConflictError as exc:
    error = exc
    ...
except IngestError as exc:
    error = exc
    ...
finally:
    _write_audit_log(
        ...,
        status_code=409 if isinstance(error, IngestConflictError)
                    else 422 if isinstance(error, IngestError)
                    else 200,
        action=f"ingest_{result.status if result else 'error'}",
        ...
    )
```

Use direct `await log_event(...)` in error paths instead of BackgroundTasks to avoid the FastAPI BackgroundTasks + HTTPException issue.

### Arq cleanup job for soft-deleted documents (REQ-057)

A periodic arq job `cleanup_soft_deleted_task` runs on a configurable schedule (default: daily). It queries `source_documents WHERE deleted_at IS NOT NULL AND deleted_at < now() - interval :retention_days`. For each expired document: hard-delete the source_documents row (CASCADE deletes document_chunks). Log the count of purged documents.

Configuration: `VEKTRA_RETENTION_DAYS` (default: None, meaning no automatic cleanup). When None, the job runs but finds nothing to delete.

### Webhook event emission

With the WebhookEventEmitter from shared-protocols-phase2 registered in ProviderRegistry (category="events"), the existing `events.emit("document.indexed", ...)` call in pipeline.py becomes functional. Add corresponding emit calls for:
- `document.failed`: in the exception handlers of `run_ingest()` and `ingest_document_task`
- `document.superseded`: when version increment soft-deletes the old version

No new code in vektra-ingest for the WebhookEventEmitter implementation itself; that is provided by shared-protocols-phase2.

### Granular ingest APIs (EX-010)

Three new endpoints for debugging and custom pipelines:
- POST /api/v1/ingest/extract: accepts a file, returns extracted DocumentChunks as JSON (no chunking, no embedding, no storage).
- POST /api/v1/ingest/chunk: accepts a file, returns chunked DocumentChunks as JSON (extraction + chunking, no embedding, no storage).
- POST /api/v1/ingest/embed: accepts a file, returns ChunkEmbeddings as JSON (extraction + chunking + embedding, no storage).

All require `ingest` or `admin` scope. These are synchronous-only (no background jobs). Size limit: same as sync threshold (10 MB).

## Tasks

### OCR support

- [x] **T1**: Implement UnstructuredExtractor in `vektra-ingest/src/vektra_ingest/extractors/unstructured.py`. Methods: `supported_types()` returns `{"application/pdf"}`, `extract()` uses `partition_pdf(strategy="auto")` with import guard. Map Unstructured element categories to Vektra ElementType. Extract coordinates as BoundingBox when available.
- [x] **T2**: Add `unstructured[pdf]` as optional dependency in `vektra-ingest/pyproject.toml` (under `[project.optional-dependencies]` group `ocr`). Add import guard that logs clear error when package is missing and VEKTRA_DOCUMENT_EXTRACTOR=unstructured.
- [x] **T3**: Update `_build_extractor_registry()` in pipeline.py: when VEKTRA_DOCUMENT_EXTRACTOR=unstructured and the package is available, register UnstructuredExtractor for application/pdf instead of PdfplumberExtractor. When not available, fall back to PdfplumberExtractor and log a warning.
- [x] **T4**: Write unit tests for UnstructuredExtractor: mock `partition_pdf`, verify element type mapping, verify scanned PDF produces text output (not ERR-INGEST-003), verify coordinates extraction.

### DualStrategyChunking

- [x] **T5**: Implement DualStrategyChunking in `vektra-ingest/src/vektra_ingest/chunking.py` alongside FixedSizeChunking. Text elements accumulated and split with overlap (reuse FixedSizeChunking logic). Table elements yielded as-is with content_format="html". Parent-child hierarchy: emit parent chunks every `parent_chunk_size` tokens, child chunks reference parent via parent_id.
- [x] **T6**: Update pipeline.py chunker selection: when VEKTRA_CHUNKING_STRATEGY=dual, instantiate DualStrategyChunking. When "fixed", use FixedSizeChunking (existing behavior).
- [x] **T7**: Write unit tests for DualStrategyChunking: text elements are split with overlap (same as FixedSizeChunking output), table elements are never split, parent chunks are emitted at correct intervals, child chunks have parent_id set, mixed text+table input produces correct output.

### Document versioning

- [x] **T8**: Refactor the conflict detection section of `run_ingest()`. When a filename match with different content_hash is found: instead of raising IngestConflictError, compute `new_version = existing.version + 1`, soft-delete the old document (deletion_reason="superseded"), hard-delete old chunks, then proceed with ingestion for the new version with `supersedes_id = existing.id`.
- [x] **T9**: Update SourceDocumentOrm creation in `run_ingest()` to pass `version` and `supersedes_id` when applicable.
- [x] **T10**: Emit `document.superseded` event via EventEmitter when old version is soft-deleted.
- [x] **T11**: Write tests for versioning: re-ingest same filename with different content creates version 2 with supersedes_id pointing to version 1. Old version is soft-deleted. Search only returns chunks from the latest version.

### Batch operations

- [ ] **T12**: Add `POST /api/v1/ingest/batch` endpoint in api.py. Accept multiple files via multipart/form-data. For each file: validate size, create IngestJobOrm, enqueue arq task. Always return 202 with array of `{job_id, filename, status}`.
- [ ] **T13**: Add `DELETE /api/v1/documents/batch` endpoint. Accept JSON body with `document_ids` list and `namespace`. Delete each document, return `{deleted: [...], not_found: [...]}`. Requires admin scope.
- [ ] **T14**: Write tests for batch ingest: multiple files produce multiple job IDs. Write tests for batch delete: mix of existing and non-existing IDs returns correct categorization.

### Markdown ingestion

- [ ] **T15**: Implement MarkdownExtractor in `vektra-ingest/src/vektra_ingest/extractors/markdown.py`. Supported types: `text/markdown`, `text/x-markdown`. Split on heading boundaries (regex `^#{1,6}\s`). Each section becomes a DocumentChunk with element_type=TEXT, content_format="markdown", metadata includes heading_level and section_title.
- [ ] **T16**: Add markdown MIME types and extensions to `_EXT_MAP` in detection.py: `.md` -> `text/markdown`, `.markdown` -> `text/markdown`.
- [ ] **T17**: Register MarkdownExtractor in `_build_extractor_registry()`.
- [ ] **T18**: Write unit tests: markdown file split on headings, metadata includes heading level, empty sections skipped.

### Ingest phase tracking (DEBT-006)

- [ ] **T19**: Add `on_phase` callback parameter to `run_ingest()` signature. Insert callback calls at phase transitions: before extraction ("extracting"), before chunking ("chunking"), before embedding ("embedding"). When callback is None, skip (backward compatible).
- [ ] **T20**: Update `ingest_document_task` in jobs.py to pass a callback that calls `_update_job(job_uuid, status="processing", phase=..., percentage=...)`.
- [ ] **T21**: Write test: mock callback, verify it is called with correct phase sequence (extracting, chunking, embedding).

### Audit log for error responses (DEBT-007)

- [ ] **T22**: Refactor the sync ingest path in `api.py:ingest()` to use try/finally for audit logging. On error paths (409, 422), call `await log_event(...)` directly instead of via BackgroundTasks. Ensure audit entries include error_code and status_code.
- [ ] **T23**: Write test: simulate 409 and 422 responses, verify audit log entries are written with correct action and status_code.

### Arq cleanup job (REQ-057)

- [ ] **T24**: Implement `cleanup_soft_deleted_task` in jobs.py. Query soft-deleted documents past VEKTRA_RETENTION_DAYS. Hard-delete expired records (CASCADE handles document_chunks). Log count of purged documents.
- [ ] **T25**: Register the cleanup task in `get_worker_settings()`. Configure schedule via arq's `cron_jobs` with default daily execution.
- [ ] **T26**: Write test: create soft-deleted documents with old timestamps, run cleanup, verify they are hard-deleted. Verify documents within retention period are not touched.

### Webhook event emission

- [ ] **T27**: Add `document.failed` event emission in `run_ingest()` exception handlers and in `ingest_document_task` exception handlers. Payload includes document_id (if available), namespace, filename, error_code, error_message.
- [ ] **T28**: Verify `document.indexed` event (already present in pipeline.py) fires correctly when WebhookEventEmitter is registered. Write test with mock EventEmitter.

### Granular ingest APIs (EX-010)

- [ ] **T29**: Add `POST /api/v1/ingest/extract` endpoint: accept file, run content detection + extraction only, return JSON array of DocumentChunks (text, element_type, metadata). No chunking, no embedding.
- [ ] **T30**: Add `POST /api/v1/ingest/chunk` endpoint: accept file, run extraction + chunking, return JSON array of chunked DocumentChunks.
- [ ] **T31**: Add `POST /api/v1/ingest/embed` endpoint: accept file, run extraction + chunking + embedding, return JSON array of ChunkEmbeddings (text, dense vector, sparse vector if available, metadata). No storage.
- [ ] **T32**: Write tests for all three granular endpoints: verify output structure matches expected types.

## Task count assessment

This plan contains 32 tasks across 10 feature groups. This exceeds the 30-task threshold mentioned in the scoping plan. However, the tasks are logically cohesive and share a single component boundary (vektra-ingest). Splitting into two plans (ingest-processing and ingest-integration) would require the second plan to depend on the first, adding coordination overhead without reducing total work. **Recommendation: keep as a single plan but implement in two sessions.** Session 1: T1-T11 (OCR, chunking, versioning). Session 2: T12-T32 (batch ops, markdown, phase tracking, audit, cleanup, webhooks, granular APIs). Add a session boundary note after T11.

## Acceptance Criteria

- [ ] Scanned PDFs are processed with OCR when Unstructured is installed (VEKTRA_DOCUMENT_EXTRACTOR=unstructured)
- [ ] Scanned PDFs are rejected with ERR-INGEST-003 when Unstructured is not installed (existing behavior preserved)
- [ ] DualStrategyChunking splits text elements with overlap, preserves table elements intact, produces parent-child hierarchy
- [ ] VEKTRA_CHUNKING_STRATEGY=fixed still uses FixedSizeChunking (no regression)
- [ ] Re-ingesting a file with same filename and different content creates version N+1 with supersedes_id
- [ ] Old version is soft-deleted with reason "superseded" and its chunks are hard-deleted
- [ ] POST /ingest/batch accepts multiple files and returns array of job IDs (202)
- [ ] DELETE /documents/batch deletes multiple documents and reports not_found IDs
- [ ] Markdown files are extracted and chunked correctly
- [ ] Job status endpoint reports phase transitions: extracting, chunking, embedding (DEBT-006)
- [ ] Audit log entries are written for 409 and 422 error responses (DEBT-007)
- [ ] Cleanup job hard-deletes soft-deleted documents past retention period
- [ ] document.indexed, document.failed, and document.superseded events are emitted via EventEmitter
- [ ] Granular APIs (extract, chunk, embed) return correct intermediate representations
- [ ] All new code has unit tests; existing Phase 1 ingest tests pass without modification
- [ ] Unstructured is an optional dependency (import-guarded, clear error when missing)

## Testing Approach

**Unit tests**: UnstructuredExtractor (mocked partition_pdf), DualStrategyChunking (text/table/mixed input), MarkdownExtractor (heading splitting), versioning logic (version increment, supersedes_id), batch endpoint (multiple files), phase tracking callback, audit log on error paths, cleanup job (soft-deleted documents).

**Integration tests**: Full ingest pipeline with versioning (store v1, re-ingest, verify v2 exists and v1 is soft-deleted). Batch ingest with multiple small files (sync path). Cleanup job with testcontainers PostgreSQL.

**Mocking strategy**: Unstructured's `partition_pdf` is always mocked in unit tests (the library is large and not installed in CI by default). Integration tests for OCR are optional and gated behind a CI flag (`VEKTRA_TEST_OCR=true`).

## Integration Notes

- shared-protocols-phase2 must provide DualStrategyChunking type additions and WebhookEventEmitter before this plan starts. The Protocol interface for ChunkingStrategy does not change (same async chunk() signature); DualStrategyChunking is a new implementation, not a Protocol change.
- database-phase2 must provide the unique partial index on source_documents (TECH-004) before the versioning logic is implemented. Without it, concurrent re-ingestion can create duplicate versions.
- The document.superseded event type must be documented in the EventEmitter emission points. Coordinate with shared-protocols-phase2 to add it to the list.
- infra-phase2 (Wave 5) updates the Dockerfile with optional Unstructured dependencies and registers the cleanup job in the worker settings.

## Notes

<!-- Progress notes during implementation -->
<!-- Session boundary: after completing T11 (versioning), commit progress and resume with T12 (batch ops) in a new session. -->
