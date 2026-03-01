# Implementation Plan: infra-phase2 - App entrypoint, Docker, CI updates

**ID**: 20260301-infra-phase2
**Status**: pending
**Branch**: N/A
**Created**: 2026-03-01T14:30:09Z
**Updated**: 2026-03-01T14:30:09Z

## Traceability

**Source**: infra-phase2
**Source Type**: architecture

## Provides / Requires

**Provides**:
- Complete Phase 2 deployment stack (consumers: none, final integration plan)
- Updated app lifespan with all Phase 2 providers registered
- Docker Compose profiles for Qdrant and TEI
- CI pipelines for new components

**Requires**:
- 20260301-shared-protocols-phase2.md: new Protocol interfaces (SparseEmbeddingProvider, AdvancedQueryPipeline)
- 20260301-database-phase2.md: Phase 2 database tables and migrations
- 20260301-core-conversations.md: ConversationStore with persistence
- 20260301-admin-enforcement.md: RLS activation, rate limiting middleware
- 20260301-index-hybrid.md: SparseEmbeddingProvider, QdrantVectorStoreProvider
- 20260301-ingest-phase2.md: Unstructured extractor, DualStrategyChunking
- 20260301-core-pipeline-v2.md: AdvancedQueryPipeline, safeguard implementations
- 20260301-admin-ui.md: HTMX admin pages, static assets
- 20260301-component-analytics.md: AnalyticsService, analytics router
- 20260301-component-learn.md: LearnService, learn router, chatbot widget

## References

### Requirements
- REQ-005: 30-minute onboarding (startup must remain within time budget) @.s2s/requirements.md
- REQ-006: Admin capabilities (register new admin pages) @.s2s/requirements.md

### Architecture
- ARCH-057: Startup validation sequence (extend for new providers) @.s2s/architecture.md
- ARCH-051: VectorStoreProvider full-store contract (Qdrant profile) @.s2s/architecture.md
- ARCH-035: EmbeddingProvider Protocol (TEI profile) @.s2s/architecture.md
- ARCH-064: Phase 2 hardware target (8GB RAM / 4 CPU) @.s2s/architecture.md
- ARCH-033: Docker Compose specification @.s2s/architecture.md

### Decisions
- ADR-0001: Hybrid monorepo strategy @.s2s/decisions/ADR-0001-hybrid-monorepo-strategy.md
- ADR-0004: Minimal Docker Compose stack @.s2s/decisions/ADR-0004-minimal-docker-compose-stack.md
- ADR-0012: Docker Compose spec @.s2s/decisions/ADR-0012-docker-compose-spec.md

### Dependencies
- 20260301-shared-protocols-phase2.md
- 20260301-database-phase2.md
- 20260301-core-conversations.md
- 20260301-admin-enforcement.md
- 20260301-index-hybrid.md
- 20260301-ingest-phase2.md
- 20260301-core-pipeline-v2.md
- 20260301-admin-ui.md
- 20260301-component-analytics.md
- 20260301-component-learn.md

## Overview

This is the final integration plan for Phase 2. It wires all new components and providers into the application entrypoint, updates the Docker Compose stack with new service profiles, extends the Dockerfile for new dependencies, adds CI pipelines for the two new components, and updates the Makefile with Phase 2 workflow targets.

The plan touches the highest-risk integration point in the system: the app lifespan in `vektra_app/main.py`. Every new provider, service, and router must be registered here in the correct order. The startup validation sequence (ARCH-057) must be extended to check new providers without exceeding the 60-second startup time constraint (NFR-004).

The Docker Compose stack gains two optional service profiles: `qdrant` (already defined as a placeholder in Phase 1, now fully configured) and `tei` (Hugging Face Text Embeddings Inference as an alternative to local sentence-transformers). The Dockerfile gains a multi-stage enhancement: an optional Node.js builder stage for the chatbot widget, and optional Unstructured system dependencies for OCR.

## Design Notes

### Lifespan updates (main.py)

Extend `_step_5_register_providers()` with:

```python
# --- Sparse embedding (Phase 2, conditional) ---
if settings.sparse_embedding_provider:
    from vektra_index.providers.fastembed_bm25 import FastEmbedBM25Provider
    sparse_provider = FastEmbedBM25Provider()
    registry.register("sparse_embedding", "default", sparse_provider)

# --- Qdrant vector store (Phase 2, conditional) ---
if settings.vector_store_provider == "qdrant":
    from vektra_index.providers.qdrant import QdrantVectorStoreProvider
    qdrant_provider = QdrantVectorStoreProvider(url=settings.qdrant_url)
    registry.register("vector_store", "default", qdrant_provider)
    registry.register("vector_store", "qdrant", qdrant_provider)

# --- Analytics service ---
from vektra_analytics.service import AnalyticsService
analytics_service = AnalyticsService()
registry.register("analytics", "default", analytics_service)

# --- Learn service ---
if settings.learn_enabled:
    from vektra_learn.service import LearnService
    learn_service = LearnService(jwt_secret=settings.learn_jwt_secret)
    registry.register("learn", "default", learn_service)

# --- Advanced query pipeline (replaces SimpleQueryPipeline) ---
from vektra_core.advanced_pipeline import AdvancedQueryPipeline
pipeline = AdvancedQueryPipeline(
    embedding=embedding_provider,
    sparse_embedding=registry.get_optional("sparse_embedding", "default"),
    vector_store=vector_store_adapter,
    llm=llm_provider,
    llm_config=llm_config,
    safeguard=safeguard,
    conversation_store=conversation_store,
    renderer=renderer,
    pipeline_config=pipeline_config,
)
registry.register("query_pipeline", "default", pipeline)

# --- QueryTrace storage (wired at HTTP layer, not pipeline constructor) ---
# analytics_service is NOT injected into the pipeline (import boundary: vektra_core
# cannot import vektra_analytics per ADR-0005). Instead, the query endpoint handler
# in vektra_core/api.py calls analytics_service.store_trace() after pipeline.execute()
# returns. For streaming, the SSE handler captures QueryChunk(type="trace") and stores it.
# The analytics_service is passed to the query router via FastAPI dependency injection.
```

### Router registration

Update `create_app()` to include new routers:

```python
from vektra_analytics.api import router as analytics_router
app.include_router(analytics_router)

if settings.learn_enabled:
    from vektra_learn.api import router as learn_router
    app.include_router(learn_router)

    # Static file serving for chatbot widget
    from fastapi.staticfiles import StaticFiles
    widget_path = Path(__file__).parent.parent.parent / "vektra-learn" / "static"
    if widget_path.exists():
        app.mount("/static", StaticFiles(directory=str(widget_path)), name="static")
```

### Docker Compose additions

```yaml
# TEI - Hugging Face Text Embeddings Inference (optional)
tei:
  image: ghcr.io/huggingface/text-embeddings-inference:cpu-1.6
  profiles:
    - tei
  command: --model-id sentence-transformers/all-MiniLM-L6-v2 --port 8080
  ports:
    - "127.0.0.1:8080:8080"
  deploy:
    resources:
      limits:
        memory: 1G
```

The existing Qdrant service (already defined in Phase 1 as a placeholder) needs healthcheck and environment configuration added.

Update the vektra service memory limit from 2G to 3G to accommodate Phase 2 dependencies (ARCH-064: total estimated ~3.4GB, PostgreSQL uses 512MB separately).

### Dockerfile changes

Add a Node.js builder stage for the chatbot widget:

```dockerfile
# Stage 1.5: widget builder (Node.js)
FROM node:22-slim AS widget-builder
WORKDIR /widget
COPY vektra-learn/widget/package.json vektra-learn/widget/package-lock.json* ./
RUN npm ci --ignore-scripts
COPY vektra-learn/widget/ ./
RUN node esbuild.config.mjs
```

Add optional Unstructured system dependencies via build arg:

```dockerfile
ARG INSTALL_UNSTRUCTURED=false
RUN if [ "$INSTALL_UNSTRUCTURED" = "true" ]; then \
    apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-eng poppler-utils \
    && rm -rf /var/lib/apt/lists/*; \
fi
```

Copy the built widget from the widget-builder stage:

```dockerfile
COPY --from=widget-builder /widget/../static/vektra-chat.js /app/vektra-learn/static/vektra-chat.js
```

Add new workspace member pyproject.toml files to the dependency manifest copy layer:

```dockerfile
COPY vektra-analytics/pyproject.toml vektra-analytics/pyproject.toml
COPY vektra-learn/pyproject.toml vektra-learn/pyproject.toml
```

### CI updates

Add `test-analytics` and `test-learn` jobs to `.github/workflows/ci-unit.yml`, following the existing pattern. Update the path filter to detect changes in the new component directories. Add both to the `ci-gate` job's needs list.

Update `.github/workflows/lint.yml` to include `vektra_analytics` and `vektra_learn` in the mypy command.

### Makefile additions

```makefile
test-all: ## Run all unit tests (including new components)
	uv run pytest \
		vektra-shared/tests/ \
		vektra-admin/tests/ \
		vektra-core/tests/ \
		vektra-ingest/tests/ \
		vektra-index/tests/ \
		vektra-analytics/tests/ \
		vektra-learn/tests/ \
		-v --tb=short -m "not integration"

reindex: ## Trigger zero-downtime reindex
	@scripts/reindex.sh

batch-ingest: ## Batch ingest files: make batch-ingest DIR=path/to/docs [NS=default]
	$(if $(DIR),,$(error DIR is required. Usage: make batch-ingest DIR=path/to/docs))
	@scripts/batch-ingest.sh "$(DIR)" "$(or $(NS),default)"
```

### Workspace members

Update root `pyproject.toml`:

```toml
[tool.uv.workspace]
members = [
    "vektra-shared", "vektra-core", "vektra-ingest", "vektra-index",
    "vektra-admin", "vektra-app", "vektra-analytics", "vektra-learn"
]
```

Note: the workspace members update is done by component-analytics and component-learn plans respectively. This plan verifies they are present and that `uv sync` resolves correctly.

### CI performance baselines (optional)

If implemented, this adds a step to the integration workflow that:
1. Runs a fixed query workload against the test stack
2. Records p95 latency
3. Compares against a stored baseline in `.github/performance-baseline.json`
4. Fails (or warns) if p95 regresses by more than 20%

This is explicitly optional (EX-004). Implement only if time permits after all required tasks are complete.

## Tasks

### App entrypoint (3 tasks)

- [ ] Update `_step_5_register_providers()` in `vektra_app/main.py` to register: SparseEmbeddingProvider (conditional), QdrantVectorStoreProvider (conditional), AnalyticsService, LearnService (conditional), AdvancedQueryPipeline
- [ ] Update `create_app()` to include analytics_router, learn_router (conditional), and StaticFiles mount for chatbot widget
- [ ] Add startup validation steps for new providers: analytics DB connectivity, learn JWT secret check (when learn_enabled), Qdrant connectivity (when vector_store_backend == "qdrant")

### Docker Compose (2 tasks)

- [ ] Add TEI service with profile `tei`, healthcheck, and memory limit (1G)
- [ ] Update vektra service memory limit from 2G to 3G (ARCH-064), add Qdrant healthcheck to existing placeholder service

### Dockerfile (2 tasks)

- [ ] Add Node.js widget-builder stage, copy built `vektra-chat.js` to runtime image
- [ ] Add `INSTALL_UNSTRUCTURED` build arg for optional Tesseract and Poppler system dependencies, add new workspace member pyproject.toml and src copies

### CI (2 tasks)

- [ ] Add `test-analytics` and `test-learn` jobs to `ci-unit.yml` with path filters, update `ci-gate` needs list
- [ ] Update `lint.yml` mypy command to include `vektra_analytics` and `vektra_learn` source paths

### Makefile and workspace (2 tasks)

- [ ] Update `test` target in Makefile to include vektra-analytics and vektra-learn test paths, add `reindex` and `batch-ingest` targets
- [ ] Verify `uv sync --dev --frozen` resolves correctly with all 8 workspace members, fix any dependency conflicts

### Performance baselines - optional (1 task)

- [ ] (Optional) Add CI performance baseline step to `integration.yml`: run fixed workload, record p95, compare against `.github/performance-baseline.json`, warn on 20%+ regression

## Acceptance Criteria

- [ ] Application starts successfully with all Phase 2 providers registered (AnalyticsService, LearnService, AdvancedQueryPipeline)
- [ ] `docker compose up -d` starts the core stack (vektra + postgres) within 60 seconds (NFR-004)
- [ ] `docker compose --profile qdrant up -d` starts with Qdrant as vector store backend
- [ ] `docker compose --profile tei up -d` starts with TEI as embedding provider
- [ ] Dockerfile builds successfully with `INSTALL_UNSTRUCTURED=true` and includes Tesseract
- [ ] Dockerfile builds the chatbot widget via Node.js builder stage
- [ ] CI runs unit tests for vektra-analytics and vektra-learn on relevant path changes
- [ ] CI lint job type-checks vektra_analytics and vektra_learn with mypy
- [ ] `uv sync --dev --frozen` resolves all 8 workspace members without errors
- [ ] Makefile `test` target runs all component tests including analytics and learn
- [ ] Application memory usage stays below 3.5GB under Phase 2 full-featured configuration (ARCH-064)

## Testing Approach

Integration testing is the primary verification method for this plan. The existing integration workflow (`integration.yml`) builds the Docker image, starts the stack, and runs integration tests. This plan extends those tests to verify:

1. Startup succeeds with all Phase 2 providers (analytics, learn, advanced pipeline)
2. New routers respond to requests (/api/v1/traces, /api/v1/learn/*)
3. Static file serving works for /static/vektra-chat.js
4. Docker Compose profiles start correctly (qdrant, tei)

Unit tests are minimal in this plan since the logic lives in the component plans. The focus is on wiring correctness: providers registered in the right order, routers included, middleware configured.

## Integration Notes

This plan is the final assembly point. It must be executed after all other Phase 2 plans are complete. If any upstream plan is incomplete, the corresponding provider registration should be guarded by a feature flag or conditional import to avoid blocking the integration.

The startup validation sequence (ARCH-057) grows from 8 steps to approximately 11 steps. The additional steps (Qdrant connectivity, analytics DB, learn JWT) are conditional and should not add significant startup time. Monitor NFR-004 (60-second startup limit) during integration testing.

The version bump from `0.2.0-dev` to `0.2.0` happens after all plans are complete and integration tests pass. This plan does not perform the version bump; it is a separate release task.

## Notes

<!-- Progress notes during implementation -->
