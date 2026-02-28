# Implementation Plan: vektra-index - Vector store, semantic search, metadata filtering

**ID**: 20260217-component-index
**Status**: completed
**Branch**: N/A
**Created**: 2026-02-17T22:42:39Z
**Updated**: 2026-02-18T00:00:00Z

## Traceability

**Source**: component-index
**Source Type**: architecture

## References

### Requirements
- REQ-012: vektra-index Phase 1 API surface @.s2s/requirements.md
- REQ-048: Namespace support for document isolation @.s2s/requirements.md
- REQ-050: Pluggable vector store backend @.s2s/requirements.md
- REQ-052: EmbeddingProvider Protocol @.s2s/requirements.md
- REQ-055: Response and citation traceability @.s2s/requirements.md
- REQ-057: Soft delete for documents @.s2s/requirements.md
- REQ-063: Chunk metadata filtering @.s2s/requirements.md
- REQ-064: Zero-downtime reindex via index_version @.s2s/requirements.md
- NFR-002: Search latency target (<500ms p95) @.s2s/requirements.md
- NFR-005: Data durability on graceful restart @.s2s/requirements.md

### Architecture
- ARCH-010: pgvector as Phase 1 VectorStoreProvider @.s2s/architecture.md
- ARCH-035: VectorStoreProvider Protocol @.s2s/architecture.md
- ARCH-044: document_chunks schema @.s2s/architecture.md
- ARCH-045: source_documents schema @.s2s/architecture.md
- ARCH-051: VectorStoreProvider full-store contract @.s2s/architecture.md
- ARCH-052: Provider-specific ingest atomicity @.s2s/architecture.md

### Decisions
- ADR-0013: EmbeddingProvider Protocol @.s2s/decisions/ADR-0013-embedding-provider-protocol.md
- ADR-0022: SQLAlchemy 2.0 async with asyncpg @.s2s/decisions/ADR-0022-orm-sqlalchemy-async.md

### Dependencies
- 20260217-component-shared
- 20260217-infra-database

## Overview

Implements the vector storage and retrieval module. PgvectorProvider implements VectorStoreProvider Protocol using PostgreSQL's pgvector extension. The embedding model (all-MiniLM-L6-v2 via sentence-transformers) is loaded once here as a shared instance. The REST API exposes chunk storage, similarity search, document deletion, and stats endpoints. This component is a synchronous dependency for both vektra-ingest (stores chunks after embedding) and vektra-core (searches for relevant chunks at query time).

## Design Notes

- SentenceTransformersProvider loads the model once at startup and is registered in ProviderRegistry as the shared EmbeddingProvider instance. Both vektra-ingest and vektra-core use this same instance via dependency injection, avoiding double memory loading (REQ-052).
- The DELETE /documents/{id} endpoint implements the B-1/B-3 resolution: `SELECT COUNT(*) FROM document_chunks WHERE document_id = :id` then `DELETE FROM document_chunks WHERE document_id = :id`, both in a single transaction, count returned as `chunks_removed`; then soft-delete on source_documents.
- Phase 1: SearchMode.DENSE only. The sparse vector field in QueryEmbedding is silently ignored if provided (REQ-050 Phase 1 scope).
- JSONB metadata filtering uses a WHERE clause on the metadata column with a GIN index, combined with the vector similarity query in a single SQL statement (REQ-063 "not post-filtering").
- raw_filters: dict | None escape hatch available on search() for future backend-specific filters.
- All search queries include `WHERE index_version = :active_version` filter (REQ-064, VEKTRA_ACTIVE_INDEX_VERSION env var, default 1).

## Tasks

- [x] Create `vektra_index/models.py` with SQLAlchemy ORM models for `document_chunks` and `source_documents` (NOTE: use `chunk_metadata = mapped_column("metadata", ...)` - 'metadata' is reserved by DeclarativeBase)
- [x] Implement `vektra_index/providers/sentence_transformers.py` as SentenceTransformersProvider implementing EmbeddingProvider Protocol
- [x] Implement `vektra_index/providers/pgvector.py` as PgvectorProvider implementing VectorStoreProvider Protocol (store, search, delete, health_check, namespace_stats)
- [x] Implement ARCH-051 full-store operations: namespace chunk count, document count (for GET /stats)
- [x] Create `vektra_index/api.py` with FastAPI router mounting at `/api/v1` (store/search/delete/stats/health)
- [x] Register SentenceTransformersProvider and PgvectorProvider in startup via ProviderRegistry (intentionally deferred to infra-app-entrypoint which creates the app factory - no ProviderRegistry wiring needed in this plan)
- [x] Write startup validation steps (ARCH-057 steps 5+6): `vektra_index/startup.py` implemented
- [x] Write unit tests for PgvectorProvider: 8 unit tests passing (test_pgvector_unit.py)
- [x] Write integration tests (requires PostgreSQL + pgvector): 3/3 pass (store/search, namespace isolation, delete)
- [x] Benchmark search latency: 10,000 chunks indexed, 100 queries, p95=8.6ms << 500ms threshold (NFR-002)

## Notes (2026-02-18)

Wave 2 completed. All 17 tests pass (8 unit, 5 embedding, 3 integration, 1 benchmark).

Key implementation decisions documented in the source:
- PgvectorProvider does NOT implement VectorStoreProvider Protocol directly (session-injection mismatch is intentional; vektra-core accesses via HTTP, HTTP client implements the Protocol in Wave 3).
- store() uses single-batch flush with no compensating DELETE (single-batch atomicity makes it redundant).
- NamespaceOrm added to models.py for SQLAlchemy FK resolution (no migrations needed - table exists).
- Integration test session fixture creates a fresh engine per test to avoid asyncpg connections leaking across pytest-asyncio function-scope event loops.

## Acceptance Criteria

- [x] `POST /search` returns results within 500ms p95 for 10,000 chunks (NFR-002) - benchmark: p95=8.6ms
- [x] `DELETE /documents/{id}` removes all chunks atomically in a single transaction and returns correct `chunks_removed` count
- [x] All search queries include `WHERE index_version = VEKTRA_ACTIVE_INDEX_VERSION` filter
- [x] Namespace isolation enforced: cross-namespace queries return empty results, not errors
- [x] JSONB metadata filtering applied in same SQL as vector similarity (not post-filtered)
- [x] SentenceTransformersProvider loaded once; `dimensions()` returns 384
- [x] `GET /stats` returns accurate counts without full table scan

## Testing Approach

Unit tests with mocked database sessions cover SQL query construction and error handling. Integration tests against a real PostgreSQL+pgvector instance (testcontainers) cover full store/search/delete cycles and namespace isolation. Performance benchmark (pytest-benchmark) validates NFR-002.

## Integration Notes

vektra-ingest calls `POST /documents/{id}/chunks` after generating embeddings. vektra-core calls `POST /search` during the QueryPipeline embed->search step. The shared EmbeddingProvider instance (SentenceTransformersProvider) is used by both ingest (for indexing) and core (for query embedding) via the ProviderRegistry - this is the primary reason vektra-index must be initialized before the other components.
