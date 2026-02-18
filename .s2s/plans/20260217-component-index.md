# Implementation Plan: vektra-index - Vector store, semantic search, metadata filtering

**ID**: 20260217-component-index
**Status**: in_progress
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
- [ ] Register SentenceTransformersProvider and PgvectorProvider in startup via ProviderRegistry (deferred to infra-app-entrypoint plan which creates the app factory)
- [x] Write startup validation steps (ARCH-057 steps 5+6): `vektra_index/startup.py` implemented
- [x] Write unit tests for PgvectorProvider: 8 unit tests passing (test_pgvector_unit.py)
- [ ] Write integration tests (requires PostgreSQL + pgvector): test_integration.py written, skipped without Docker - VERIFY when Docker available
- [ ] Benchmark search latency: 10,000 chunks indexed, 100 queries, verify p95 < 500ms (NFR-002) - NOT YET DONE

## Notes (2026-02-18)

Session stopped here to compact context. Resume from first unchecked task:
- Provider registration in ProviderRegistry is intentionally deferred to infra-app-entrypoint.
- Next: verify integration tests with Docker, then write benchmark test.
- SentenceTransformersProvider test_embedding_provider.py is written but not yet run (uses real model, ~1GB download on first run).

## Acceptance Criteria

- [ ] `POST /search` returns results within 500ms p95 for 10,000 chunks (NFR-002)
- [ ] `DELETE /documents/{id}` removes all chunks atomically in a single transaction and returns correct `chunks_removed` count
- [ ] All search queries include `WHERE index_version = VEKTRA_ACTIVE_INDEX_VERSION` filter
- [ ] Namespace isolation enforced: cross-namespace queries return empty results, not errors
- [ ] JSONB metadata filtering applied in same SQL as vector similarity (not post-filtered)
- [ ] SentenceTransformersProvider loaded once; `dimensions()` returns 384
- [ ] `GET /stats` returns accurate counts without full table scan

## Testing Approach

Unit tests with mocked database sessions cover SQL query construction and error handling. Integration tests against a real PostgreSQL+pgvector instance (testcontainers) cover full store/search/delete cycles and namespace isolation. Performance benchmark (pytest-benchmark) validates NFR-002.

## Integration Notes

vektra-ingest calls `POST /documents/{id}/chunks` after generating embeddings. vektra-core calls `POST /search` during the QueryPipeline embed->search step. The shared EmbeddingProvider instance (SentenceTransformersProvider) is used by both ingest (for indexing) and core (for query embedding) via the ProviderRegistry - this is the primary reason vektra-index must be initialized before the other components.
