# vektra-index

Vector store abstraction, embedding generation, and semantic search for the Vektra platform.

## Protocol implementations

- **`VectorStoreProvider`** with two backends:
  - **pgvector** (default, Phase 1): PostgreSQL extension. Single-deployable, no extra service.
  - **Qdrant** (Phase 2, opt-in via `VEKTRA_VECTOR_STORE_PROVIDER=qdrant`): adds native dense + sparse + hybrid search modes via Qdrant's REST API. Includes the `qdrant` Docker Compose profile and the 11th startup validation step (collection check).
- **`EmbeddingProvider`** with `sentence-transformers` (default model `paraphrase-multilingual-MiniLM-L12-v2`).
- **`SparseEmbeddingProvider`** (Phase 2): `FastEmbedBM25Provider` via `fastembed` for BM25 sparse vectors. Activated via `VEKTRA_SPARSE_EMBEDDING_PROVIDER=fastembed-bm25`.

## Search modes

`SearchMode` enum (Phase 2):

- `DENSE` — vector similarity only (Phase 1 behavior).
- `SPARSE` — BM25-only (lexical match).
- `HYBRID` — fusion of dense and sparse, native to Qdrant.

The provider also exposes a `raw_filters` escape hatch for backend-specific metadata filtering, namespace isolation enforcement, and atomic index version switching for zero-downtime reindex.

## Reindex

Operator script `scripts/reindex.sh` triggers a full re-embedding into a new index version while serving from the active one. After completion, `VEKTRA_ACTIVE_INDEX_VERSION` is bumped atomically (provider-specific transaction).

See [architecture.md](../.s2s/architecture.md) for the component specification.
