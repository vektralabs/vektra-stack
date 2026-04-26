# vektra-shared

Shared protocol interfaces, configuration, and helpers used by all Vektra components.

This package is the platform contract: every other component depends on it, and no component depends on another (enforced by `import-linter`).

## What lives here

- **9 Protocol interfaces**: `LLMProvider`, `EmbeddingProvider`, `SparseEmbeddingProvider`, `VectorStoreProvider`, `DocumentExtractor`, `ChunkingStrategy`, `QueryPipeline`, `SafeguardHook`, `EventEmitter`. Each is a `typing.Protocol` so any conforming class is a valid implementation, no inheritance required.
- **Shared data models** (`types.py`): `QueryRequest`, `QueryResponse`, `SourceRef`, `QueryTrace`, `SearchResult`, `Chunk`, `Conversation`, etc.
- **Configuration** (`config.py`): `VektraSettings` (the single Pydantic source of truth, ARCH-060) plus sub-configs (`RewriteConfig`, `RerankConfig`, `WebhookConfig`).
- **Auth primitives** (`auth.py`): `require_scope` FastAPI dependency, `ApiKeyInfo` model, scope-checking logic. `admin` is a project-wide superscope: a key with `admin` satisfies any `require_scope("X")` check.
- **Errors** (`errors.py`): the REQ-010 error envelope, error code constants (`ERR_AUTH_*`, `ERR_INGEST_*`, `ERR_QUERY_*`, `ERR_ADMIN_*`, `ERR_LEARN_*`, ...), category-to-HTTP mapping, and `http_status_for()`.
- **Audit** (`audit.py`): `log_event()` helper that writes a row into the audit log; called from every component on sensitive content access (NFR-007).
- **Namespace config resolvers** (`namespace.py`):
  - `resolve_grounding_mode(namespace_id, *, defaults)` — reads `namespaces.config.grounding_mode`, falls back to `VEKTRA_PROMPT_GROUNDING_MODE`.
  - `resolve_show_sources(namespace_id, *, defaults)` — reads `namespaces.config.show_sources`, falls back to `VEKTRA_LEARN_SHOW_SOURCES`. Used by both `vektra-learn` (per-request hint to the widget) and `vektra-admin` (PATCH/GET symmetry on `/admin/namespaces/{id}/config`).
- **`ProviderRegistry`** (`registry.py`): the runtime registry where `vektra-app` registers Protocol implementations and other components resolve them by name.
- **Startup validation** (`startup.py`): the helpers used by `vektra-app` to run the 11-step ARCH-057 sequence (config validation, DB connectivity, schema check, pgvector extension, provider registration, embedding model warmup, LLM connectivity, prompt template verification, conversation store registration, qdrant collection check, audit table presence).

## Why it's a separate package

ADR-0005 (module boundary enforcement). Every other component imports `vektra_shared` for types, config, and protocols, but **never** imports another component. This keeps the modular monolith honest and gives us a clean Phase 3 extraction path: components can move to separate repos individually because they share no code outside `vektra-shared`.

See [.s2s/architecture.md](../.s2s/architecture.md) for the full protocol specifications.
