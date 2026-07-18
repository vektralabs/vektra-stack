# ADR-0026: document_chunks is a pgvector implementation detail

**Status**: accepted
**Date**: 2026-07-13
**Context**: BUG-023 investigation 2026-07-13

## Context

`VectorStoreProvider` (ARCH-051) exists so the vector store is pluggable: pgvector is the
default, Qdrant is an alternative, and they are peers. Neither is meant to be mandatory
when the other is active.

The `document_chunks` table breaks that contract in practice. It is written **only** by
`PgvectorProvider.store()`. `QdrantVectorStoreProvider` keeps chunk text and metadata in
the Qdrant payload. But several modules bypass the Protocol and read `document_chunks`
directly with SQL, which silently assumes pgvector is present even when Qdrant is the
active provider.

With `VEKTRA_VECTOR_STORE_PROVIDER=qdrant` (the configuration every real deployment runs)
the table is empty. Every one of those paths therefore operates on an empty table and
**reports success**. They do not fail, they lie.

### Measured on the development stack (2026-07-13)

Qdrant collection `vektra` held 1323 points; `document_chunks` held **0 rows**.

| Path | Reported | Reality |
|------|----------|---------|
| `run_reindex` (namespace `default`, v1 -> v2) | `completed`, 3/3 documents | 0 points written to v2 |
| `GET /api/v1/stats` | `chunk_count: 0` | 12 / 105 / 562 points in the three namespaces |
| `DELETE /api/v1/documents/{id}` | `200 {"chunks_removed": 0}` | Qdrant points untouched; the deleted document still answered queries (score 0.559) |
| `POST /api/v1/documents/{id}/chunks` | `200`, chunks stored | written to pgvector, i.e. the inactive store |
| `cleanup_soft_deleted_task` (REQ-057 retention) | purge succeeded | Postgres row hard-deleted, Qdrant points orphaned and no longer traceable to any document |

The RAG pipeline, `/api/v1/search` and the ingest pipeline are unaffected: they resolve the
provider from the `ProviderRegistry`.

Two facts shaped the decision:

1. `source_documents.chunk_count` is already correct and provider-neutral (12/105/562 match
   the Qdrant counts exactly). Document-level bookkeeping in Postgres is not the problem.
2. `PgvectorProvider.store()` honours caller-supplied deterministic chunk ids
   (`uuid5(document_id, position)`), and FEAT-017 parent/child linkage depends on them.

## Decision

**`document_chunks` is an implementation detail private to `PgvectorProvider`. No other
module may read or write it.** Every path that touches chunk text, chunk counts or chunk
deletion goes through the `VectorStoreProvider` Protocol.

The boundary, stated explicitly:

- **Chunk text, embeddings and chunk-level metadata** belong to the active vector store
  provider. It is the single source of truth for them.
- **Document-level records** (`source_documents`, ingest and reindex jobs, conversations,
  API keys) stay in Postgres. They are not vector-store business and are unaffected.

To serve the paths that previously used SQL, the Protocol grows three things, each with a
real caller and no speculative surface:

| Addition | Caller | Why the existing surface is insufficient |
|----------|--------|------------------------------------------|
| `list_chunks(namespace, document_id)` | `run_reindex`, `GET /documents/{id}/chunks` | `retrieve()` takes known chunk ids; here the ids are not known |
| `count_chunks(namespace)` | `GET /stats` | no method counts |
| `index_version` kwarg on `store()` | `run_reindex` | `store()` writes only to the active index version |

`store()` keeps its signature for every existing caller: the kwarg is optional and defaults
to the active version, so the ingest path is untouched.

### Rejected: document_chunks as a provider-neutral store

The alternative was to make `document_chunks` a neutral table that **both** providers write,
keeping SQL as the read path for chunk text.

Rejected because it inverts the intent of ARCH-051. It would make Postgres a **mandatory
co-store under every provider**, which is exactly the redundancy the pluggable-store design
exists to avoid. It also introduces a dual write to two systems with no transaction spanning
them (Postgres commits, Qdrant fails, or the reverse), duplicates every chunk's text, and
leaves Qdrant only nominally the source of truth while a second copy silently drifts.

This ADR does not change the standing of the two providers: pgvector and Qdrant remain
interchangeable peers. It restores that property rather than altering it.

## Consequences

### Positive

- Swapping the provider now swaps the behaviour of **every** chunk path, not just search.
- The lying paths become correct in Qdrant mode: reindex re-embeds and writes, `stats`
  counts, `DELETE` removes the points, retention actually purges.
- A reindex that stores nothing fails loudly instead of reporting `completed`, and
  `reindex_jobs.chunks_reindexed` (migration 0007) makes the work it did observable.
- The rule is mechanically checkable: any SQL against `document_chunks` outside
  `providers/pgvector.py` is a violation.

### Negative

- The Protocol is wider. Every future provider must implement `list_chunks` and
  `count_chunks`, not only search. This is the price of the paths being honest, and both
  operations are cheap to implement on any vector store (Qdrant: `scroll` and `count`).
- `count_chunks` on a large collection costs an exact count. It is called by `/stats` only.

### Reindex point ids under Qdrant

Reindex writes the target index version alongside the source (zero-downtime, ARCH-045). In
Qdrant both versions share one collection, distinguished by the `index_version` payload
field. Re-storing under the ingest id seed `uuid5(document_id, position)` would reuse the
same point ids and **overwrite** the source version instead of adding to it.

Reindex therefore derives target ids as `uuid5(document_id, "{position}:v{target_version}")`,
which keeps the two versions disjoint and keeps reindex idempotent. Parent/child links
(FEAT-017) are remapped through the same map, so parent expansion works in the reindexed
version. The ingest seed is deliberately left unchanged: changing it would orphan the ids of
already-stored data.

### Violations remaining after this ADR

`vektra-admin/quotas.py::check_namespace_quota` counts `document_chunks` with raw SQL and
would never enforce the chunk quota in Qdrant mode. It is currently **dead code** (no
callers), so the defect is latent, not live. Tracked as DEBT-028; it must be routed through
`count_chunks()` before the quota is wired up.

## Enforcement

The gap that let this survive for months was the test layer, not the code: the only Qdrant
test mocked the entire `qdrant_client` module, and the integration suite ran against
pgvector only. A mocked client cannot notice that a table is empty or that a reindex wrote
nothing.

The integration suite therefore runs as a **CI matrix over both providers**
(`pgvector` and `qdrant`), exercising the same assertions against each. `tests/integration/
test_chunk_lifecycle.py` covers the paths this ADR fixes, including the one that matters
most: **a deleted document must not be retrievable.**

## Traceability

- Fixes: BUG-023, and the reindex no-op previously misattributed to BUG-021
- Related: ARCH-039 (ProviderRegistry), ARCH-051 (full-store contract), ARCH-045 (index
  versioning), FEAT-017 (`retrieve()` and parent expansion), REQ-057 (retention)
- Spawns: DEBT-028 (quota chunk count)
