# Vektra - Workspace Context

<!--
This file is maintained by Spec2Ship init command.
Import this in CLAUDE.md using @.s2s/CONTEXT.md
Run /s2s:init to populate or update this file.

MEMORY LOADING:
- This file is loaded via @ import from CLAUDE.md
- Component CONTEXT.md files reference THIS file via @ cascade
- DO NOT use @ references to component CONTEXT.md files here (memory bloat)
- Components are listed as TEXT only - details loaded on-demand when needed

HEADER CONVENTION:
- Headers use "System" or "Workspace" prefix to avoid ambiguity
- When loaded alongside component CONTEXT.md, Claude can distinguish

NOTE: S2S commands, paths, and how-to documentation are in README.md (not loaded in memory)
-->

## System Overview

Modular open-source platform for Retrieval-Augmented Generation (RAG) with specialized verticals for e-learning and beyond. Vektra is designed as infrastructure, not as a consumer application. It provides building blocks for developers and organizations that need to integrate RAG capabilities into their systems, with a specialized vertical for learning management and knowledge delivery.

This repository (vektra-stack) is the entrypoint and documentation hub for the multi-repo Vektra ecosystem.

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
- Multi-repo structure: each component in a separate repo for independent deployment
- Configuration over fork: customizations via config, not code forks

## Cross-Cutting Concerns

<!-- Populated by /s2s:design or manually -->
- **Authentication**: TBD
- **Authorization**: TBD
- **Logging**: TBD
- **Monitoring**: TBD

## Components

<!-- TEXT ONLY - No @ references to avoid loading all components into memory -->
| Component | Role |
|-----------|------|
| vektra-core | RAG engine, LLM abstraction, conversation management, safeguards |
| vektra-ingest | Document processing pipeline (PDF, OCR, PPT, Word) |
| vektra-index | Vector store abstraction, embedding, semantic search |
| vektra-analytics | Metrics aggregation, reporting API, alerting |
| vektra-learn | E-learning vertical (chatbot, instructor dashboard) |
| vektra-moodle | Moodle LMS adapter (SSO, content sync, embed) |
| vektra-admin | System administration interface |
| vektra-sdk-py | Python SDK |
| vektra-sdk-js | JavaScript/TypeScript SDK |

*Component registry is maintained in `.s2s/workspace.yaml`*

## Architecture Principles

<!-- Populated by /s2s:design -->
TBD - run `/s2s:design` to define architecture

## Workspace Open Questions

<!-- Populated during /s2s:specs or /s2s:design sessions -->
- None identified yet

---

*Last updated: 2026-01-28*
