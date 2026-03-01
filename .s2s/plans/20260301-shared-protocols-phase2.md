# Implementation Plan: Phase 2 Protocol additions and import boundaries

**ID**: 20260301-shared-protocols-phase2
**Status**: pending
**Branch**: N/A
**Created**: 2026-03-01T14:30:09Z
**Updated**: 2026-03-01T14:30:09Z

## Traceability

**Source**: shared-protocols-phase2
**Source Type**: architecture

## Provides / Requires

**Provides**:
- AdvancedQueryPipeline Protocol with rewrite, rerank, and hybrid search steps (consumers: core-pipeline-v2)
- WebhookEventEmitter implementation with HMAC-SHA256 signatures (consumers: ingest-phase2, core-pipeline-v2)
- DualStrategyChunking additions to ChunkingStrategy Protocol (consumers: ingest-phase2)
- Extended Namespace type with quota enforcement fields (consumers: admin-enforcement)
- import-linter contracts for vektra-analytics and vektra-learn (consumers: component-analytics, component-learn, infra-phase2)

**Requires**: none

## References

### Requirements
- REQ-050: Pluggable vector store backend with hybrid search @.s2s/requirements.md
- REQ-054: ChunkingStrategy Protocol with dual-strategy Phase 2 @.s2s/requirements.md
- REQ-061: EventEmitter interface with WebhookEventEmitter Phase 2 @.s2s/requirements.md

### Architecture
- ARCH-036: QueryPipeline Protocol (AdvancedQueryPipeline Phase 2) @.s2s/architecture.md
- ARCH-037: ChunkingStrategy Protocol (DualStrategyChunking Phase 2) @.s2s/architecture.md
- ARCH-038: EventEmitter interface (WebhookEventEmitter with HMAC-SHA256) @.s2s/architecture.md
- ARCH-047: Namespace as first-class entity (quota enforcement Phase 2) @.s2s/architecture.md
- ARCH-053: SparseEmbeddingProvider Protocol (already defined, no changes) @.s2s/architecture.md
- ARCH-061: Conversational query rewriting (AdvancedQueryPipeline step) @.s2s/architecture.md

### Decisions
- ADR-0005: Module boundary enforcement @.s2s/decisions/ADR-0005-module-boundary-enforcement.md
- ADR-0014: QueryPipeline abstraction @.s2s/decisions/ADR-0014-query-pipeline-abstraction.md
- ADR-0023: Conversational query rewriting @.s2s/decisions/ADR-0023-conversational-query-rewriting.md

### Dependencies
none

## Overview

This plan extends vektra_shared with the Protocol definitions and shared types that Phase 2 components depend on. The SparseEmbeddingProvider Protocol already exists from Phase 1 (ARCH-053, defined but not registered). The QueryPipeline Protocol also already exists. The work here adds a new AdvancedQueryPipeline Protocol for the Phase 2 pipeline variant, extends the EventEmitter implementation with WebhookEventEmitter, adds DualStrategyChunking-related types, extends the Namespace dataclass with quota enforcement helpers, and adds import-linter contracts for the two new components (vektra-analytics, vektra-learn).

All changes are backward-compatible. Existing Phase 1 implementations (SimpleQueryPipeline, FixedSizeChunking, NoOpEventEmitter, PassthroughSafeguard) continue to work unchanged. New implementations will be added in their respective Wave 1+ plans.

The AdvancedQueryPipeline Protocol shares the same execute/execute_stream signatures as QueryPipeline (it IS a QueryPipeline) but adds optional configuration hooks for rewriting, reranking, and classification steps. This is implemented as a concrete subclass pattern, not a separate Protocol, since the external contract (execute/execute_stream) is identical and callers select the implementation via VEKTRA_QUERY_PIPELINE config.

## Design Notes

**AdvancedQueryPipeline**: Not a new Protocol. The existing `QueryPipeline` Protocol (execute + execute_stream) is sufficient. The AdvancedQueryPipeline will be a concrete class implementing this Protocol with additional internal steps: classify -> rewrite -> retrieve -> rerank -> synthesize -> verify. No Protocol signature changes needed. However, new configuration types are needed:

```python
class RewriteConfig(BaseModel):
    enabled: bool = True                    # VEKTRA_QUERY_REWRITE_ENABLED
    model: str | None = None                # Override LLM model for rewrite step

class RerankConfig(BaseModel):
    enabled: bool = True                    # VEKTRA_RERANK_ENABLED
    provider: str = "flashrank"             # flashrank | cross-encoder | cohere
    model: str | None = None                # Provider-specific model name
    top_k: int = 5                          # Final top-k after reranking
```

**WebhookEventEmitter**: New class in `vektra_shared/events.py` alongside NoOpEventEmitter. Sends HTTP POST to configured webhook URLs with HMAC-SHA256 signature in `X-Vektra-Signature-256` header. Uses `httpx.AsyncClient` for non-blocking delivery. Fire-and-forget with structured logging on failure (no retries in Phase 2, retry queue deferred to Phase 3).

```python
class WebhookConfig(BaseModel):
    url: str                                # VEKTRA_WEBHOOK_URL
    secret: str                             # VEKTRA_WEBHOOK_SECRET (HMAC key)
    timeout_seconds: float = 5.0            # VEKTRA_WEBHOOK_TIMEOUT

class WebhookEventEmitter:
    def __init__(self, config: WebhookConfig) -> None: ...
    async def emit(self, event_type: str, payload: dict[str, Any]) -> None: ...
    def _sign(self, body: bytes) -> str: ...  # HMAC-SHA256 hex digest
```

**DualStrategyChunking**: The existing `ChunkingStrategy` Protocol (single method `chunk()`) is sufficient. The DualStrategyChunking implementation will live in vektra-ingest (plan: ingest-phase2). What this plan adds: a `ChunkingConfig` extension with dual-strategy fields.

```python
class ChunkingConfig(BaseSettings):
    strategy: str = "fixed"                 # VEKTRA_CHUNKING_STRATEGY: "fixed" | "dual"
    chunk_size: int = 1000                  # Existing
    chunk_overlap: int = 200                # Existing
    # Phase 2 dual-strategy fields:
    table_split: bool = False               # Never split tables
    parent_child_levels: int = 0            # 0 = disabled, 2 = Phase 2 default
```

**Extended Namespace**: Add `quota_bytes: int | None` field and a `check_quota()` helper method to the Namespace dataclass. The enforcement logic lives in vektra-admin (plan: admin-enforcement), but the type and validation belong in shared.

**import-linter**: Add two new contracts to `pyproject.toml` for vektra_analytics and vektra_learn, following the same pattern as existing contracts. Also add them to `root_packages`, `known-first-party`, and coverage source lists.

## Tasks

- [ ] Add `RewriteConfig` and `RerankConfig` Pydantic settings to `vektra-shared/src/vektra_shared/config.py` with env var aliases `VEKTRA_QUERY_REWRITE_ENABLED`, `VEKTRA_RERANK_ENABLED`, `VEKTRA_RERANK_PROVIDER`, `VEKTRA_RERANK_MODEL`, `VEKTRA_RERANK_TOP_K`. Extend `QueryPipelineConfig` with `rewrite: RewriteConfig` and `rerank: RerankConfig` nested fields.

- [ ] Add `WebhookConfig` Pydantic settings to `vektra-shared/src/vektra_shared/config.py` with env var aliases `VEKTRA_WEBHOOK_URL`, `VEKTRA_WEBHOOK_SECRET`, `VEKTRA_WEBHOOK_TIMEOUT`. All three fields optional (webhook disabled when URL is None).

- [ ] Extend `ChunkingConfig` in `vektra-shared/src/vektra_shared/config.py` with Phase 2 dual-strategy fields: `table_split: bool = False`, `parent_child_levels: int = 0`. Add validator: when `strategy == "dual"`, `parent_child_levels` must be >= 1.

- [ ] Add `quota_bytes: int | None = None` field to the `Namespace` dataclass in `vektra-shared/src/vektra_shared/types.py`. This field exists as nullable in the database (ARCH-040 forward-compatible) and will be enforced in admin-enforcement plan.

- [ ] Implement `WebhookEventEmitter` in `vektra-shared/src/vektra_shared/events.py`: class with `__init__(self, config: WebhookConfig)`, `async def emit(self, event_type: str, payload: dict[str, Any]) -> None`, and `def _sign(self, body: bytes) -> str`. Implementation: serialize payload to JSON with `event_type` and `timestamp` fields, compute HMAC-SHA256 with `config.secret` as key, POST to `config.url` with `Content-Type: application/json` and `X-Vektra-Signature-256: sha256={hex_digest}` header. Use `httpx.AsyncClient` with `config.timeout_seconds` timeout. Log errors via `structlog` (no exceptions raised to caller, fire-and-forget). Create client once in `__init__` (reuse across calls).

- [ ] Verify `WebhookEventEmitter` satisfies the `EventEmitter` Protocol via `isinstance()` check (runtime_checkable). Add `__all__` export in `events.py` for both `NoOpEventEmitter` and `WebhookEventEmitter`.

- [ ] Add `httpx` to `vektra-shared/pyproject.toml` dependencies (already a dev dependency, now needed at runtime for WebhookEventEmitter). Verify structlog is available (already transitive via FastAPI/uvicorn, but add explicit dependency if needed).

- [ ] Add import-linter contracts for the two new components in `pyproject.toml`. Add `"vektra_analytics"` and `"vektra_learn"` to the `root_packages` list. Add two new `[[tool.importlinter.contracts]]` entries following the existing pattern:
  - `vektra_analytics must not import from other vektra components except vektra_shared` (forbidden: vektra_core, vektra_ingest, vektra_index, vektra_admin, vektra_learn)
  - `vektra_learn must not import from other vektra components except vektra_shared` (forbidden: vektra_core, vektra_ingest, vektra_index, vektra_admin, vektra_analytics)
  - Update existing contract "No component shall import the app entrypoint" to include vektra_analytics, vektra_learn in source_modules.
  - Add `"vektra_analytics"` and `"vektra_learn"` to `[tool.ruff.lint.isort] known-first-party`.
  - Add `"vektra_analytics"` and `"vektra_learn"` to `[tool.coverage.run] source`.

- [ ] Write unit tests for `WebhookEventEmitter` in `vektra-shared/tests/test_events.py`:
  - Test `_sign()` produces correct HMAC-SHA256 hex digest for known input.
  - Test `emit()` sends POST with correct headers (`Content-Type`, `X-Vektra-Signature-256`), body contains `event_type` and `timestamp`.
  - Test `emit()` does not raise on HTTP error (fire-and-forget, logs warning).
  - Test `emit()` does not raise on connection timeout (logs warning).
  - Test `isinstance(WebhookEventEmitter(...), EventEmitter)` passes.
  - Use `httpx` mock or `respx` library for HTTP mocking.

- [ ] Write unit tests for new config types in `vektra-shared/tests/test_config.py`:
  - Test `RewriteConfig` defaults (enabled=True, model=None).
  - Test `RerankConfig` defaults and env var loading.
  - Test `WebhookConfig` with all fields set.
  - Test `ChunkingConfig` validator: strategy="dual" with parent_child_levels=0 raises ValidationError.
  - Test `ChunkingConfig` validator: strategy="fixed" with parent_child_levels=0 passes.

- [ ] Update `vektra-shared/tests/test_protocols.py` to verify the Protocol count is still 9 (no new Protocols added, AdvancedQueryPipeline is a concrete class not a Protocol). Add assertion that `QueryPipeline` Protocol signatures are unchanged.

## Acceptance Criteria

- [ ] `WebhookEventEmitter` implements `EventEmitter` Protocol (passes `isinstance` check at runtime)
- [ ] `WebhookEventEmitter.emit()` sends HMAC-SHA256 signed POST requests to configured URL
- [ ] `WebhookEventEmitter.emit()` is fire-and-forget: HTTP failures logged but never raised
- [ ] `RewriteConfig`, `RerankConfig`, `WebhookConfig` load from environment variables
- [ ] `ChunkingConfig` validates that `strategy="dual"` requires `parent_child_levels >= 1`
- [ ] `Namespace` dataclass has `quota_bytes` field (nullable)
- [ ] import-linter contracts exist for `vektra_analytics` and `vektra_learn`
- [ ] All 9 existing Protocol interfaces remain unchanged (backward compatible)
- [ ] All existing tests pass without modification
- [ ] New tests cover WebhookEventEmitter (signing, HTTP calls, error handling) and config types

## Testing Approach

Unit tests only. WebhookEventEmitter tested with `respx` or manual `httpx` transport mock to intercept outgoing HTTP calls and verify headers, body, and signature. Config types tested via Pydantic model instantiation with env var overrides (monkeypatch). No integration tests needed for this plan since there are no database or external service dependencies.

## Integration Notes

The `WebhookEventEmitter` will be registered in `ProviderRegistry` under category `"event_emitter"`, name `"webhook"` during app lifespan (plan: infra-phase2). Selection controlled by `VEKTRA_EVENT_EMITTER` env var (default: `"noop"` for backward compatibility).

The `AdvancedQueryPipeline` concrete class will be implemented in core-pipeline-v2 (Wave 2). It will be registered under category `"query_pipeline"`, name `"advanced"`. The config types defined here (`RewriteConfig`, `RerankConfig`) will be consumed by that implementation.

The import-linter contracts for vektra-analytics and vektra-learn will initially fail lint-contracts if those packages don't exist yet. The plan for component-analytics and component-learn (Waves 3-4) must create the package stubs before import-linter CI runs against them. Until then, the contracts are defined but the packages are absent (import-linter skips missing root packages gracefully).

## Notes

<!-- Progress notes during implementation -->
