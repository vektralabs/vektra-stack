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

<!-- Populated by /s2s:design or manually -->
- **Authentication**: TBD
- **Authorization**: TBD
- **Logging**: TBD
- **Monitoring**: TBD

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

## Architecture Principles

<!-- Populated by /s2s:design -->
TBD - run `/s2s:design` to define architecture

## Open Questions

- **Periodic indexing pattern**: n8n orchestrates ingestion, but the scheduling pattern (e.g., daily sync of Moodle materials) needs documentation as a reference workflow
- **learn-ui architecture**: is the chatbot widget a standalone npm package or served by the backend? (decide in /s2s:design)
- **admin-ui architecture**: is admin a separate SPA or integrated? (decide in /s2s:design)

---

*Last updated: 2026-01-29*
