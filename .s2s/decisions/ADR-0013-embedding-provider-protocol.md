# ADR-0013: EmbeddingProvider as dedicated Protocol

**Status**: accepted
**Date**: 2026-02-06
**Context**: Architectural review 2026-02-06

## Context

Embedding generation is used in two separate components: vektra-ingest (to embed document chunks during indexing) and vektra-core (to embed user queries during search). In the original architecture, both components call sentence-transformers directly without a shared abstraction.

This creates three concrete problems:

1. **Double memory loading**: if ingest and core instantiate the model independently, the ~90MB model (MiniLM) or ~1.2GB model (e5-large in Phase 2) is loaded twice
2. **Asymmetric embedding logic duplicated**: models like e5-large require different prefixes for documents (`"passage: ..."`) vs queries (`"query: ..."`). Without an abstraction, this logic is duplicated in both components
3. **Provider swap impossible without refactoring**: switching from local sentence-transformers to an external API (OpenAI embeddings, TEI server) in Phase 2 would require changes in both ingest and core

## Decision

Introduce `EmbeddingProvider` as a dedicated Protocol in vektra_shared, with a single shared instance used by both vektra-ingest and vektra-core.

```python
class EmbeddingProvider(Protocol):
    async def embed_documents(texts: list[str]) -> list[list[float]]
    async def embed_query(query: str) -> list[float]
    def dimensions() -> int
    async def health_check() -> HealthStatus
```

**Phase 1 implementation**: `SentenceTransformersProvider` using all-MiniLM-L6-v2 (384 dimensions). Single instance created at startup, injected into both ingest and core components. With MiniLM, `embed_documents()` and `embed_query()` produce identical results, but the contract is correct for asymmetric models from day one.

**Configuration**:
- `VEKTRA_EMBEDDING_PROVIDER=sentence-transformers` (default)
- `VEKTRA_EMBEDDING_MODEL=all-MiniLM-L6-v2` (default)

**Phase 2 options** (no contract change):
- `SentenceTransformersProvider` with e5-large (1024 dimensions, asymmetric)
- `ExternalEmbeddingProvider` calling OpenAI/TEI API (frees ~1.2GB RAM)
- `CachedEmbeddingProvider` decorator (LRU in-memory or Redis-backed)

**Multilingual note** (added 2026-02-26): Phase 1 testing with Italian documents showed that all-MiniLM-L6-v2 produces low-discrimination cosine similarity scores (0.50-0.58 range) for queries that should have high affinity. For non-English deployments, the Phase 2 model should be multilingual. Candidates:

| Model | Dimensions | Size | Notes |
|-------|-----------|------|-------|
| multilingual-e5-large | 1024 | ~1.2GB | Best quality for multilingual, asymmetric |
| multilingual-e5-base | 768 | ~450MB | Compromise size/quality |
| bge-m3 | 1024 | ~2.2GB | Produces both dense and sparse embeddings from a single model, could simplify SparseEmbeddingProvider (ARCH-053) |

bge-m3 is particularly interesting: it generates dense and sparse vectors in one pass, which would allow a single model to serve both EmbeddingProvider and SparseEmbeddingProvider roles for hybrid search, reducing memory and operational complexity.

**Critical note**: changing embedding model changes vector dimensions. Existing chunks (384-dim) are incompatible with new queries (1024-dim). Model change requires full reindex via the `index_version` pattern (ARCH-045, REQ-064).

## Options Considered

### No abstraction (status quo)

**Pros**:
- Fewer files, simpler Phase 1

**Cons**:
- Double model loading in RAM
- Asymmetric prefix logic duplicated
- Phase 2 provider swap touches both ingest and core

### EmbeddingProvider Protocol (chosen)

**Pros**:
- Single model instance, no RAM waste
- Asymmetric model support built into contract
- Phase 2 swap is a config change, not a refactor
- `dimensions()` enables schema validation at startup

**Cons**:
- One additional Protocol and implementation to maintain

### Embedding as a separate service

**Pros**:
- Clean separation, independent scaling

**Cons**:
- Violates ADR-0004 three-service constraint
- Network latency for every embedding call
- Overkill for Phase 1

## Consequences

### Positive

- Single model instance shared between ingest and core: no RAM waste
- Asymmetric embedding supported from day one (e5-large ready)
- Phase 2 provider swap (local to API, model change) is a config change
- `dimensions()` validates vector compatibility at startup before ingestion
- Embedding cache (Phase 2) becomes a transparent decorator

### Negative

- One additional Protocol and factory to maintain
- Shared instance requires lifecycle management (startup/shutdown)
