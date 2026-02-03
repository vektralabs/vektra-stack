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
- **Authentication**: API key authentication with argon2id hashing, scoped permissions (read/ingest/admin). Single trust boundary at vektra-core gateway. See [ADR-0010](decisions/ADR-0010-authentication-gateway.md).
- **Authorization**: Namespace isolation via PostgreSQL RLS policies. Application-level filtering for Phase 1, RLS binding via feature flag for multi-tenant activation. See [ADR-0009](decisions/ADR-0009-namespace-isolation-rls.md).
- **Logging**: structlog with JSON output, PII redaction processors. Correlation ID propagation across sync calls and arq jobs. OpenTelemetry spans at module boundaries.
- **Monitoring**: Prometheus metrics on /metrics via starlette-prometheus. Hierarchical health endpoints (GET /health, GET /health/{component}). Memory observability via GET /health/memory.

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
- 46 approved functional requirements (REQ-001 to REQ-051, some IDs unused)
- 5 business rules
- 13 non-functional requirements (8 HARD, 5 TARGET)
- 12 explicit exclusions (Phase 2/3 deferrals)
- 5 open questions (deferred to design or Phase 2)

**Primary user persona**: Platform Operator (DevOps/platform teams)
**MVP exit criterion**: 30 minutes from git clone to successful query

## Architecture

See [architecture.md](architecture.md) for complete architecture documentation.

**Architectural style**: Modular monolith for Phase 1. Single deployable container with internal package boundaries. See [ADR-0003](decisions/ADR-0003-modular-monolith-phase1.md).

**Deployment**: Three-service docker-compose stack (vektra + postgres + ollama optional). See [ADR-0004](decisions/ADR-0004-minimal-docker-compose-stack.md), [ADR-0012](decisions/ADR-0012-docker-compose-spec.md).

**Key technology choices**:
- Web framework: FastAPI 0.115+ with Pydantic v2
- LLM abstraction: litellm (~5MB footprint)
- Embeddings: sentence-transformers (all-MiniLM-L6-v2)
- Vector store: pgvector (PostgreSQL extension)
- Background tasks: arq with PostgreSQL job persistence
- PDF extraction: pdfplumber

**Protocol interfaces** (defined in vektra_shared):
- LLMProvider: multi-provider LLM abstraction
- VectorStoreProvider: pluggable vector store backend
- DocumentExtractor: PDF, Word, PowerPoint extraction
- SafeguardHook: pre/post query safeguards

**Key decisions** (34 total, 10 ADRs generated):
- [ADR-0003](decisions/ADR-0003-modular-monolith-phase1.md): Modular monolith for Phase 1
- [ADR-0005](decisions/ADR-0005-module-boundary-enforcement.md): Module boundary enforcement
- [ADR-0006](decisions/ADR-0006-background-tasks-arq.md): Background tasks with arq
- [ADR-0007](decisions/ADR-0007-tech-stack.md): Technology stack selection
- [ADR-0008](decisions/ADR-0008-llm-abstraction-litellm.md): LLM abstraction with litellm
- [ADR-0011](decisions/ADR-0011-conversation-encryption.md): Conversation encryption

## Open Questions

- **Periodic indexing pattern**: n8n orchestrates ingestion, but the scheduling pattern (e.g., daily sync of Moodle materials) needs documentation as a reference workflow
- **learn-ui architecture**: is the chatbot widget a standalone npm package or served by the backend? (decide in /s2s:design)
- **admin-ui architecture**: is admin a separate SPA or integrated? (decide in /s2s:design)

---

*Last updated: 2026-02-03*
