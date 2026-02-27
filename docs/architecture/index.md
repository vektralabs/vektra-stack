# Architecture

Vektra is a modular monolith: a single deployable container with internal package boundaries enforced by import-linter.

## Components

```text
vektra-app          Application entrypoint, startup validation, middleware
  vektra-core       RAG query pipeline, LLM abstraction, conversations
  vektra-ingest     Document processing (PDF, DOCX, PPTX), chunking, async jobs
  vektra-index      Vector store (pgvector), embedding, semantic search
  vektra-admin      Health endpoints, API key management, audit logging
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

- [`.s2s/architecture.md`](../../.s2s/architecture.md) - arc42 architecture document (60 decisions, 9 Protocol interfaces)
- [`.s2s/decisions/`](../../.s2s/decisions/) - Architecture Decision Records (ADR-0001 through ADR-0023)
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

### Protocol interfaces

Vektra defines 9 Protocol interfaces in `vektra_shared` for pluggability:

1. **LLMProvider** - multi-provider LLM abstraction with graceful degradation
2. **EmbeddingProvider** - embedding generation (sentence-transformers in Phase 1)
3. **SparseEmbeddingProvider** - sparse vectors for hybrid search (Phase 2)
4. **VectorStoreProvider** - pluggable vector store (pgvector in Phase 1)
5. **DocumentExtractor** - PDF, DOCX, PPTX extraction
6. **ChunkingStrategy** - document chunking (fixed-size in Phase 1)
7. **QueryPipeline** - RAG pipeline orchestration
8. **SafeguardHook** - pre/post query safeguards
9. **EventEmitter** - internal event hooks (no-op in Phase 1)

### Startup validation

The application runs an 8-step validation sequence at startup (ARCH-057):

1. Configuration validation (Pydantic)
2. Database connectivity
3. Database schema verification
4. pgvector extension check
5. Provider registration
6. Embedding model warmup
7. LLM connectivity check (warning-only)
8. Prompt template verification
