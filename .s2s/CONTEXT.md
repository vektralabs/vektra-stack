# Vektra - Project Context

<!--
This file is maintained by Spec2Ship init command.
Import this in CLAUDE.md using @.s2s/CONTEXT.md
Run /s2s:init to populate or update this file.

NOTE: S2S commands, paths, and how-to documentation are in README.md (not loaded in memory)
-->

## System Overview

Modular open-source platform for Retrieval-Augmented Generation (RAG) with specialized verticals for e-learning and beyond. Vektra is designed as infrastructure, not as a consumer application. It provides building blocks for developers and organizations that need to integrate RAG capabilities into their systems, with a specialized vertical for learning management and knowledge delivery.

This repository (vektra-stack) is the main development monorepo containing core platform components and the e-learning vertical. Separate repositories exist only for components with incompatible tech stacks or deployment targets (Moodle plugin, SDKs).

## Business Domain

AI / ML platform - Modular RAG infrastructure with a first vertical deployment in educational technology.

## System Objectives

- New product development: build a modular open-source RAG platform from scratch
- Deliver a complete feature set as defined in the functional and business requirements

## System Constraints

- On-premises deployment support required (university infrastructure)
- GDPR compliance: configurable retention, audit logs, student data on-premises
- Multi-provider LLM support from the start (OpenAI, Anthropic, Ollama)
- Vendor-neutral vector store (pluggable backend, pgvector as default)
- Pipeline orchestration via n8n (external to Vektra)
- Configuration over fork: customizations via config, not code forks

## Repository Strategy

**Hybrid monorepo approach** (see [ADR-0001](decisions/ADR-0001-hybrid-monorepo-strategy.md)):

- **Monorepo (vektra-stack)**: all Python components sharing types, config, and deployment target
- **Separate repos**: only for components with incompatible stack or lifecycle
  - vektra-moodle: PHP plugin deployed into Moodle, tied to Moodle versions
  - vektra-sdk-py / vektra-sdk-js: published to PyPI/npm, independent versioning (Phase 3)

Split criteria defined in [ADR-0002](decisions/ADR-0002-repo-split-criteria.md).

## Cross-Cutting Concerns

<!-- Populated by /s2s:design -->
- **Authentication**: API key authentication with argon2id hashing, scoped permissions (admin/ingest/query per REQ-031, multiple scopes per key). Single trust boundary at vektra-core gateway. Rate limiting slot in middleware (Phase 2 enforcement). See [ADR-0010](decisions/ADR-0010-authentication-gateway.md).
- **Authorization**: Namespace isolation via PostgreSQL RLS policies. Application-level filtering for Phase 1, RLS binding via feature flag for multi-tenant activation. Namespace as first-class entity with metadata (ARCH-047). See [ADR-0009](decisions/ADR-0009-namespace-isolation-rls.md).
- **Logging**: structlog with JSON output, PII redaction processors. Correlation ID propagation across sync calls and arq jobs. OpenTelemetry spans at module boundaries. QueryTrace (ARCH-041) for RAG-specific observability, separate from audit log.
- **Monitoring**: Prometheus metrics on /metrics via starlette-prometheus. Hierarchical health endpoints (GET /health, GET /health/{component}). Memory observability via GET /health/memory.
- **Security**: TLS termination at reverse proxy layer (NFR-012). Encryption at rest via pgcrypto for conversations (ARCH-031) and PostgreSQL TDE for database. Soft delete for compliance (REQ-057). SafeguardResult supports content modification for PII anonymization (ARCH-049). See [architecture.md](architecture.md#security).
- **Extensibility**: 9 Protocol interfaces with ProviderRegistry (ARCH-039). Forward-compatible data model with Phase 2 fields present from Phase 1 (ARCH-040). EventEmitter for internal hooks (ARCH-038). LlamaIndex not adopted for Phase 1-2, standalone evaluation via RAGAS/DeepEval (ARCH-046). Three-tier evaluation strategy: CI synthetic tests, staging eval mode, production metrics-only (ARCH-050).
- **Pipeline quality**: SimpleQueryPipeline includes retrieval quality controls (relevance threshold, overlap deduplication, no-relevant-context detection per ARCH-056) and token budget allocation (ARCH-055) for prompt construction. Prompt templates are composable Jinja2 files (system, context, conversation per ARCH-054) with configurable path. Startup validation sequence (ARCH-057) ensures clear error reporting on misconfiguration.

## Components

### Monorepo components (vektra-stack/)

| Component | Role | Phase |
|-----------|------|-------|
| vektra-core | RAG engine, LLM abstraction, conversation management, safeguards | 1 |
| vektra-ingest | Document processing pipeline (PDF, OCR, PPT, Word) | 1 |
| vektra-index | Vector store abstraction, embedding, semantic search | 1 |
| vektra-analytics | Metrics aggregation, reporting API, alerting | 2 |
| vektra-learn | E-learning vertical backend (LMS-agnostic) | 2 |
| vektra-admin | System administration interface | 1 (minimal), 2 (full) |

### Separate repositories

| Component | Role | Reason | Phase |
|-----------|------|--------|-------|
| vektra-moodle | Moodle LMS adapter (PHP plugin) | PHP, Moodle Plugin Directory, different lifecycle | 2 |
| vektra-sdk-py | Python SDK | Published to PyPI, independent versioning | 3 |
| vektra-sdk-js | JavaScript/TypeScript SDK | Published to npm, independent versioning | 3 |

### Component relationships

```
vektra-core ─────┬──── vektra-ingest
                 │
                 ├──── vektra-index
                 │
                 └──── vektra-analytics
                            │
vektra-learn ───────────────┘ (uses core + analytics)
     │
     │ (LMS-agnostic API)
     │
vektra-moodle ──────────────── (integrates learn into Moodle)
```

**vektra-learn** is LMS-agnostic. It exposes APIs for:
- Enrollment registration (student X enrolled in course Y)
- Content ingestion trigger (with course metadata)
- Dashboard token generation
- Chatbot embedding with course context

**vektra-moodle** is the Moodle-specific adapter that calls these APIs.

## Requirements

See [requirements.md](requirements.md) for the complete Software Requirements Specification.

**Key Phase 1 deliverables**:
- 60 approved functional requirements (REQ-001 to REQ-065, some IDs unused)
- 5 business rules
- 13 non-functional requirements (8 HARD, 5 TARGET)
- 14 explicit exclusions (Phase 2/3 deferrals)
- 7 open questions (1 resolved, remainder deferred to design or Phase 2)

**Primary user persona**: Platform Operator (DevOps/platform teams)
**MVP exit criterion**: 30 minutes from git clone to successful query

## Architecture

See [architecture.md](architecture.md) for complete architecture documentation.

**Architectural style**: Modular monolith for Phase 1. Single deployable container with internal package boundaries. See [ADR-0003](decisions/ADR-0003-modular-monolith-phase1.md).

**Deployment**: Docker-compose stack: vektra + postgres (always), ollama (profile: local-llm), qdrant (profile: qdrant, Phase 2). See [ADR-0004](decisions/ADR-0004-minimal-docker-compose-stack.md), [ADR-0012](decisions/ADR-0012-docker-compose-spec.md).

**Key technology choices**:
- Web framework: FastAPI 0.115+ with Pydantic v2
- LLM abstraction: litellm (~5MB footprint)
- Embeddings: sentence-transformers (all-MiniLM-L6-v2) via EmbeddingProvider Protocol
- Vector store: pgvector (PostgreSQL extension) via VectorStoreProvider Protocol
- Background tasks: arq with PostgreSQL job persistence
- PDF extraction: pdfplumber (Phase 1), Unstructured (Phase 2) via DocumentExtractor Protocol
- Content type detection: python-magic (magic bytes)
- ORM: SQLAlchemy 2.0 async with asyncpg (ORM models internal to modules, Pydantic models as public API)

**Protocol interfaces** (9 defined in vektra_shared):
- LLMProvider: multi-provider LLM abstraction with graceful degradation
- EmbeddingProvider: shared embedding generation with asymmetric model support
- SparseEmbeddingProvider: sparse vector generation for hybrid search (ARCH-053). Phase 1: not registered. Phase 2: BM25 or SPLADE via fastembed
- VectorStoreProvider: pluggable vector store with SearchMode, metadata filtering, index versioning, raw_filters escape hatch, full-store contract (ARCH-051), provider-specific atomicity (ARCH-052). Phase 2 candidate: Qdrant
- DocumentExtractor: PDF, Word, PowerPoint extraction with extended element classification (10 ElementType values)
- ChunkingStrategy: pluggable chunking (fixed-size Phase 1, dual-strategy Phase 2)
- QueryPipeline: RAG pipeline abstraction returning QueryResponse + QueryTrace, rerankers library recommended for Phase 2
- SafeguardHook: pre/post query safeguards (3 trust boundary points) with content modification support (ARCH-049)
- EventEmitter: internal event hooks (NoOp Phase 1, webhooks Phase 2)

**Key decisions** (60 total, 22 ADRs):
- [ADR-0003](decisions/ADR-0003-modular-monolith-phase1.md): Modular monolith for Phase 1
- [ADR-0005](decisions/ADR-0005-module-boundary-enforcement.md): Module boundary enforcement
- [ADR-0006](decisions/ADR-0006-background-tasks-arq.md): Background tasks with arq
- [ADR-0007](decisions/ADR-0007-tech-stack.md): Technology stack selection
- [ADR-0008](decisions/ADR-0008-llm-abstraction-litellm.md): LLM abstraction with litellm
- [ADR-0011](decisions/ADR-0011-conversation-encryption.md): Conversation encryption
- [ADR-0013](decisions/ADR-0013-embedding-provider-protocol.md): EmbeddingProvider Protocol
- [ADR-0014](decisions/ADR-0014-query-pipeline-abstraction.md): QueryPipeline abstraction
- [ADR-0015](decisions/ADR-0015-forward-compatible-data-model.md): Forward-compatible data model
- [ADR-0016](decisions/ADR-0016-llamaindex-deferral.md): LlamaIndex deferral (Phase 1-2)
- [ADR-0017](decisions/ADR-0017-audit-analytics-separation.md): Audit/analytics separation via QueryTrace
- [ADR-0018](decisions/ADR-0018-safeguard-content-modification.md): SafeguardResult content modification
- [ADR-0019](decisions/ADR-0019-rag-evaluation-strategy.md): Three-tier RAG evaluation strategy
- [ADR-0020](decisions/ADR-0020-prompt-template-architecture.md): Composable Jinja2 prompt templates
- [ADR-0021](decisions/ADR-0021-retrieval-quality-controls.md): Retrieval quality controls in QueryPipeline
- [ADR-0022](decisions/ADR-0022-orm-sqlalchemy-async.md): SQLAlchemy 2.0 async with asyncpg for ORM

## Open Questions

- **Periodic indexing pattern**: n8n orchestrates ingestion, but the scheduling pattern (e.g., daily sync of Moodle materials) needs documentation as a reference workflow
- ~~**ORM layer**: SQLAlchemy 2.0 async (with asyncpg) vs SQLModel. Must be decided before implementation (OQ-017)~~ Resolved: ADR-0022
- **learn-ui architecture**: is the chatbot widget a standalone npm package or served by the backend? (OQ-018)
- **admin-ui architecture**: is admin a separate SPA or integrated? (OQ-018)
- **Phase 2 hardware minimum**: Phase 2 full-featured estimated at ~3.4GB total. Recommend 8GB / 4 CPU target (OQ-019)

---

*Last updated: 2026-02-09*
