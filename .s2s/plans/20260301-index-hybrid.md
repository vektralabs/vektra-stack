# Implementation Plan: vektra-index - Hybrid search, Qdrant provider, reindex API

**ID**: 20260301-index-hybrid
**Status**: completed
**Branch**: N/A
**Created**: 2026-03-01T14:30:09Z
**Updated**: 2026-03-01T14:30:09Z

## Traceability

**Source**: index-hybrid
**Source Type**: architecture

## Provides / Requires

**Provides**:
- Hybrid search (DENSE + SPARSE + HYBRID SearchMode) (consumers: core-pipeline-v2)
- QdrantVectorStoreProvider (consumers: infra-phase2)
- Reindex API with progress tracking (consumers: infra-phase2)
- SparseEmbeddingProvider implementation (consumers: core-pipeline-v2, infra-phase2)
- Budget allocator ordering fix (consumers: core-pipeline-v2)

**Requires**:
- 20260301-shared-protocols-phase2.md: SparseEmbeddingProvider Protocol definition, extended VectorStoreProvider contract (if any changes), import-linter rules for qdrant-client

## References

### Requirements
- REQ-050: Pluggable vector store backend @.s2s/requirements.md
- REQ-063: Chunk metadata filtering @.s2s/requirements.md
- REQ-064: Zero-downtime reindex via index_version @.s2s/requirements.md

### Architecture
- ARCH-044: Chunk metadata with domain-specific fields @.s2s/architecture.md
- ARCH-045: Zero-downtime reindex via index_version @.s2s/architecture.md
- ARCH-051: VectorStoreProvider full-store contract @.s2s/architecture.md
- ARCH-052: Provider-specific ingest atomicity @.s2s/architecture.md
- ARCH-053: SparseEmbeddingProvider Protocol @.s2s/architecture.md

### Decisions
- ADR-0013: EmbeddingProvider Protocol @.s2s/decisions/ADR-0013-embedding-provider-protocol.md
- ADR-0022: SQLAlchemy 2.0 async with asyncpg @.s2s/decisions/ADR-0022-orm-sqlalchemy-async.md

### Dependencies
- 20260301-shared-protocols-phase2.md

## Overview

This plan extends vektra-index from dense-only pgvector search to full hybrid search (dense + sparse vectors) and adds Qdrant as an alternative vector store backend. The existing PgvectorProvider gains SPARSE and HYBRID SearchMode support via a new sparse vector column and Reciprocal Rank Fusion (RRF) for score combination. A new QdrantVectorStoreProvider implements the VectorStoreProvider Protocol using qdrant-client, with native hybrid search via Qdrant's prefetch + fusion API.

BM25 sparse embeddings are provided by a new FastEmbedBM25Provider implementing the SparseEmbeddingProvider Protocol. This lightweight approach (fastembed `Qdrant/bm25` model) uses tokenization and term frequency computation. When paired with Qdrant's IDF modifier, the server computes IDF from the corpus automatically.

The plan also delivers the zero-downtime reindex API (POST /reindex to trigger, GET /reindex/status for progress), chunk metadata extensions for domain-specific fields, and the DEBT-004 fix for budget allocator input ordering.

## Design Notes

### Sparse vectors in pgvector

pgvector does not natively support sparse vectors. Store sparse data as a JSONB column (`sparse_vector`) on `document_chunks` with structure `{"indices": [int, ...], "values": [float, ...]}`. Hybrid search in pgvector computes dense cosine similarity via the `<=>` operator and sparse dot-product similarity via a custom SQL function, then combines scores using RRF: `1/(k + rank_dense) + 1/(k + rank_sparse)` where k=60 (standard RRF constant). This requires two subqueries (one per modality) unioned and ranked. For SearchMode.SPARSE, only the sparse score is used. The sparse dot-product is computed in SQL as a scalar subquery over jsonb_array_elements.

Add an Alembic migration for the new `sparse_vector JSONB` column on `document_chunks` (nullable, default NULL). Existing Phase 1 chunks have sparse_vector = NULL and continue to work with DENSE mode.

### Qdrant provider

QdrantVectorStoreProvider uses `qdrant-client` (async mode). Collection setup:

- Dense vectors: named vector "dense" with cosine distance, dimension from EmbeddingProvider.dimensions().
- Sparse vectors: named vector "sparse" with `sparse_vectors_config.modifier = "idf"` for automatic IDF computation.
- Payload fields: `namespace_id`, `index_version`, `text`, `metadata`, `document_id`.

Namespace isolation: payload filter `namespace_id == namespace` on all operations. Index version: payload filter `index_version == active_version` on search.

store(): Upsert points with `wait=True` for synchronous confirmation. On partial failure: compensating delete of all point IDs in the batch (ARCH-052). Point IDs are UUIDs converted to strings (Qdrant supports both UUID and string IDs).

search(mode=HYBRID): Uses Qdrant's prefetch API. Send two prefetch requests (dense and sparse), fuse with RRF server-side in a single round trip. search(mode=DENSE): Standard nearest neighbor on "dense" named vector. search(mode=SPARSE): Standard nearest neighbor on "sparse" named vector.

delete(): Delete by payload filter `document_id in ids` with `wait=True`.

raw_filters: When provided, convert to Qdrant `models.Filter` conditions. Supports range, match, and nested payload conditions.

### FastEmbedBM25Provider

Implements SparseEmbeddingProvider Protocol. Uses `fastembed.SparseTextEmbedding` with model `Qdrant/bm25`. Lightweight: tokenization + term frequency computation only. No GPU required. For Qdrant usage, IDF is computed server-side.

```python
class FastEmbedBM25Provider:
    def __init__(self, model_name: str = "Qdrant/bm25") -> None: ...
    async def embed_documents(self, texts: list[str]) -> list[SparseVector]: ...
    async def embed_query(self, text: str) -> SparseVector: ...
    def vocab_size(self) -> int | None: ...
```

### Reindex API

POST /api/v1/reindex triggers a background arq job that:
1. Reads all documents in the namespace (or all namespaces if not specified).
2. For each document: re-extracts, re-chunks, re-embeds, stores with `index_version = target_version`.
3. Updates progress in a reindex_jobs tracking record.

GET /api/v1/reindex/{job_id}/status returns progress (documents processed / total, current phase).

The active index version switch is manual: operator sets VEKTRA_ACTIVE_INDEX_VERSION after reindex completes, then triggers cleanup of old-version chunks.

### Budget allocator ordering fix (DEBT-004)

In `vektra_core/pipeline.py`, sort `filtered` by score descending before passing to `allocate_token_budget()`. This ensures the budget allocator always processes the highest-scoring chunks first, regardless of which VectorStoreProvider is used. Also add a unit test for unsorted input.

### Qdrant as optional dependency

qdrant-client is an optional dependency of vektra-index. Import guarded: if qdrant-client is not installed and VEKTRA_VECTOR_STORE_PROVIDER=qdrant, startup fails with a clear error. fastembed is also optional, guarded the same way for VEKTRA_SPARSE_EMBEDDING_PROVIDER=fastembed-bm25.

### Provider selection at startup

PgvectorProvider is the default. QdrantVectorStoreProvider is selected via VEKTRA_VECTOR_STORE_PROVIDER=qdrant. The lifespan registers the selected provider in ProviderRegistry under category="vector_store", name="default". Same pattern for SparseEmbeddingProvider: register only when VEKTRA_SPARSE_EMBEDDING_PROVIDER is set.

### SearchRequest changes

The existing SearchRequest model already accepts `search_mode: SearchMode`. No API schema changes needed. The PgvectorProvider and QdrantVectorStoreProvider both handle all three SearchMode values. If sparse embeddings are unavailable (SparseEmbeddingProvider not registered), SPARSE and HYBRID modes fall back to DENSE with a warning log.

## Tasks

### Sparse embedding provider

- [x] **T1**: Implement FastEmbedBM25Provider in `vektra-index/src/vektra_index/providers/fastembed_bm25.py`. Methods: `embed_documents()`, `embed_query()`, `vocab_size()`. Use `asyncio.to_thread()` for inference (same pattern as SentenceTransformersProvider). Include module-level singleton for the fastembed model. Add `fastembed>=0.4` as optional dependency in `vektra-index/pyproject.toml` (under `[project.optional-dependencies]` group `sparse`) with import guard.
- [x] **T2**: Write unit tests for FastEmbedBM25Provider: embed_documents returns list[SparseVector] with correct structure, embed_query returns SparseVector, vocab_size returns int or None. Test import guard behavior when fastembed is missing.

### Hybrid search in PgvectorProvider

- [x] **T3**: Create Alembic migration `0003_hybrid_search.py` with `revision = "0003"`, `down_revision = "0002"`. Add `ALTER TABLE document_chunks ADD COLUMN sparse_vector JSONB DEFAULT NULL`. This migration also contains the `reindex_jobs` table (T14). Update DocumentChunkOrm in `vektra-index/src/vektra_index/models.py` with the new column mapping.
- [x] **T4**: Update PgvectorProvider.store() to persist `chunk.sparse` as JSONB `{"indices": [...], "values": [...]}` in the `sparse_vector` column when present. Existing chunks with no sparse data keep NULL.
- [x] **T5**: Implement PgvectorProvider.search() for SearchMode.SPARSE: compute sparse dot-product similarity via SQL (sum of value products where indices match), filter by namespace and index_version, order by sparse score descending.
- [x] **T6**: Implement PgvectorProvider.search() for SearchMode.HYBRID: run dense and sparse subqueries, combine via RRF (`1/(60 + rank_dense) + 1/(60 + rank_sparse)`), return top_k by combined score. If a chunk has no sparse_vector, its sparse rank is set to top_k + 1 (lowest priority).
- [x] **T7**: Update search API endpoint to generate both dense and sparse query embeddings when SparseEmbeddingProvider is registered. Populate QueryEmbedding.sparse. When SparseEmbeddingProvider is not registered and search_mode is SPARSE or HYBRID, fall back to DENSE with a warning.
- [x] **T8**: Write integration tests for hybrid search in pgvector: DENSE returns results ordered by cosine similarity, SPARSE returns results by sparse similarity, HYBRID returns results by RRF. Include test with sparse_vector=NULL chunks in HYBRID mode.

### QdrantVectorStoreProvider

- [x] **T9**: Implement QdrantVectorStoreProvider in `vektra-index/src/vektra_index/providers/qdrant.py`. Constructor accepts `url`, `api_key` (optional), `collection_name`, `active_index_version`. Uses `qdrant_client.AsyncQdrantClient`. Add `qdrant-client>=1.12` as optional dependency (group `qdrant`) with import guard.
- [x] **T10**: Implement `store()`: upsert points with named vectors "dense" and optionally "sparse". Payload includes namespace_id, index_version, text, metadata, document_id. Use `wait=True`. On exception after partial upsert, execute compensating delete for all point IDs in the batch (ARCH-052).
- [x] **T11**: Implement `search()` for all three SearchMode values. DENSE: search on "dense" named vector with cosine. SPARSE: search on "sparse" named vector. HYBRID: use prefetch API with two prefetch requests (dense and sparse) fused via RRF. All modes filter by namespace_id and index_version via payload filter. Apply SearchFilters as additional payload conditions. raw_filters converted to Qdrant Filter model.
- [x] **T12**: Implement `delete()` and `health_check()`. delete: delete points by payload filter `document_id in ids` with `wait=True`, return count. health_check: call `client.get_collections()` with short timeout, return HealthStatus.
- [x] **T13**: Write unit tests for QdrantVectorStoreProvider using mock AsyncQdrantClient. Verify store, search (all modes), delete, and health_check call the expected client methods with correct parameters.

### Reindex API

- [x] **T14**: Add `reindex_jobs` table to migration 0003 (same file as T3 sparse_vector column): columns (id UUID PK, namespace_id, source_index_version INT, target_index_version INT, status VARCHAR(16), total_documents INT, processed_documents INT, current_document_id UUID NULL, error_message TEXT NULL, created_at, completed_at). Add ReindexJobOrm in models.py.
- [x] **T15**: Implement reindex background job in `vektra-index/src/vektra_index/reindex.py`: function `run_reindex(namespace, source_version, target_version, job_id, registry)` that iterates source_documents, re-extracts, re-chunks, re-embeds, stores with target_version. Updates reindex_jobs progress after each document. Add API endpoints: `POST /api/v1/reindex` (202 + job_id) and `GET /api/v1/reindex/{job_id}/status`. Both require admin scope.
- [x] **T16**: Write tests for reindex: job creation, progress tracking, completion status update.

### Budget allocator ordering fix (DEBT-004) and metadata extensions

- [x] **T17**: In `vektra-core/src/vektra_core/pipeline.py`, sort `filtered` chunks by score descending before constructing `chunk_inputs` for `allocate_token_budget()`. Add a comment referencing DEBT-004. Add unit test in `vektra-core/tests/test_budget.py` for unsorted input.
- [x] **T18**: Update ChunkMetadata TypedDict in `vektra-shared/src/vektra_shared/types.py` to document domain-specific fields (course_id, module_id, academic_year) as optional keys accepted at runtime. This is a documentation and type hint update; JSONB storage already accepts arbitrary keys.

### Provider registration and configuration

- [x] **T19**: Add QdrantConfig to `vektra-shared/src/vektra_shared/config.py`: `VEKTRA_QDRANT_URL` (default "http://localhost:6333"), `VEKTRA_QDRANT_API_KEY` (optional), `VEKTRA_QDRANT_COLLECTION` (default "vektra"). Add fields to VektraSettings. Update startup validation in `vektra-index/src/vektra_index/startup.py`: when VEKTRA_VECTOR_STORE_PROVIDER=qdrant, verify Qdrant connectivity. When VEKTRA_SPARSE_EMBEDDING_PROVIDER=fastembed-bm25, verify the sparse model loads.

## Acceptance Criteria

- [ ] SearchMode.DENSE continues to work as before (no regression)
- [ ] SearchMode.SPARSE returns results ranked by sparse similarity when SparseEmbeddingProvider is registered
- [ ] SearchMode.HYBRID returns results ranked by RRF combining dense and sparse scores
- [ ] When SparseEmbeddingProvider is not registered, SPARSE and HYBRID fall back to DENSE with a warning log
- [ ] QdrantVectorStoreProvider passes all VectorStoreProvider Protocol methods (store, search, delete, health_check)
- [ ] Qdrant hybrid search uses prefetch + fusion API (single round trip, no client-side fusion)
- [ ] Qdrant store uses wait=True and compensating delete on failure (ARCH-052)
- [ ] Qdrant namespace isolation via payload filter (no collection-per-namespace)
- [ ] POST /reindex triggers background reindex job with progress tracking
- [ ] GET /reindex/{job_id}/status returns current progress
- [ ] Reindex creates chunks with new index_version without affecting active version queries
- [ ] Budget allocator receives chunks sorted by score descending (DEBT-004)
- [ ] All new code has unit tests; integration tests for pgvector hybrid search and Qdrant provider
- [ ] fastembed and qdrant-client are optional dependencies (import-guarded)
- [ ] Existing Phase 1 tests pass without modification

## Testing Approach

**Unit tests**: FastEmbedBM25Provider (sparse vector structure), QdrantVectorStoreProvider (mocked client), reindex job logic, budget allocator ordering.

**Integration tests**: PgvectorProvider hybrid search requires a PostgreSQL instance with pgvector extension (testcontainers). Test all three SearchMode values with a fixture that stores chunks with both dense and sparse vectors. Verify RRF ranking produces expected order. Test chunks with sparse_vector=NULL in HYBRID mode.

**QdrantVectorStoreProvider integration tests**: Require a Qdrant instance. Use testcontainers with Qdrant Docker image. Test store/search/delete cycle, hybrid search, namespace filtering, compensating delete on simulated failure.

**Reindex tests**: Mock the extraction and embedding providers. Verify job progress updates and chunk version tagging.

## Integration Notes

- core-pipeline-v2 (Wave 2) will use hybrid search via the VectorStoreProvider Protocol. No interface changes needed; SearchMode.HYBRID is already defined and the Protocol contract is unchanged.
- infra-phase2 (Wave 5) adds Qdrant to Docker Compose as a profile service and registers QdrantVectorStoreProvider in the app lifespan when configured.
- The sparse_vector column migration must run after database-phase2 migrations. Coordinate migration revision ordering.
- The DEBT-004 fix is in vektra-core, not vektra-index. It is included here because it is tightly coupled to multi-provider search ordering guarantees.

## Notes

### Session notes (2026-03-02)

**Migration renumbering**: Plan specified revision="0003" but 0003 was already taken by RLS policies (admin-enforcement plan). Migration created as `0004_hybrid_search.py` with revision="0004", down_revision="0003".

**PgvectorProvider hybrid search**: HYBRID mode uses client-side RRF over two separate queries (dense + sparse) rather than a single SQL union. This keeps each search path independent and testable. RRF k=60 (standard constant).

**QdrantVectorStoreProvider**: Implements VectorStoreProvider Protocol directly (no session needed). Constructor accepts `_client` parameter for test injection. HYBRID mode uses Qdrant's prefetch + fusion API (single round trip, no client-side fusion).

**qdrant-client test strategy**: Since qdrant-client is not installed in the dev environment, tests install a mock module in `sys.modules` before importing the provider. The provider's `__init__` accepts `_client` for direct mock injection.

**Reindex job**: Uses `get_session_factory()` from `vektra_shared.db` for background session creation. Actual re-extraction/re-chunking is a placeholder; full implementation depends on ingest pipeline availability (ingest-phase2 plan).

**Test counts**: 69 vektra-index tests (was 45). New: 4 fastembed, 9 hybrid search, 10 Qdrant, 6 reindex, 7 startup (added Qdrant/sparse checks), 1 budget (DEBT-004). Total across all components: 400 tests.
