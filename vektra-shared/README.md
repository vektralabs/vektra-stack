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
- **Namespace config resolvers** (`namespace.py`) — async helpers that read `namespaces.config` JSONB once per request and fall back to a caller-supplied default. Both use raw SQL (ADR-0005, no ORM imports across packages) and return the default on any error (missing namespace, DB error, invalid value):
  - `resolve_grounding_mode(namespace: str, session_factory: Any, default_mode: str = "strict") -> str` — returns `"strict"` or `"hybrid"`. Called by `vektra-core` and `vektra-learn` per query. The caller pre-resolves the env-var fallback (`VEKTRA_PROMPT_GROUNDING_MODE`) and passes it as `default_mode`.
  - `resolve_show_sources(namespace: str, session_factory: Any, default_value: bool = True) -> bool` — FEAT-014. Called by `vektra-learn` per query; the caller pre-resolves the env-var fallback (`VEKTRA_LEARN_SHOW_SOURCES`) into `default_value`. `vektra-admin` does not use this helper directly: it computes the equivalent `resolved` view inline in `GET /api/v1/admin/namespaces/{id}/config` so the response can carry both stored and resolved values.
- **`ProviderRegistry`** (`registry.py`): the runtime registry where `vektra-app` registers Protocol implementations and other components resolve them by name.
- **Startup validation** (`startup.py`): two helpers consumed by `vektra-app`'s lifespan (steps 2 and 3 of the 11-step ARCH-057 sequence): `check_database_connectivity` and `check_database_schema`, plus the `StartupValidationError` exception. The remaining nine steps live inline in `vektra-app/main.py:lifespan` (config validation, pgvector extension, provider registration, embedding warmup, LLM connectivity, prompt template loading, analytics check, learn check, qdrant check); see `docs/architecture/index.md > Startup validation` for the full list.

## Why it's a separate package

ADR-0005 (module boundary enforcement). Every other component imports `vektra_shared` for types, config, and protocols, but **never** imports another component. This keeps the modular monolith honest and gives us a clean Phase 3 extraction path: components can move to separate repos individually because they share no code outside `vektra-shared`.

See [.s2s/architecture.md](../.s2s/architecture.md) for the full protocol specifications.
