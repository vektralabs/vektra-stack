# Vektra Roadmap

This document outlines the phased development plan for Vektra.

## North Star Metric

**Time from `git clone` to first successful RAG query via API**: target under 30 minutes.

This metric guides all decisions about developer experience, documentation, and default configurations.

## Phase 1: MVP

**Goal**: Minimal viable RAG platform that demonstrates core value proposition.

### Components

| Component | Scope |
|-----------|-------|
| vektra-shared | Shared types, configuration, Protocol interfaces |
| vektra-core | RAG engine, LLM abstraction (OpenAI, Anthropic, Ollama), conversation management |
| vektra-ingest | Document processing (PDF only) |
| vektra-index | Vector store abstraction (pgvector only), embedding, semantic search |
| vektra-admin | Minimal system health monitoring |

### Development Order

1. **vektra-index** (first): Establishes domain vocabulary and data model that other components depend on
2. **vektra-core + vektra-ingest** (parallel): Can develop in parallel once index interfaces stabilize
3. **vektra-admin** (last): Minimal ops interface after core functionality works

### Exit Criteria

- [ ] Clone repo
- [ ] Run `docker compose up`
- [ ] Ingest a PDF document
- [ ] Query via API and receive cited response
- [ ] Total time under 30 minutes

### Exclusions

- Multiple document formats (only PDF)
- Multiple vector stores (only pgvector)
- Analytics and reporting
- E-learning vertical features
- SDKs

---

## Phase 2: Verticals

**Goal**: E-learning vertical with full instructor and student experience.

### Components

| Component | Scope |
|-----------|-------|
| vektra-analytics | Metrics aggregation, reporting API, alerting |
| vektra-learn | E-learning vertical (chatbot widget, instructor dashboard) |
| vektra-moodle | Moodle LMS adapter (SSO, content sync, chatbot embed) |
| vektra-admin | Full administration interface |
| vektra-ingest | Extended formats (PowerPoint, Word, OCR) |
| vektra-index | Additional vector stores (Qdrant, Pinecone) |

### Exit Criteria

- [ ] University deployment scenario works end-to-end
- [ ] Instructor can view analytics for their courses
- [ ] Student can chat with course materials via Moodle
- [ ] Content syncs automatically from Moodle

### Exclusions

- Additional LMS adapters (Canvas, Blackboard)
- Multi-tenancy at core level
- Mobile native apps

---

## Phase 3: Ecosystem

**Goal**: Developer ecosystem enabling third-party integrations and verticals.

### Components

| Component | Scope |
|-----------|-------|
| vektra-sdk-py | Python SDK published to PyPI |
| vektra-sdk-js | JavaScript/TypeScript SDK published to npm |
| Additional providers | More LLM and vector store integrations |
| Community plugins | Plugin architecture for custom verticals |

### Exit Criteria

- [ ] SDK documentation complete
- [ ] At least one community-contributed vertical or plugin
- [ ] SDK stability (no breaking changes for 2+ releases)

---

## Future Considerations (Post-Phase 3)

These items have been discussed but are not committed to any phase:

- Core-level multi-tenancy
- Additional LMS adapters (Canvas, Blackboard)
- Mathematical formula and diagram support
- Auto-generated quizzes from materials
- Proactive suggestions based on study patterns
- Adaptive tutoring
- Advanced multilingual support

---

*This roadmap is derived from roundtable session 20260128-roundtable-vektra (REQ-019, REQ-020).*
