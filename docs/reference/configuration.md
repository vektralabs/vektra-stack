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
| `VEKTRA_LLM_API_BASE` | str | - | Custom API base URL for OpenAI-compatible providers (e.g., vLLM, LMStudio) |
| `VEKTRA_LLM_FALLBACK_MODEL` | str | - | Fallback model when the primary times out |
| `VEKTRA_LLM_FALLBACK_TIMEOUT_MS` | int | `60000` | Milliseconds before switching to fallback model |
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
| `VEKTRA_EMBEDDING_MODEL` | str | `paraphrase-multilingual-MiniLM-L12-v2` | Model name within the selected provider |
| `VEKTRA_SPARSE_EMBEDDING_PROVIDER` | str | - | Sparse embedding provider: `fastembed-bm25`, `splade` |
| `VEKTRA_SPARSE_EMBEDDING_MODEL` | str | - | Sparse embedding model name |

## Vector store

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_VECTOR_STORE_PROVIDER` | str | `pgvector` | Vector store implementation: `pgvector`, `qdrant` |
| `VEKTRA_ACTIVE_INDEX_VERSION` | int | `1` | Active index version for search. Change after re-embedding for zero-downtime reindex. |
| `VEKTRA_QDRANT_URL` | str | `http://localhost:6333` | Qdrant server URL. Only used when `vector_store_provider=qdrant`. |
| `VEKTRA_QDRANT_API_KEY` | str | - | Qdrant API key for authenticated access. Optional. |
| `VEKTRA_QDRANT_COLLECTION` | str | `vektra` | Qdrant collection name |

## Query pipeline

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_QUERY_PIPELINE` | str | `advanced` | Pipeline implementation: `simple`, `advanced` |
| `VEKTRA_MIN_RELEVANCE_SCORE` | float | `0.3` | Minimum cosine similarity for chunk inclusion (0.0-1.0). Chunks below this threshold are filtered out. |
| `VEKTRA_CHUNK_DEDUP_ENABLED` | bool | `true` | Deduplicate overlapping adjacent chunks from the same document |
| `VEKTRA_RESPONSE_TOKEN_RESERVE` | int | `2048` | Tokens reserved for LLM response generation |
| `VEKTRA_CONTEXT_CHUNK_RATIO` | float | `0.6` | Fraction of context window allocated to retrieved chunks (0.0-1.0) |
| `VEKTRA_PROMPT_TEMPLATES_DIR` | str | - | Directory for custom Jinja2 prompt templates (`system.j2`, `context.j2`, `conversation.j2`). Uses built-in defaults if unset. |

### Query rewriting

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_QUERY_REWRITE_ENABLED` | bool | `true` | Enable conversational query rewriting in AdvancedQueryPipeline |
| `VEKTRA_QUERY_REWRITE_MODEL` | str | - | Override LLM model for the rewrite step. Falls back to primary model. |

### Reranking

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_RERANK_ENABLED` | bool | `true` | Enable cross-encoder reranking after retrieval |
| `VEKTRA_RERANK_PROVIDER` | str | `flashrank` | Reranking provider: `flashrank`, `cross-encoder`, `cohere` |
| `VEKTRA_RERANK_MODEL` | str | - | Provider-specific reranking model name |
| `VEKTRA_RERANK_TOP_K` | int | `5` | Final top-k results after reranking |

## Ingestion

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_CHUNKING_STRATEGY` | str | `fixed` | Chunking strategy: `fixed`, `dual` |
| `VEKTRA_CHUNK_SIZE` | int | `500` | Token count per chunk |
| `VEKTRA_CHUNK_OVERLAP` | int | `100` | Token overlap between adjacent chunks |
| `VEKTRA_MAX_FILE_SIZE_MB` | int | `50` | Maximum file size for ingestion (megabytes) |
| `VEKTRA_DOCUMENT_EXTRACTOR` | str | `pdfplumber` | Extractor implementation: `pdfplumber`, `unstructured` |
| `VEKTRA_TABLE_SPLIT` | bool | `false` | Allow splitting table elements across chunks. Dual-strategy only. |
| `VEKTRA_PARENT_CHILD_LEVELS` | int | `0` | Parent-child hierarchy depth. 0=disabled. Must be >= 1 for dual strategy. |

## Security

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_ADMIN_BOOTSTRAP_KEY` | str | - | Single-use credential for creating the first API key. Consumed after first use. |
| `VEKTRA_ENV` | str | `development` | Environment mode. In `production`, non-TLS connections are rejected. |
| `VEKTRA_SAFEGUARD_MODE` | str | `passthrough` | Safeguard implementation: `passthrough`, `presidio`, `guardrails-ai` |
| `VEKTRA_MULTI_TENANT` | bool | `false` | Activate PostgreSQL RLS binding. Phase 1 uses application-level namespace filtering. |
| `VEKTRA_CONVERSATION_KEY` | str | - | Symmetric encryption key for conversation content |
| `VEKTRA_PII_CHUNK_THRESHOLD` | int | `3` | PII entity count threshold per chunk for post_retrieval filtering. Presidio mode only. |

## Webhooks

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_WEBHOOK_URL` | str | - | Webhook endpoint URL for event delivery. Disabled when unset. |
| `VEKTRA_WEBHOOK_SECRET` | str | - | HMAC-SHA256 signing secret for webhook payloads |
| `VEKTRA_WEBHOOK_TIMEOUT` | float | `5.0` | HTTP timeout in seconds for webhook delivery |

## E-learning (vektra-learn)

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_LEARN_JWT_SECRET` | str | - | JWT signing secret for dashboard tokens. Required when vektra-learn is active. Min 32 characters. |
| `VEKTRA_LEARN_REQUIRE_ENROLLMENT` | bool | `true` | Require enrollment record for learn queries. Set to false when LMS manages enrollment externally. |

## Observability

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_MAX_CONVERSATION_TURNS` | int | `10` | Maximum conversation history turns retained per conversation |
| `VEKTRA_AUDIT_RETENTION_DAYS` | int | `90` | Audit log retention period in days |
| `VEKTRA_RETENTION_DAYS` | int | - | Soft-deleted record retention period (cleanup job) |
| `VEKTRA_ANALYTICS_RETENTION_DAYS` | int | - | QueryTrace storage retention days |
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
| VEKTRA_* variables (ARCH-060 registry) | 48 |
| VEKTRA_* infrastructure (`VEKTRA_CORS_ORIGINS`, `VEKTRA_PORT`) | 2 |
| External API keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) | 2 |
| Infrastructure (`POSTGRES_PASSWORD`, `CMD_TARGET`, service ports) | 6 |
| **Total documented** | **58** |

The ARCH-060 registry covers the 48 VEKTRA_* variables managed by `VektraSettings`. Infrastructure variables (`POSTGRES_PASSWORD`, `CMD_TARGET`, `VEKTRA_CORS_ORIGINS`, service ports) are used by Docker Compose or the application factory and are listed here for completeness.
