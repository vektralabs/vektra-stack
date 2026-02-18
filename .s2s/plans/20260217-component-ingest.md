---
provides_requires:
  provides:
    - "vektra_ingest.pipeline:module"
    - "arq.ingest_tasks:module"
  requires:
    - "vektra_shared.audit.log_event:callable"
---
# Implementation Plan: vektra-ingest - Document processing pipeline and async jobs

**ID**: 20260217-component-ingest
**Status**: active
**Branch**: N/A
**Created**: 2026-02-17T22:42:39Z
**Updated**: 2026-02-17T22:42:39Z

## Traceability

**Source**: component-ingest
**Source Type**: architecture

## References

### Requirements
- REQ-002: WF-INGEST-001: Document Ingestion workflow @.s2s/requirements.md
- REQ-014: vektra-ingest Phase 1 API surface @.s2s/requirements.md
- REQ-016: Phase 1 input constraints @.s2s/requirements.md
- REQ-029: REQ-002 boundary behavior specification (10MB threshold) @.s2s/requirements.md
- REQ-033: Duplicate document handling @.s2s/requirements.md
- REQ-034: Content hash algorithm specification @.s2s/requirements.md
- REQ-035: Audit logging for deduplication events @.s2s/requirements.md
- REQ-040: API endpoint path conventions @.s2s/requirements.md
- REQ-045: Word document text extraction @.s2s/requirements.md
- REQ-046: PowerPoint presentation text extraction @.s2s/requirements.md
- REQ-057: Soft delete for documents @.s2s/requirements.md
- REQ-058: Content type detection via magic bytes @.s2s/requirements.md
- BR-003: Atomic ingestion @.s2s/requirements.md
- BR-004: Document ingestion status model @.s2s/requirements.md
- BR-005: Content hash collision with different filename @.s2s/requirements.md
- NFR-003: Ingest latency target (<30s for 10-page PDF) @.s2s/requirements.md
- NFR-005: Data durability on graceful restart @.s2s/requirements.md
- NFR-010: Long operation progress feedback @.s2s/requirements.md

### Architecture
- ARCH-005: arq job persistence @.s2s/architecture.md
- ARCH-006: Background task architecture @.s2s/architecture.md
- ARCH-009: DocumentExtractor Protocol @.s2s/architecture.md
- ARCH-011: Magic bytes detection @.s2s/architecture.md
- ARCH-037: ChunkingStrategy Protocol @.s2s/architecture.md
- ARCH-042: Content type detection pipeline @.s2s/architecture.md

### Decisions
- ADR-0006: Background tasks with arq @.s2s/decisions/ADR-0006-background-tasks-arq.md
- ADR-0022: SQLAlchemy 2.0 async with asyncpg @.s2s/decisions/ADR-0022-orm-sqlalchemy-async.md

### Dependencies
- 20260217-component-shared
- 20260217-infra-database
- 20260217-component-index
- 20260217-component-admin

## Overview

Implements the document processing pipeline: magic bytes detection, text extraction (PDF/Word/PPT), fixed-size chunking, embedding generation, and storage. Handles both synchronous (<= 10MB) and asynchronous (> 10MB, arq job) ingestion paths. Implements SHA-256 deduplication, filename alias handling, scanned PDF detection, and full GDPR-compliant audit logging for all deduplication events.

## Design Notes

**BLOCKER B-2 resolution**: `document_chunks.content_type` is populated with the MIME type detected by python-magic. If detection fails or returns an unknown type, falls back to `'application/octet-stream'` (never NULL, never empty). This behavior is implemented in the magic bytes detection layer before DocumentExtractor dispatch.

**VectorStoreProvider access pattern**: vektra-ingest does NOT call vektra-index via HTTP, and does NOT import vektra-index code directly (ADR-0005). Instead, infra-app-entrypoint registers a `VectorStoreServiceAdapter` into ProviderRegistry. The adapter wraps `PgvectorProvider` with an internally-managed session, implementing the session-free `VectorStoreProvider` Protocol. vektra-ingest retrieves this adapter from ProviderRegistry via `registry.get(VectorStoreProvider)`. The adapter's `store()` method opens a session internally, calls `PgvectorProvider.store(session, ...)`, and commits. The implementation of `VectorStoreServiceAdapter` lives in `vektra_index/adapters.py` and is created and registered by `infra-app-entrypoint`.

**Audit log boundary**: all ingest audit writes must go through `vektra_shared.audit.log_event()`. This function is implemented in vektra_admin but re-exported from vektra_shared so that other components can call it without importing vektra_admin directly. Direct import of `vektra_admin.audit` from vektra_ingest would violate ADR-0005.

- python-magic requires libmagic on Linux (documented in Dockerfile and getting-started guide).
- Scanned PDF detection: text extraction yields < 100 characters per page (average of first 5 pages). Detection occurs before chunking (fail fast per REQ-016). Rejected with ERR-INGEST-003.
- For > 10MB files, the POST /ingest endpoint creates an ingest_job record, returns 202 with job_id, and enqueues an arq task. Job status survives container restarts because it's persisted in PostgreSQL (ADR-0006).
- The ingest pipeline calls vektra-index `POST /documents/{id}/chunks` after embedding. On failure at the storage step, the source_document is soft-deleted (deletion_reason='user_request') and the job marked FAILED. No orphan chunks possible (BR-003).
- FixedSizeChunking: uses tiktoken for token counting (same tokenizer as LLM providers), respects VEKTRA_CHUNK_SIZE (default 1000) and VEKTRA_CHUNK_OVERLAP (default 200).

## Tasks

- [ ] Create `vektra_ingest/models.py`: SQLAlchemy ORM for `source_documents` and `ingest_jobs` tables
- [ ] Implement `vektra_ingest/detection.py`: magic bytes detection via python-magic (`from_buffer()`), content_type extraction, mismatch logging, fallback to `'application/octet-stream'` on failure
- [ ] Implement `vektra_ingest/extractors/pdf.py` as PdfplumberExtractor implementing DocumentExtractor Protocol: extracts text page by page, scanned PDF detection (< 100 chars/page avg across first 5 pages → ERR-INGEST-003), returns `AsyncIterator[DocumentChunk]` with page_number and ElementType.TEXT
- [ ] Implement `vektra_ingest/extractors/word.py` as WordExtractor implementing DocumentExtractor Protocol: extracts paragraphs and headings via python-docx, logs warnings on tables/images/embedded objects, returns `AsyncIterator[DocumentChunk]`
- [ ] Implement `vektra_ingest/extractors/powerpoint.py` as PowerPointExtractor implementing DocumentExtractor Protocol: extracts slide titles, text boxes, speaker notes in slide order via python-pptx, logs warnings on charts/SmartArt/embedded media
- [ ] Implement `vektra_ingest/chunking.py` as FixedSizeChunking implementing ChunkingStrategy Protocol: tokenizes with tiktoken, splits at VEKTRA_CHUNK_SIZE with VEKTRA_CHUNK_OVERLAP overlap, preserves chunk_index and token_count, returns `AsyncIterator[DocumentChunk]`
- [ ] Implement `vektra_ingest/pipeline.py` as the core ingestion function: compute SHA-256 → check duplicate (REQ-033) → detect content_type → dispatch to DocumentExtractor → FixedSizeChunking → embed via shared EmbeddingProvider (from ProviderRegistry) → store via VectorStoreProvider (from ProviderRegistry, session-managed; see Design Notes for access pattern) → update source_document.chunk_count → emit EventEmitter events
- [ ] Implement deduplication logic: if content_hash exists and not soft-deleted → return 200 with existing document_id and status='exists'; if same content_hash but different filename → update filename_aliases, return 200 with alias count (BR-005); if same filename but different content_hash → return 409 Conflict (REQ-033)
- [ ] Implement `vektra_ingest/jobs.py`: arq task `ingest_document_task(job_id, document_id, file_bytes, namespace_id)` that updates job status/phase throughout execution, handles errors by updating job to FAILED with error_code; configure arq worker with PostgreSQL job store
- [ ] Create `vektra_ingest/api.py` with FastAPI router:
  - `POST /api/v1/ingest`: multipart/form-data with file upload; validate file size (ERR-INGEST-002 if > VEKTRA_MAX_FILE_SIZE_MB); sync path for <= 10MB (returns 200), async path for > 10MB (creates job, returns 202 with job_id); require `ingest` or `admin` scope
  - `GET /api/v1/ingest/jobs/{id}/status`: returns {status, phase, document_id, error_code, error_message}; require `ingest` or `admin` scope
- [ ] Write audit log entries for every ingest attempt (success, failure, dedup, alias): call `vektra_shared.audit.log_event()` — do NOT import `vektra_admin.audit` directly (ADR-0005 boundary violation; vektra_shared re-exports the audit helper so all components can call it without cross-component imports)
- [ ] Benchmark ingestion of a 10-page text-extractable PDF (< 500KB); verify completion within 30s (NFR-003)
- [ ] Write unit tests: scanned PDF detection threshold, deduplication logic (existing, alias, conflict), content_type fallback, chunk boundary calculation, job status transitions
- [ ] Write integration tests: full ingest flow with real PDF → verify chunk count in index, verify job reaches INDEXED status, verify duplicate returns 200 with status='exists', verify > 10MB returns 202 and can be polled to completion

## Acceptance Criteria

- [ ] `POST /ingest` with < 10MB PDF returns 200 with document_id and chunk_count within 30 seconds (NFR-003)
- [ ] `POST /ingest` with > 10MB returns 202 with job_id; polling reaches INDEXED within 60s
- [ ] Scanned PDF (< 100 chars/page average) rejected with ERR-INGEST-003 before chunking
- [ ] Duplicate content hash returns 200 with status='exists' and existing document_id
- [ ] Same filename, different content hash returns 409 Conflict
- [ ] Failed ingestion leaves no orphan chunks in vector store (BR-003)
- [ ] Job status endpoint returns phase field ('extracting', 'chunking', 'embedding') during processing
- [ ] `document_chunks.content_type` is always set (never NULL, fallback to 'application/octet-stream')

## Testing Approach

Unit tests for each extractor (PDF, Word, PPT), chunking logic, and dedup logic using sample files. Integration tests use testcontainers (PostgreSQL + pgvector) and a real embedding model to verify end-to-end flow. Performance test: 10-page PDF ingestion timed. arq job tests use in-process execution (no Redis/worker process needed in tests).

## Integration Notes

vektra-ingest depends on vektra-index to store chunks after embedding. The shared EmbeddingProvider (SentenceTransformersProvider) and VectorStoreProvider (VectorStoreServiceAdapter wrapping PgvectorProvider) are both accessed via ProviderRegistry — do not instantiate a second model or a direct PgvectorProvider in this component. Both are registered by infra-app-entrypoint at startup.

Audit log writes use `vektra_shared.audit.log_event()` — not `vektra_admin.audit` (boundary violation) and not direct SQLAlchemy writes to the audit_log table (fragile, bypasses fire-and-forget logic).

vektra-ingest does NOT communicate with vektra-index via HTTP. It uses the in-process ProviderRegistry. The VectorStoreServiceAdapter manages sessions internally, so vektra-ingest never sees an AsyncSession for vector store operations.
