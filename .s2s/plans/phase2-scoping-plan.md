# Phase 2 scoping plan

**Created**: 2026-03-01
**Purpose**: Phase 1 of three-phase plan generation (scoping -> validation -> detailed plans)
**Status**: draft

---

## Scope overview

Phase 2 builds on the v0.1.0 foundation to deliver:
- Multi-tenancy with RLS enforcement
- Advanced RAG pipeline (hybrid search, query rewriting, reranking)
- Persistent encrypted conversations with feedback loops
- Enhanced document processing (OCR, dual chunking, versioning)
- Admin UI (HTMX + Jinja2, ADR-0024)
- Two new components: vektra-analytics, vektra-learn
- Chatbot widget for LMS integration (ADR-0025)
- Technical debt resolution (DEBT-002..008)

**Out of scope (Phase 3)**: SDKs (vektra-sdk-py, vektra-sdk-js), separate SPA admin frontend, npm chat widget package, CLI tool (EX-001), LlamaIndex/LangGraph adoption.

---

## Plans (11 total)

### 1. shared-protocols-phase2

**Component**: vektra-shared
**Complexity**: medium

New and extended Protocol definitions required by all Phase 2 components.

| Feature | Traceability |
|---------|-------------|
| SparseEmbeddingProvider Protocol definition | ARCH-053 |
| AdvancedQueryPipeline Protocol (rewrite, rerank, hybrid) | ARCH-036, ARCH-061, ADR-0014 |
| WebhookEventEmitter implementation (HMAC-SHA256) | ARCH-038, REQ-061 |
| Extended Namespace type (quota enforcement fields) | ARCH-047 |
| DualStrategyChunking Protocol additions | ARCH-037, REQ-054 |
| import-linter rules for vektra-analytics, vektra-learn | ADR-0005 |
| entry_points plugin discovery for ProviderRegistry | ARCH-039, REQ-062 |

**Provides**: all new Protocol interfaces, import boundary rules for new components
**Requires**: nothing

---

### 2. database-phase2

**Component**: infra (Alembic migrations)
**Complexity**: medium

New tables and schema changes for Phase 2 features.

| Feature | Traceability |
|---------|-------------|
| conversations + conversation_turns tables (pgcrypto) | REQ-049, ARCH-031 |
| query_traces table | ARCH-041, REQ-060 |
| feedback table | REQ-055 |
| Unique partial index on source_documents (TOCTOU fix) | TECH-004, REQ-033 |
| Namespace quota columns activation (forward-compatible) | ARCH-040, ARCH-047 |

**Provides**: all Phase 2 database tables, migration chain
**Requires**: nothing

---

### 3. index-hybrid

**Component**: vektra-index
**Complexity**: large

Hybrid search, Qdrant support, and reindex API.

| Feature | Traceability |
|---------|-------------|
| SparseEmbeddingProvider implementation (BM25 via fastembed) | ARCH-053 |
| Hybrid search mode in PgvectorProvider (DENSE + SPARSE) | ARCH-053, REQ-050 |
| QdrantVectorStoreProvider (alternative backend) | ARCH-051 |
| Zero-downtime reindex API with progress tracking | ARCH-045, REQ-064 |
| Chunk metadata with domain-specific fields | ARCH-044 |
| Budget allocator ordering fix | DEBT-004 |

**Provides**: hybrid search, Qdrant provider, reindex API
**Requires**: shared-protocols-phase2 (SparseEmbeddingProvider Protocol)

---

### 4. admin-enforcement

**Component**: vektra-admin
**Complexity**: medium

Multi-tenancy enforcement and security hardening.

| Feature | Traceability |
|---------|-------------|
| RLS activation for namespace isolation | ARCH-025, ADR-0009, EX-006 |
| Granular scope enforcement (ingest/query) | REQ-020, REQ-024 |
| Per-key rate limiting enforcement (RPM) | EX-013 |
| Token expiration support | REQ-041 |
| Namespace quotas enforcement | ARCH-047 |
| TTLCache for API key verification | DEBT-008 |

**Provides**: enforced multi-tenancy, scope checking, rate limiting
**Requires**: database-phase2 (namespace quota columns)

---

### 5. core-conversations

**Component**: vektra-core
**Complexity**: medium

Persistent multi-turn conversations with encryption and feedback.

| Feature | Traceability |
|---------|-------------|
| Conversation CRUD with pgcrypto encryption | REQ-049, ARCH-031, ADR-0011 |
| Multi-turn context management | REQ-049 |
| Feedback endpoints (POST /feedback for response + citation) | REQ-055 |
| Client disconnect cancellation | DEBT-005 |

**Provides**: conversation persistence, feedback API
**Requires**: database-phase2 (conversations, feedback tables)

---

### 6. ingest-phase2

**Component**: vektra-ingest
**Complexity**: large

Enhanced document processing, versioning, and event emission.

| Feature | Traceability |
|---------|-------------|
| OCR support for scanned PDFs (Unstructured) | EX-002, REQ-002, REQ-016 |
| DualStrategyChunking (table preservation, parent-child) | REQ-054, ARCH-037 |
| Document re-ingestion with version increment | REQ-056 |
| Batch operations (multi-document ingest/delete) | EX-005 |
| Markdown file ingestion | EX-009 |
| Ingest phase tracking (extracting -> chunking -> embedding) | DEBT-006 |
| Audit log for error responses (409, 422) | DEBT-007 |
| Arq cleanup job for soft-deleted documents | REQ-057 |
| Webhook event emission for ingest outcomes | REQ-061, EX-011 |
| Granular ingest APIs (extract/clean/chunk steps) | EX-010 |

**Provides**: OCR, dual chunking, versioning, batch ops, webhook events
**Requires**: shared-protocols-phase2 (DualStrategyChunking, WebhookEventEmitter), database-phase2

---

### 7. core-pipeline-v2

**Component**: vektra-core
**Complexity**: large

Advanced query pipeline with rewriting, reranking, and safeguards.

| Feature | Traceability |
|---------|-------------|
| AdvancedQueryPipeline implementation | ARCH-036, ADR-0014 |
| Conversational query rewriting (ARCH-061) | ADR-0023 |
| Reranking step (cross-encoder) | ARCH-036 |
| Post-retrieval safeguard boundary | DEBT-003, ARCH-049 |
| Safeguard implementations (Presidio PII) | REQ-044, ARCH-049 |
| Streaming QueryTrace collection | DEBT-002, ARCH-041 |
| Enhanced graceful degradation matrix | ARCH-043 |
| Prompt versioning for A/B testing | ARCH-048 |

**Provides**: advanced pipeline, safeguards, streaming trace
**Requires**: shared-protocols-phase2 (AdvancedQueryPipeline Protocol), index-hybrid (hybrid search), core-conversations (conversation context)

---

### 8. admin-ui

**Component**: vektra-admin
**Complexity**: medium

HTMX + Jinja2 admin dashboard (ADR-0024).

| Feature | Traceability |
|---------|-------------|
| Health dashboard page | REQ-006 |
| API keys management (list, create, revoke) | REQ-006, ARCH-062 |
| Namespace management (list, create, delete) | REQ-006, ARCH-062 |
| Audit log viewer with pagination and filtering | REQ-006, ARCH-062 |
| System config viewer (read-only) | ARCH-062 |

**Design constraint**: no business logic in templates, all pages call REST API endpoints (Phase 3 SPA migration path).

**Provides**: admin dashboard UI
**Requires**: admin-enforcement (endpoints to render)

---

### 9. component-analytics

**Component**: vektra-analytics (NEW)
**Complexity**: medium

New component for query observability and metrics.

| Feature | Traceability |
|---------|-------------|
| Component scaffold (pyproject.toml, package, import-linter) | ADR-0001 |
| QueryTrace dedicated storage and query API | ARCH-041, REQ-060 |
| /api/v1/traces endpoint with filtering | REQ-060 |
| Metrics aggregation (query latency, retrieval quality) | EX-007 |
| Reporting API for operator dashboards | EX-007 |

**Provides**: QueryTrace storage, metrics API, reporting
**Requires**: database-phase2 (query_traces table), core-pipeline-v2 (QueryTrace emission)

---

### 10. component-learn

**Component**: vektra-learn (NEW)
**Complexity**: large

E-learning vertical backend and chatbot widget.

| Feature | Traceability |
|---------|-------------|
| Component scaffold (pyproject.toml, package, import-linter) | ADR-0001 |
| Enrollment registration API (student-course binding) | CONTEXT.md |
| Content ingestion trigger with course metadata | CONTEXT.md |
| Dashboard token generation | CONTEXT.md |
| Course-scoped query pipeline integration | ARCH-044 |
| Chatbot widget (JS bundle at /static/vektra-chat.js) | ADR-0025, ARCH-063 |
| Widget configuration via data-* attributes | ADR-0025 |

**Design constraint**: REST API only, widget self-contained, stable data-* API for Phase 3 npm migration.

**Provides**: LMS-agnostic e-learning API, chatbot widget
**Requires**: core-pipeline-v2, component-analytics

---

### 11. infra-phase2

**Component**: vektra-app, Docker, CI
**Complexity**: medium

Integration layer: app entrypoint, Docker, CI updates.

| Feature | Traceability |
|---------|-------------|
| Updated lifespan for new providers (analytics, learn, Qdrant) | ARCH-057 |
| Docker Compose: Qdrant service (profile: qdrant) | ADR-0004, ARCH-051 |
| Docker Compose: TEI service option (profile: tei) | ARCH-035, ARCH-064 |
| Updated Dockerfile (new dependencies: Unstructured, fastembed) | ARCH-064 |
| CI performance baselines (latency regression detection) | EX-004 |
| Updated Makefile targets for Phase 2 workflows | - |
| Workspace members: add vektra-analytics, vektra-learn | ADR-0001 |

**Provides**: complete Phase 2 deployment stack
**Requires**: all plans (registers all new providers and components)

---

## Wave structure

### Wave 0 - Foundation (parallel)

| Plan | Title | Complexity |
|------|-------|------------|
| shared-protocols-phase2 | Protocol additions and import boundaries | medium |
| database-phase2 | New tables, migrations, TOCTOU fix | medium |

---

### Wave 1 - Component enhancements (parallel)

| Plan | Title | Complexity |
|------|-------|------------|
| index-hybrid | Hybrid search, Qdrant, reindex API | large |
| admin-enforcement | RLS, scope enforcement, rate limiting | medium |
| core-conversations | Persistent conversations, feedback | medium |
| ingest-phase2 | OCR, dual chunking, versioning, batch ops | large |

All require Wave 0 completion. No inter-dependencies within Wave 1.

---

### Wave 2 - Advanced features (parallel)

| Plan | Title | Complexity |
|------|-------|------------|
| core-pipeline-v2 | Advanced pipeline, safeguards, streaming trace | large |
| admin-ui | HTMX + Jinja2 admin dashboard | medium |

- core-pipeline-v2 requires: index-hybrid + core-conversations (Wave 1)
- admin-ui requires: admin-enforcement (Wave 1)

---

### Wave 3 - Analytics

| Plan | Title | Complexity |
|------|-------|------------|
| component-analytics | QueryTrace storage, metrics, reporting API | medium |

Requires: core-pipeline-v2 (Wave 2)

---

### Wave 4 - E-learning vertical

| Plan | Title | Complexity |
|------|-------|------------|
| component-learn | LMS-agnostic API, chatbot widget | large |

Requires: component-analytics (Wave 3)

---

### Wave 5 - Integration

| Plan | Title | Complexity |
|------|-------|------------|
| infra-phase2 | App entrypoint, Docker, CI | medium |

Requires: all plans completed

---

## Dependency graph

```
shared-protocols-phase2 ──┬──► index-hybrid ─────────────┐
                          ├──► admin-enforcement ──► admin-ui
                          ├──► core-conversations ──┐     │
                          └──► ingest-phase2        │     │
                                                    ▼     │
database-phase2 ──────────┘  core-pipeline-v2 ◄────┘     │
                                    │                     │
                                    ▼                     │
                             component-analytics          │
                                    │                     │
                                    ▼                     │
                             component-learn              │
                                    │                     │
                                    ▼                     │
                              infra-phase2 ◄──────────────┘
```

---

## Provides/requires summary

| Plan | Provides | Requires |
|------|----------|----------|
| shared-protocols-phase2 | Protocol interfaces, import rules | - |
| database-phase2 | Tables, migrations | - |
| index-hybrid | Hybrid search, Qdrant, reindex | shared-protocols-phase2 |
| admin-enforcement | RLS, scopes, rate limiting | database-phase2 |
| core-conversations | Conversation CRUD, feedback API | database-phase2 |
| ingest-phase2 | OCR, dual chunking, versioning | shared-protocols-phase2, database-phase2 |
| core-pipeline-v2 | Advanced pipeline, safeguards, trace | shared-protocols-phase2, index-hybrid, core-conversations |
| admin-ui | Admin dashboard pages | admin-enforcement |
| component-analytics | QueryTrace storage, metrics API | database-phase2, core-pipeline-v2 |
| component-learn | E-learning API, chatbot widget | core-pipeline-v2, component-analytics |
| infra-phase2 | Deployment stack | all |

---

## Risk areas

1. **core-pipeline-v2 is the bottleneck**: 3 dependencies (shared-protocols, index-hybrid, core-conversations) and 2 dependents (analytics, learn). Any delay cascades.

2. **Qdrant integration complexity**: first non-PostgreSQL provider. Docker Compose profiles, CI matrix expansion, integration test isolation.

3. **OCR dependency size**: Unstructured library adds significant container weight. Impacts ARCH-064 (8GB target).

4. **Chatbot widget build pipeline**: first JS artifact in a Python-only project. Needs esbuild or similar, CI integration for JS build.

5. **RLS activation is a breaking change**: application-level filtering must coexist with RLS during migration. Feature flag controls activation.

---

## Backlog items addressed

| Backlog item | Addressed in plan |
|-------------|-------------------|
| TECH-004 (unique indexes) | database-phase2 |
| DEBT-002 (streaming trace) | core-pipeline-v2 |
| DEBT-003 (post_retrieval hook) | core-pipeline-v2 |
| DEBT-004 (budget ordering) | index-hybrid |
| DEBT-005 (disconnect cancel) | core-conversations |
| DEBT-006 (ingest phase tracking) | ingest-phase2 |
| DEBT-007 (audit log errors) | ingest-phase2 |
| DEBT-008 (TTLCache) | admin-enforcement |

**Not addressed in plans** (separate backlog items):
- DOCS-004 (traceability tables): can be done anytime, not a plan dependency
- DOCS-005 (roundtable QA+BA): can be done anytime
- TECH-002 (good-first-issue): before public announcement, not blocking Phase 2
- DOCS-002 (SSG choice): deferred
- DOCS-003 (API auto-gen): deferred

---

## Feature-to-requirement traceability

| Requirement | Plan |
|-------------|------|
| REQ-002 (OCR, async jobs) | ingest-phase2 |
| REQ-006 (admin UI) | admin-ui |
| REQ-016 (scanned PDF handling) | ingest-phase2 |
| REQ-020 (scope enforcement) | admin-enforcement |
| REQ-024 (ingest/query scopes) | admin-enforcement |
| REQ-033 (filename dedup) | database-phase2 |
| REQ-041 (token expiration) | admin-enforcement |
| REQ-044 (safeguard implementations) | core-pipeline-v2 |
| REQ-049 (persistent conversations) | core-conversations |
| REQ-050 (Qdrant support) | index-hybrid |
| REQ-054 (dual chunking) | ingest-phase2 |
| REQ-055 (feedback loops) | core-conversations |
| REQ-056 (document versioning) | ingest-phase2 |
| REQ-057 (soft delete cleanup) | ingest-phase2 |
| REQ-060 (QueryTrace storage) | component-analytics |
| REQ-061 (WebhookEventEmitter) | shared-protocols-phase2 |
| REQ-062 (plugin discovery) | shared-protocols-phase2 |
| REQ-064 (reindex API) | index-hybrid |
| EX-002 (OCR) | ingest-phase2 |
| EX-005 (batch ops) | ingest-phase2 |
| EX-006 (multi-tenant) | admin-enforcement |
| EX-007 (analytics) | component-analytics |
| EX-009 (markdown) | ingest-phase2 |
| EX-010 (granular ingest) | ingest-phase2 |
| EX-011 (ingest events) | ingest-phase2 |
| EX-012 (chunking strategies) | ingest-phase2 |
| EX-013 (rate limiting) | admin-enforcement |
