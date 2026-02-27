# Configuration reference

All configuration is via environment variables. Set them in `.env` (loaded by Docker Compose) or export them in your shell.

Copy `.env.example` as a starting point:

```bash
cp .env.example .env
```

Source of truth: `vektra_shared/config.py` (ARCH-060).

## Required variables

Only one variable is strictly required:

| Variable | Type | Description |
|----------|------|-------------|
| `VEKTRA_LLM_PROVIDER` | str | LLM model in litellm format (e.g., `ollama/llama3`, `openai/gpt-4o`, `anthropic/claude-sonnet-4-20250514`) |

If using a cloud provider, also set the corresponding API key (see [LLM provider keys](#llm-provider-keys)).

## LLM

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_LLM_PROVIDER` | str | (required) | LLM model in litellm format |
| `VEKTRA_LLM_API_KEY` | str | - | API key for the LLM provider. Not needed for Ollama. |
| `VEKTRA_LLM_FALLBACK_MODEL` | str | - | Fallback model when the primary times out |
| `VEKTRA_LLM_FALLBACK_TIMEOUT_MS` | int | `30000` | Milliseconds before switching to fallback model |
| `VEKTRA_LLM_CONTEXT_ONLY_ENABLED` | bool | `true` | Return raw chunks without LLM synthesis when both models fail |
| `VEKTRA_STARTUP_LLM_CHECK` | bool | `true` | Test LLM connectivity at startup (warning-only, not fatal) |

### LLM provider keys

Set one based on your `VEKTRA_LLM_PROVIDER`:

| Variable | Provider |
|----------|----------|
| `OPENAI_API_KEY` | OpenAI (`openai/*`) |
| `ANTHROPIC_API_KEY` | Anthropic (`anthropic/*`) |

These are standard provider environment variables recognized by litellm. `VEKTRA_LLM_API_KEY` can also be used as a generic alternative.

## Embedding

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_EMBEDDING_PROVIDER` | str | `sentence-transformers` | Embedding provider implementation |
| `VEKTRA_EMBEDDING_MODEL` | str | `all-MiniLM-L6-v2` | Model name within the selected provider |
| `VEKTRA_SPARSE_EMBEDDING_PROVIDER` | str | - | Sparse embedding provider (Phase 2: `fastembed-bm25`, `splade`) |
| `VEKTRA_SPARSE_EMBEDDING_MODEL` | str | - | Sparse embedding model name (Phase 2) |

## Vector store

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_VECTOR_STORE_PROVIDER` | str | `pgvector` | Vector store implementation (Phase 2: `qdrant`) |
| `VEKTRA_ACTIVE_INDEX_VERSION` | int | `1` | Active index version for search. Change after re-embedding for zero-downtime reindex. |

## Query pipeline

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_QUERY_PIPELINE` | str | `simple` | Pipeline implementation: `simple` (Phase 1), `advanced` (Phase 2) |
| `VEKTRA_MIN_RELEVANCE_SCORE` | float | `0.3` | Minimum cosine similarity for chunk inclusion (0.0-1.0). Chunks below this threshold are filtered out. |
| `VEKTRA_CHUNK_DEDUP_ENABLED` | bool | `true` | Deduplicate overlapping adjacent chunks from the same document |
| `VEKTRA_RESPONSE_TOKEN_RESERVE` | int | `1024` | Tokens reserved for LLM response generation |
| `VEKTRA_CONTEXT_CHUNK_RATIO` | float | `0.6` | Fraction of context window allocated to retrieved chunks (0.0-1.0) |
| `VEKTRA_PROMPT_TEMPLATES_DIR` | str | - | Directory for custom Jinja2 prompt templates (`system.j2`, `context.j2`, `conversation.j2`). Uses built-in defaults if unset. |

## Ingestion

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_CHUNKING_STRATEGY` | str | `fixed` | Chunking strategy: `fixed` (Phase 1), `dual` (Phase 2) |
| `VEKTRA_CHUNK_SIZE` | int | `1000` | Token count per chunk |
| `VEKTRA_CHUNK_OVERLAP` | int | `200` | Token overlap between adjacent chunks |
| `VEKTRA_MAX_FILE_SIZE_MB` | int | `50` | Maximum file size for ingestion (megabytes) |
| `VEKTRA_DOCUMENT_EXTRACTOR` | str | `pdfplumber` | Extractor implementation: `pdfplumber` (Phase 1), `unstructured` (Phase 2) |

## Security

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_ADMIN_BOOTSTRAP_KEY` | str | - | Single-use credential for creating the first API key. Consumed after first use. |
| `VEKTRA_ENV` | str | `development` | Environment mode. In `production`, non-TLS connections are rejected. |
| `VEKTRA_SAFEGUARD_MODE` | str | `passthrough` | Safeguard implementation: `passthrough` (Phase 1), `presidio` or `guardrails-ai` (Phase 2) |
| `VEKTRA_MULTI_TENANT` | bool | `false` | Activate PostgreSQL RLS binding (Phase 2). Phase 1 uses application-level namespace filtering. |
| `VEKTRA_CONVERSATION_KEY` | str | - | Symmetric encryption key for conversation content (Phase 2) |

## Observability

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_MAX_CONVERSATION_TURNS` | int | `10` | Maximum conversation history turns retained per conversation |
| `VEKTRA_AUDIT_RETENTION_DAYS` | int | `90` | Audit log retention period in days |
| `VEKTRA_RETENTION_DAYS` | int | - | Soft-deleted record retention period (Phase 2: cleanup job) |
| `VEKTRA_ANALYTICS_RETENTION_DAYS` | int | - | QueryTrace storage retention days (Phase 2) |
| `VEKTRA_EVAL_MODE` | bool | `false` | Enable temporary text capture for batch RAG evaluation (staging only) |

## Server

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_PORT` | int | `8000` | HTTP port for the FastAPI application |
| `VEKTRA_CORS_ORIGINS` | str | `http://localhost:3000` | Comma-separated list of allowed CORS origins |

## Database

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_DATABASE_URL` | str | `postgresql+asyncpg://vektra:vektra@postgres:5432/vektra` | AsyncPG connection string for PostgreSQL |
| `POSTGRES_PASSWORD` | str | `vektra` | PostgreSQL container password (Docker Compose) |

## Infrastructure

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `CMD_TARGET` | str | `server` | Container entrypoint target: `server` (default) or `migrate` |

## Variable count summary

| Category | Count |
|----------|-------|
| VEKTRA_* variables (ARCH-060 registry) | 35 |
| VEKTRA_* infrastructure (`VEKTRA_CORS_ORIGINS`) | 1 |
| External API keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) | 2 |
| Infrastructure (`POSTGRES_PASSWORD`, `CMD_TARGET`) | 2 |
| **Total documented** | **40** |

The ARCH-060 registry covers the 35 VEKTRA_* variables managed by `VektraSettings`. Infrastructure variables (`POSTGRES_PASSWORD`, `CMD_TARGET`, `VEKTRA_CORS_ORIGINS`) are used by Docker Compose or the application factory and are listed here for completeness.
