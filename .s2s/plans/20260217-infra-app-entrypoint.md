---
provides_requires:
  provides:
    - "VectorStoreServiceAdapter:class"
    - "ProviderRegistry[LLMProvider]:registered"
    - "ProviderRegistry[EmbeddingProvider]:registered"
    - "ProviderRegistry[VectorStoreProvider]:registered"
    - "AuditMiddleware:registered"
    - "FastAPI.app:factory"
  requires:
    - "AuditMiddleware:class"
    - "vektra_admin.audit.log_event:callable"
    - "LitellmProvider:class"
---
# Implementation Plan: FastAPI application assembly and startup validation

**ID**: 20260217-infra-app-entrypoint
**Status**: active
**Branch**: N/A
**Created**: 2026-02-17T22:42:39Z
**Updated**: 2026-02-17T22:42:39Z

## Traceability

**Source**: architecture
**Source Type**: architecture

## References

### Requirements
- REQ-019: API key authentication for all components @.s2s/requirements.md
- REQ-024: Phase 1 scope enforcement @.s2s/requirements.md
- REQ-025: Health endpoint authentication: two-tier model @.s2s/requirements.md
- NFR-004: Container startup time (<60s) @.s2s/requirements.md
- NFR-009: Error actionability (100% of errors have remediation) @.s2s/requirements.md

### Architecture
- ARCH-001: Single container deployment @.s2s/architecture.md
- ARCH-003: Module boundary enforcement @.s2s/architecture.md
- ARCH-008: Correlation ID propagation @.s2s/architecture.md
- ARCH-013: Structlog JSON setup @.s2s/architecture.md
- ARCH-015: FastAPI app assembly @.s2s/architecture.md
- ARCH-020: Single trust boundary (auth middleware) @.s2s/architecture.md
- ARCH-039: ProviderRegistry startup registration @.s2s/architecture.md
- ARCH-057: 8-step startup validation sequence @.s2s/architecture.md

### Decisions
- ADR-0003: Modular monolith for Phase 1 @.s2s/decisions/ADR-0003-modular-monolith-phase1.md
- ADR-0005: Module boundary enforcement @.s2s/decisions/ADR-0005-module-boundary-enforcement.md

### Dependencies
- 20260217-component-shared
- 20260217-component-core
- 20260217-component-ingest
- 20260217-component-index
- 20260217-component-admin
- 20260217-infra-database

## Overview

Wires all component modules into a single FastAPI application. Implements the 8-step startup validation sequence that ensures clear error messages on any misconfiguration. Configures all cross-cutting middleware (auth, correlation ID, logging, metrics). This is the integration layer - it does not contain business logic, only assembly and validation.

## Design Notes

- The startup validation sequence (ARCH-057) runs at application lifespan start (`@asynccontextmanager lifespan`). Any step failure raises `StartupValidationError` with a human-readable remediation message, which is caught by the lifespan handler and logged before raising to abort startup.
- Steps: (1) Pydantic config validation - all required env vars present, (2) DB connectivity, (3) Alembic migration check, (4) pgvector extension check, (5) provider registration (LLM, embedding, vector store, safeguard, event emitter), (6) embedding model warm-up, (7) LLM connectivity (warning-only, not fatal), (8) Jinja2 template loading.
- The auth middleware from vektra_shared is registered globally. Excluded paths: /health, /metrics only. /admin is protected at the handler level by vektra_admin.
- AuditMiddleware from vektra_admin is registered after auth middleware so request state already contains the resolved key_id.
- import-linter contract prevents circular imports between components at CI time.
- arq background worker is NOT started by this plan. The arq worker is a separate process/service defined in infra-docker (vektra-worker service). This plan starts only the FastAPI HTTP server.

## Tasks

- [ ] Create `main.py` (or `vektra/app.py`): instantiate FastAPI app with lifespan context manager
- [ ] Implement lifespan startup: run all 8 ARCH-057 validation steps in sequence; log each step as structlog INFO with step name and duration; on failure raise with remediation text; on success log "Vektra startup complete in {duration}ms". Step 5 (provider registration) code lives here — this is the only plan that creates and registers provider instances:
  ```python
  # Step 5: provider registration (all in infra-app-entrypoint lifespan, NOT in component plans)
  registry.register(LLMProvider, LitellmProvider(config.llm))
  registry.register(EmbeddingProvider, SentenceTransformersProvider(config.embedding_model))
  registry.register(VectorStoreProvider, VectorStoreServiceAdapter(PgvectorProvider()))
  registry.register(SafeguardHook, PassthroughSafeguard())
  registry.register(EventEmitter, NoOpEventEmitter())
  # Health checks registered under "health" category for vektra_admin aggregator:
  registry.register_health("health/llm", litellm_provider.health_check)
  registry.register_health("health/embedding", embedding_provider.health_check)
  registry.register_health("health/vector_store", vector_store_adapter.health_check)
  ```
  Note: VectorStoreServiceAdapter is imported from vektra_index.adapters. SentenceTransformersProvider is imported from vektra_index.providers. This is the ONLY module that imports from all components.
- [ ] Register all component FastAPI routers: mount vektra_admin routes (/ prefix for health/admin/metrics), vektra_core routes (/api/v1), vektra_ingest routes (/api/v1), vektra_index routes (/api/v1)
- [ ] Register auth middleware from vektra_shared: exclude paths = ["/health", "/metrics"] (truly unauthenticated); all other paths require Bearer token. Note: /admin is NOT excluded here — vektra_admin's GET /admin handler enforces Bearer token at the handler level. This dual-layer is intentional: middleware enforces auth globally, handler adds a secondary check for /admin specifically. Do not add /admin to the exclusion list.
- [ ] Register vektra_admin.middleware.AuditMiddleware on the FastAPI app via `app.add_middleware(AuditMiddleware)`. This middleware writes audit log entries for every authenticated request. Register it AFTER auth middleware so key_id is already resolved in request state.
- [ ] Implement correlation ID middleware: generate UUID per request if X-Request-ID header absent; propagate to structlog context; include in all structured log events and error responses
- [ ] Configure structlog: JSON output with PII redaction processors (strip query text, response text from log events at WARNING and above), timestamp, level, correlation_id, module fields
- [ ] Register starlette-prometheus middleware for GET /metrics endpoint
- [ ] Register global exception handler: catches unhandled exceptions, returns REQ-010 error envelope with ERR-CONFIG-001 and remediation hint, logs traceback at ERROR level without exposing it in the response
- [ ] Configure CORS (FastAPI CORSMiddleware): configurable via VEKTRA_CORS_ORIGINS env var; default to localhost for development
- [ ] Configure import-linter in `.importlinter`: contracts ensuring no cross-component imports outside vektra_shared; run as part of CI linting step
- [ ] Create `vektra_index/adapters.py`: implement `VectorStoreServiceAdapter` wrapping `PgvectorProvider` with an internally-managed async session (opens session from `get_session()`, calls `PgvectorProvider` methods, commits/closes). This adapter implements the session-free `VectorStoreProvider` Protocol and is the only VectorStoreProvider registered in ProviderRegistry. Both vektra-ingest and vektra-core use it via the registry without knowing about sessions or `PgvectorProvider` directly.
- [ ] Write smoke test: start application with test config (SQLite not available; requires PostgreSQL testcontainer), verify GET /health returns 200 within 60 seconds of startup, verify GET /health?detail=full with valid token returns component array
- [ ] Verify startup time: measure time from process start to /health returning 200; assert < 60s (NFR-004)

## Acceptance Criteria

- [ ] Application starts and passes /health within 60 seconds on fresh PostgreSQL (NFR-004)
- [ ] Each of the 8 startup steps logs its name and duration at INFO level
- [ ] Failed startup step produces human-readable error with remediation hint (not a Python traceback)
- [ ] Correlation ID present in all structured log events and in error response bodies
- [ ] Unauthenticated access to any path except /health and /metrics returns 401 with REQ-010 envelope
- [ ] import-linter passes: no cross-component imports outside vektra_shared
- [ ] Global exception handler prevents stack traces from appearing in API responses

## Testing Approach

Smoke test using testcontainers (PostgreSQL+pgvector) and the full FastAPI test client. Startup validation tested by: missing env var (step 1 failure), wrong database URL (step 2 failure), verify each produces correct remediation message. Import boundary tested by running import-linter in CI (not a Python test, a linting step).

## Integration Notes

This is the only module that imports from all component modules (vektra_admin, vektra_core, vektra_ingest, vektra_index). All other cross-component communication goes through ProviderRegistry and Protocol interfaces. The assembly here is the only valid place for cross-component wiring.

Ownership of shared responsibilities resolved here:
- Provider instantiation and registry registration: lifespan step 5 (in this plan)
- VectorStoreServiceAdapter: implemented in vektra_index/adapters.py, instantiated and registered here
- Health check registration under "health/*" keys: done here after providers are created
- AuditMiddleware registration: done here via app.add_middleware()
- Auth middleware exclusions: /health and /metrics only (/admin is handler-level auth)
- arq worker entrypoint: NOT here — see infra-docker (vektra-worker service)
