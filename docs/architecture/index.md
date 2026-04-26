# Architecture

Vektra is a modular monolith: a single deployable container with internal package boundaries enforced by import-linter.

## Components

```text
vektra-app          Application entrypoint, startup validation, middleware
  vektra-core       RAG query pipeline, LLM abstraction, conversations
  vektra-ingest     Document processing (PDF, DOCX, PPTX), chunking, async jobs
  vektra-index      Vector store (pgvector / qdrant), embedding, semantic search
  vektra-analytics  QueryTrace storage, metrics aggregation, reporting (Phase 2)
  vektra-learn      E-learning vertical: LMS-agnostic API and chatbot widget (Phase 2)
  vektra-admin      Health endpoints, API key management, namespace config, audit logging
  vektra-shared     Protocols, types, config, auth, ProviderRegistry
```

Dependencies flow downward: all components depend on `vektra-shared`, but never on each other (enforced by `import-linter` in CI).

## Deployment

Docker Compose stack with two required services:

- **vektra**: FastAPI application (Python 3.12)
- **postgres**: PostgreSQL 16 with pgvector extension

Optional profiles:

- `local-llm`: adds Ollama for local LLM inference
- `qdrant`: adds Qdrant vector store (Phase 2)

## Detailed documentation

Architecture documentation is maintained in the `.s2s/` directory:

- [`.s2s/architecture.md`](../../.s2s/architecture.md) - arc42 architecture document (64 decisions, 9 Protocol interfaces)
- [`.s2s/decisions/`](../../.s2s/decisions/) - Architecture Decision Records (ADR-0001 through ADR-0025)
- [`.s2s/requirements.md`](../../.s2s/requirements.md) - Software Requirements Specification (61 REQs, 13 NFRs)

### Key decisions

| ADR | Title |
|-----|-------|
| [ADR-0003](../../.s2s/decisions/ADR-0003-modular-monolith-phase1.md) | Modular monolith for Phase 1 |
| [ADR-0005](../../.s2s/decisions/ADR-0005-module-boundary-enforcement.md) | Module boundary enforcement |
| [ADR-0006](../../.s2s/decisions/ADR-0006-background-tasks-arq.md) | Background tasks with arq |
| [ADR-0007](../../.s2s/decisions/ADR-0007-tech-stack.md) | Technology stack selection |
| [ADR-0008](../../.s2s/decisions/ADR-0008-llm-abstraction-litellm.md) | LLM abstraction with litellm |
| [ADR-0013](../../.s2s/decisions/ADR-0013-embedding-provider-protocol.md) | EmbeddingProvider Protocol |
| [ADR-0014](../../.s2s/decisions/ADR-0014-query-pipeline-abstraction.md) | QueryPipeline abstraction |
| [ADR-0022](../../.s2s/decisions/ADR-0022-orm-sqlalchemy-async.md) | SQLAlchemy 2.0 async with asyncpg |
| [ADR-0023](../../.s2s/decisions/ADR-0023-conversational-query-rewriting.md) | Conversational query rewriting (Phase 2) |
| [ADR-0024](../../.s2s/decisions/ADR-0024-admin-ui-server-side.md) | Admin UI with server-side rendering (Phase 2: HTMX + Jinja2) |
| [ADR-0025](../../.s2s/decisions/ADR-0025-learn-chatbot-widget.md) | Learn chatbot widget as backend-served JS bundle |

### Protocol interfaces

Vektra defines 9 Protocol interfaces in `vektra_shared` for pluggability:

1. **LLMProvider** - multi-provider LLM abstraction (litellm) with graceful degradation to fallback model and context-only mode.
2. **EmbeddingProvider** - dense embedding generation. Default: `sentence-transformers` with `paraphrase-multilingual-MiniLM-L12-v2`.
3. **SparseEmbeddingProvider** - sparse vectors for hybrid search. Phase 1: not registered. Phase 2: `FastEmbedBM25Provider` via `fastembed`.
4. **VectorStoreProvider** - pluggable vector store with `SearchMode` enum (DENSE/SPARSE/HYBRID). Phase 1: pgvector. Phase 2: also Qdrant with native hybrid search.
5. **DocumentExtractor** - PDF, DOCX, PPTX extraction. Phase 1: pdfplumber. Phase 2: also Unstructured (opt-in, adds OCR).
6. **ChunkingStrategy** - document chunking. Phase 1: fixed-size. Phase 2: also dual-strategy (semantic + table preservation).
7. **QueryPipeline** - RAG pipeline orchestration returning `QueryResponse` + `QueryTrace`. Phase 1: SimpleQueryPipeline. Phase 2: AdvancedQueryPipeline (query rewriting, reranking, hybrid retrieval).
8. **SafeguardHook** - pre/post query safeguards at the three trust boundaries. Phase 1: passthrough. Phase 2: also Presidio (PII detection with content modification, ARCH-049).
9. **EventEmitter** - internal event hooks. Phase 1: NoOpEventEmitter. Phase 2: WebhookEventEmitter (HMAC-SHA256 signed HTTP POST, activated via `VEKTRA_WEBHOOK_URL`).

### Startup validation

The application runs an 11-step validation sequence at startup (ARCH-057). Source of truth: `vektra-app/src/vektra_app/main.py:lifespan`.

1. Configuration validation: instantiate the flat `VektraSettings` aggregation. Sub-configs (`RewriteConfig`, `RerankConfig`, `WebhookConfig`) are validated independently by their consumers, not at this step.
2. Database connectivity
3. Database schema verification
4. pgvector extension check
5. Provider registration (LLM, embedding, sparse embedding, vector store, safeguard, event emitter, key store, conversation store, analytics service, learn service)
6. Embedding model warmup
7. LLM connectivity check (warning-only)
8. Prompt template loading
9. Analytics check: verify `AnalyticsService` is registered in `ProviderRegistry` (registration check only; no storage I/O).
10. Learn check: when `VEKTRA_LEARN_JWT_SECRET` is set, verify a `LearnService` is registered (instantiation happens in step 5) and that the secret is at least 32 characters. Skipped when learn is not configured.
11. Qdrant collection check (when `vector_store_provider=qdrant`)

Steps 9–11 were added in Phase 2 to cover the analytics, e-learning, and hybrid-search features.
