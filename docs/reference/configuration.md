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
| `VEKTRA_LLM_EXTRA_BODY` | json | - | Extra JSON body passed to litellm (e.g. `{"chat_template_kwargs": {"enable_thinking": false}}` for vLLM thinking models) |
| `VEKTRA_LLM_FALLBACK_MODEL` | str | - | Fallback model when the primary times out |
| `VEKTRA_LLM_FALLBACK_TIMEOUT_MS` | int | `60000` | Milliseconds before switching to fallback model |
| `VEKTRA_LLM_CONTEXT_WINDOW` | int | - | Context window size in tokens. Required for models not in litellm's registry (e.g. local vLLM). If unset, litellm lookup with 4096 fallback (warns on fallback) |
| `VEKTRA_LLM_CONTEXT_ONLY_ENABLED` | bool | `true` | Return raw chunks without LLM synthesis when both models fail |
| `VEKTRA_STARTUP_LLM_CHECK` | bool | `true` | Test LLM connectivity at startup (warning-only, not fatal) |

### LLM provider keys

Use `VEKTRA_LLM_API_KEY` for any provider. It is passed directly to litellm and works with OpenAI, Anthropic, vLLM, and any OpenAI-compatible endpoint.

Provider-specific environment variables (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) are also recognized by litellm as a fallback, but `VEKTRA_LLM_API_KEY` takes precedence when set.

### Using a local vLLM instance

```bash
VEKTRA_LLM_PROVIDER=openai//models/your-model-name
VEKTRA_LLM_API_KEY=<vllm-api-key>
VEKTRA_LLM_API_BASE=http://<vllm-host>:8000/v1
VEKTRA_LLM_CONTEXT_WINDOW=32768
# For models with thinking mode (e.g. Qwen3.5, DeepSeek-R1), disable it:
VEKTRA_LLM_EXTRA_BODY={"chat_template_kwargs": {"enable_thinking": false}}
```

In `.env` files (Docker Compose), JSON values do not need quoting. In shell, use single quotes: `VEKTRA_LLM_EXTRA_BODY='{"chat_template_kwargs": {"enable_thinking": false}}'`.

The model name must match the vLLM `--model` path exactly (e.g., `/models/qwen35-27b`).

## Embedding

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_EMBEDDING_PROVIDER` | str | `sentence-transformers` | Embedding provider implementation: `sentence-transformers` (in-process), `tei` (remote) |
| `VEKTRA_EMBEDDING_MODEL` | str | `paraphrase-multilingual-MiniLM-L12-v2` | Model name within the selected provider (`sentence-transformers` only; a TEI instance serves one fixed model) |
| `VEKTRA_TEI_URL` | str | `http://localhost:8080` | TEI server base URL (native API, no `/v1` suffix). Used when provider is `tei`. The Qdrant collection is sized from the served model's dimensions at startup |
| `VEKTRA_TEI_API_KEY` | str | - | Bearer token for the TEI embedding server (`--api-key`). Optional |
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
| `VEKTRA_MIN_RELEVANCE_SCORE` | float | `0.15` | Minimum relevance score for chunk inclusion (0.0-1.0). Safety net filter; top-k is the primary control. |
| `VEKTRA_CHUNK_DEDUP_ENABLED` | bool | `true` | Deduplicate overlapping adjacent chunks from the same document |
| `VEKTRA_RETRIEVAL_RESCUE_TOP_K` | int | `0` | When the `VEKTRA_MIN_RELEVANCE_SCORE` filter empties the candidate set, keep this many top-scored chunks above the rescue floor instead of refusing. Multi-part and comparative questions get uniformly low reranker scores (each chunk answers only one part), so with the rescue the LLM arbitrates via grounding instead of the query dying at the filter. `0` disables the rescue. Recommended starting point when enabling: `3`. |
| `VEKTRA_RETRIEVAL_RESCUE_FLOOR` | float | `0.02` | Absolute minimum score for rescued chunks (0.0-1.0): candidates below this are never rescued. Only used when `VEKTRA_RETRIEVAL_RESCUE_TOP_K` > 0. Lower values rescue more multi-part questions but feed more irrelevant context to adversarial ones. |
| `VEKTRA_PARENT_EXPANSION_ENABLED` | bool | `false` | Replace retrieved child chunks with their parent chunk text before prompt construction (advanced pipeline only). Requires documents ingested with `VEKTRA_CHUNKING_STRATEGY=dual`. |
| `VEKTRA_RESPONSE_TOKEN_RESERVE` | int | `2048` | Tokens reserved for LLM response generation |
| `VEKTRA_CONTEXT_CHUNK_RATIO` | float | `0.6` | Fraction of context window allocated to retrieved chunks (0.0-1.0) |
| `VEKTRA_PROMPT_TEMPLATES_DIR` | str | - | Directory for custom Jinja2 prompt templates (`system.j2`, `context.j2`, `conversation.j2`). Uses built-in defaults if unset. |
| `VEKTRA_PROMPT_GROUNDING_MODE` | str | `strict` | Default RAG grounding policy. `strict` answers from retrieved context + history only; `hybrid` falls back to model knowledge when confident. Per-namespace override via `PATCH /api/v1/admin/namespaces/{id}/config`. |
| `VEKTRA_DEBUG_LOG_QUERIES` | bool | `false` | Log original and rewritten query text at debug level. Development only. |

### Query rewriting

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_QUERY_REWRITE_ENABLED` | bool | `true` | Enable conversational query rewriting in AdvancedQueryPipeline |
| `VEKTRA_QUERY_REWRITE_MODEL` | str | - | Override LLM model for the rewrite step. Falls back to primary model. |

### Reranking

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_RERANK_ENABLED` | bool | `true` | Enable cross-encoder reranking after retrieval |
| `VEKTRA_RERANK_PROVIDER` | str | `cross-encoder` | Reranking provider: `flashrank`, `cross-encoder`, `cohere`, `tei` (remote) |
| `VEKTRA_RERANK_MODEL` | str | `BAAI/bge-reranker-v2-m3` | Multilingual reranking model. For English-only lightweight deployments: provider=`flashrank`, model=`ms-marco-MiniLM-L-12-v2` |
| `VEKTRA_RERANK_FETCH_K` | int | `20` | Candidates fetched from vector search for reranking. The post-rerank cut is the request's `top_k` |
| `VEKTRA_RERANK_API_KEY` | str | - | API key for API-based providers (`cohere`) |
| `VEKTRA_RERANK_TEI_URL` | str | `http://localhost:8080` | TEI reranker server base URL (one TEI instance per model, e.g. serving `BAAI/bge-reranker-v2-m3`). Used when provider is `tei`. Scores are sigmoid-normalized like the in-process path |
| `VEKTRA_RERANK_TEI_API_KEY` | str | - | Bearer token for the TEI reranker server. Optional |

## Ingestion

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_CHUNKING_STRATEGY` | str | `fixed` | Chunking strategy: `fixed`, `dual` |
| `VEKTRA_CHUNK_SIZE` | int | `500` | Token count per chunk |
| `VEKTRA_CHUNK_OVERLAP` | int | `100` | Token overlap between adjacent chunks |
| `VEKTRA_MAX_FILE_SIZE_MB` | int | `50` | Maximum file size for ingestion (megabytes) |
| `VEKTRA_DOCUMENT_EXTRACTOR` | str | `pdfplumber` | Extractor implementation: `pdfplumber`, `unstructured` |
| `VEKTRA_TABLE_SPLIT` | bool | `false` | Allow splitting table elements across chunks. Dual-strategy only. |

## Security

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_ADMIN_BOOTSTRAP_KEY` | str | - | Single-use credential for creating the first API key. Consumed after first use. |
| `VEKTRA_ENV` | str | `development` | Environment mode. In `production`, non-TLS connections are rejected. |
| `VEKTRA_SAFEGUARD_MODE` | str | `passthrough` | Safeguard implementation: `passthrough`, `presidio`, `guardrails-ai` |
| `VEKTRA_MULTI_TENANT` | bool | `false` | Activate PostgreSQL RLS binding for namespace isolation |
| `VEKTRA_CONVERSATION_KEY` | str | - | Symmetric encryption key for conversation content. **Unset means conversations are not stored**: the app falls back to an in-memory store, so history dies with the process and the student-facing history endpoints return `ERR-LEARN-001`. See [Conversation history](#conversation-history). |
| `VEKTRA_PII_CHUNK_THRESHOLD` | int | `3` | PII entity count threshold per chunk for post_retrieval filtering. Presidio mode only. |

### Conversation history

`VEKTRA_CONVERSATION_KEY` is what selects the persistent conversation store. With it set, questions and answers are written to Postgres encrypted with pgcrypto (`pgp_sym_encrypt`, [ADR-0011](../../.s2s/decisions/ADR-0011-conversation-encryption.md)) and a student's history survives a closed tab, a reboot and a different device. With it unset, the app registers an in-memory store: nothing is persisted, and `GET /api/v1/learn/conversations` and the turns endpoint answer `ERR-LEARN-001` rather than pretending the student has no history.

```bash
# 32 bytes of entropy is plenty; anything shorter is the weak link in the chain
VEKTRA_CONVERSATION_KEY=$(openssl rand -base64 32)
```

Three things to know before turning it on:

- **The key cannot be rotated in place.** Existing turns are decryptable only with the key that encrypted them; changing it makes previous history unreadable (the rows stay, the decrypt fails). Re-encryption would need a migration that reads every turn with the old key — there is no such tooling today.
- **Enabling it starts storing personal data.** Turns are pseudonymous (`owner_subject` holds the LMS user id the token was issued for) and encrypted at rest, but they are still personal data. Set `VEKTRA_RETENTION_DAYS` in the same change, and make sure the deployment's privacy notice covers it.
- **Losing the key loses the history**, by design. Back it up wherever the deployment keeps its secrets, not in the database it protects.

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
| `VEKTRA_LEARN_SHOW_SOURCES` | bool | `true` | Default visibility of the source-citations section in the widget (FEAT-014). Per-namespace override via `PATCH /api/v1/admin/namespaces/{id}/config` > `show_sources`. Per-widget override via the `data-show-sources` script-tag attribute. The API always returns the full sources list; the flag only instructs the widget whether to render them. |

## Assessment discussion (vektra-assess)

Dormant by default. With `VEKTRA_ASSESS_ENABLED=false` no `/api/v1/assess` route is mounted, and the
`assess-worker` / `assess-parser` entrypoints exit with code 2. Enable it with the Compose overlay:
`make assess-keys`, then `docker compose -f docker-compose.yml -f docker-compose.assess.yml --profile assess up -d`.
A production worker refuses to start while any job handler is unbound. One still is: the `rotate_keys` job kind
(owned by plan assess-hardening-release, never enqueued; key rotation is the `python -m vektra_assess.keys rewrap` command),
so today only development and CI run the worker, with `-f docker-compose.assess.dev.yml`, which sets
`VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND` and `VEKTRA_ENV=development` (see the [deployment guide](../guides/operators/assess-deployment.md#current-release-blocker)).
These variables are read only by the assess code (`vektra_assess.config.VektraAssessSettings`), from the
process environment, never from an env file. Every numeric setting has a lower and an upper
bound; a value outside its range (or any invalid value) stops the API at startup step
`assess_check`, and the worker before it touches the database, with a `StartupValidationError`
that names each rejected variable and the rule it broke. The rejected value itself is never
printed, because some settings carry connection strings.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_ASSESS_ENABLED` | bool | `false` | Master switch (see above). |
| `VEKTRA_ASSESS_API_DATABASE_URL` | str | - | DSN of the `vektra_assess_api` role, **without a password** (read from the `assess_db_api_password` secret). A DSN with a password, or one that logs in as the `VEKTRA_DATABASE_URL` user, is refused at startup. |
| `VEKTRA_ASSESS_WORKER_DATABASE_URL` | str | - | DSN of the `vektra_assess_worker` role, without a password (`assess_db_worker_password` secret). Worker only. |
| `VEKTRA_ASSESS_SECRETS_DIR` | path | `/run/secrets` | Directory holding the assess secret files. |
| `VEKTRA_ASSESS_API_POOL_SIZE` | int | `15` | Connection pool of the assess API engine. |
| `VEKTRA_ASSESS_WORKER_POOL_SIZE` | int | `5` | Connection pool of the worker engine. |
| `VEKTRA_ASSESS_HEARTBEAT_MAX_AGE_S` | int | `60` | A worker heartbeat older than this makes `/health/assess` non-healthy. Range `30`..`600` (at least three beat intervals). |
| `VEKTRA_ASSESS_MOODLE_CA_FILE` | path | - | CA bundle for verifying the Moodle TLS certificate (on-prem CA); the system store when unset. |
| `VEKTRA_ASSESS_WS_TIMEOUT_CONNECT_S` | float | `3` | Connect timeout of one Moodle web-service call. |
| `VEKTRA_ASSESS_WS_TIMEOUT_READ_S` | float | `10` | Read timeout of one Moodle web-service call. |
| `VEKTRA_ASSESS_WS_TOTAL_TIMEOUT_S` | float | `30` | Overall deadline of one Moodle call including the body; a server dripping bytes under the byte cap cannot hold it open (`Unreadable(timeout)`). |
| `VEKTRA_ASSESS_WS_MAX_RESPONSE_BYTES` | int | `8388608` | Byte cap of a JSON response; the stream is aborted on overflow (`Unreadable(size_cap)`). Range `1024`..`67108864`. |
| `VEKTRA_ASSESS_WS_MAX_FILE_BYTES` | int | `10485760` | Byte cap of one downloaded submission file (REQ-101). Range `1024`..`67108864`. |
| `VEKTRA_ASSESS_WS_RPS` | float | `10` | Cap on Moodle calls per second, shared by every caller in the worker **process** (one replica, COMP-003: running several worker replicas multiplies it). |
| `VEKTRA_ASSESS_BREAKER_THRESHOLD` | int | `5` | Consecutive transport/5xx failures that open the per-site circuit breaker (in memory, per worker process). Range `1`..`100`. |
| `VEKTRA_ASSESS_BREAKER_COOLDOWN_S` | float | `30` | Seconds the breaker stays open before one half-open probe. Range `>0`..`3600`. |
| `VEKTRA_ASSESS_MOODLE_ALLOW_HTTP` | bool | `false` | Plain http to Moodle. Honoured only with `VEKTRA_ENV=development`; the worker refuses to start with it set in any other environment. |
| `VEKTRA_ASSESS_MOODLE_ALLOWED_CIDRS` | str | - | Comma-separated CIDRs a Moodle host may resolve into (an on-prem Moodle on a private network, e.g. `10.20.0.0/16`). Empty: private addresses are refused. Metadata, link-local and multicast addresses are always refused. |
| `VEKTRA_ASSESS_INGEST_ACCOUNT_USERNAME` | str | - | Username of the ingestion web-service account. The setup check refuses an assess token that belongs to it; when unset that item is `UNVERIFIABLE` (not OK). |
| `VEKTRA_ASSESS_UI_ORIGIN` | str | - | Origin (`https://host[:port]`) the chat UI is served from. The setup check item `ui_not_same_origin` refuses a UI origin that is the same host or the same site as the Moodle `base_url` or as the API public origin (CONF-004). Same-site is judged without the public-suffix list: the last two host labels are compared, which can refuse a legitimate pair under a multi-label suffix (e.g. two `*.co.uk` hosts) but never accepts a same-site pair. Unset: that item is `UNVERIFIABLE` (not OK). The value must be a bare origin (no path, query, credentials or wildcard; it is normalised: lower case, no default port); malformed, it is refused while assess is enabled and only warned about while it is disabled. With assess enabled the API refuses to start when it is unset outside development, not `https` outside development, or equal to a registered site's Moodle origin (plan 9, ARCH-076: the iframe's `allow-same-origin` sandbox is safe only across distinct origins). The discussion UI page `GET /api/v1/assess/ui` answers on this origin only: unset, or requested through any other scheme and `Host`, it is 503. Behind a TLS-terminating proxy the scheme comes from `X-Forwarded-Proto` only when the proxy's address is in uvicorn's `FORWARDED_ALLOW_IPS` (default `127.0.0.1`; set it to the proxy's address when the proxy runs in another container), and the proxy must forward the client's `Host` (the nginx example does). |
| `VEKTRA_ASSESS_API_PUBLIC_ORIGIN` | str | - | Public origin of the assess API, compared with the UI origin by `ui_not_same_origin`. Unset while the UI origin is set: the item is `UNVERIFIABLE`. |
| `VEKTRA_ASSESS_WORKER_HEARTBEAT_FILE` | path | `/tmp/vektra-assess-worker.heartbeat` | File the worker refreshes every `VEKTRA_ASSESS_WORKER_HEARTBEAT_INTERVAL_S` (container healthcheck). |
| `VEKTRA_ASSESS_WORKER_HEARTBEAT_INTERVAL_S` | float | `5` | Seconds between worker heartbeats (row upsert and file touch). At most 10, so `VEKTRA_ASSESS_HEARTBEAT_MAX_AGE_S` (minimum 30) always spans at least three beats. |
| `VEKTRA_ASSESS_JOB_POLL_INTERVAL_S` | float | `1` | Claim-loop poll when no `NOTIFY` arrives. The poll is the correctness backstop; `NOTIFY` is only a latency hint. |
| `VEKTRA_ASSESS_JOB_BACKOFF_BASE_S` / `VEKTRA_ASSESS_JOB_BACKOFF_CAP_S` | float | `5` / `300` | Retry backoff of a failed job attempt: exponential from the base, jittered to 50-100 % of the step, capped. |
| `VEKTRA_ASSESS_JOB_DONE_RETENTION_H` | int | `24` | Hours a `done` job row is kept before the hourly cleanup deletes it. `dead` rows are kept for the purge plan. Range `1`..`8760`. |
| `VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND` | bool | `false` | Development and CI only: start the worker while some job kinds or ticks have no handler yet. Unbound slots stay unbound and a job of that kind fails with `handler_not_bound`, never completing silently. Honoured only with `VEKTRA_ENV=development`; set in any other environment the worker refuses to start. The shipped overlay does **not** set it; the dev-only `docker-compose.assess.dev.yml` does. Default: a worker with an unbound slot refuses to start and names each slot and the plan that owns it. |
| `VEKTRA_ASSESS_WORKER_DB_COMMAND_TIMEOUT_S` | float | `30` | Upper bound on any single worker database statement (pool). A restart or a dropped network can leave a connection half-open: no error, only silence; without a bound the worker would wait on it. The control connection uses a fixed 5 s bound. |
| `VEKTRA_ASSESS_WORKER_WEDGE_EXIT_S` | float | `60` | Liveness watchdog (10-600): if the heartbeat row has not been written for this long while the worker should be ready, it exits with code 70 and the supervisor (`restart: unless-stopped`) starts a fresh process, which reconciles on startup. Backstop for any wedge the timeouts do not catch. |
| `VEKTRA_ASSESS_WORKER_DRAIN_S` | float | `20` | On SIGTERM, seconds to wait for in-flight handlers before cancelling them and releasing their leases. At most 25, under the Compose `stop_grace_period` of 30 s. |
| `VEKTRA_ASSESS_POOL_<POOL>_CONCURRENCY` | int | `preflight` 10, `ws` 2, `transcript` 2, `delivery` 2, `setup` 1, `maintenance` 1 | Concurrent jobs of a pool. `VEKTRA_ASSESS_PREFLIGHT_CONCURRENCY` is an accepted alias for the pre-flight pool (the NFR-020 admission number K; 10 is a placeholder until the reference hardware is decided). |
| `VEKTRA_ASSESS_JOB_<KIND>_BUDGET_S` / `_LEASE_S` / `_MAX_ATTEMPTS` | float / float / int | per kind (for example `ws` kinds 20 / 40 / 5, `setup_check` 120 / 240 / 5, `preflight` derived from `VEKTRA_ASSESS_START_T_S` = T: budget T − 15 s, lease max(2 × budget, budget + 30 s), 2 attempts, so 45 / 90 / 2 at the default T of 60; an override must keep budget ≤ T − 5 s and lease ≥ budget + 30 s, or the worker refuses to start. A pre-flight cancelled at its budget is rolled back at once (`start_timeout`)) | Hard timeout, lease and attempts of a job kind, e.g. `VEKTRA_ASSESS_JOB_PURGE_BUDGET_S`. The lease must be at least twice the budget (no renewal exists); setting only the budget sets the lease to twice it. `_MAX_ATTEMPTS` can **lower** the attempts a job row allows (the row's own `max_attempts`, default 5, is the ceiling: an enqueuer that wants more passes it); the effective value is written back on claim. The write-back is one-way: raising the setting again does not restore the attempts of rows already claimed under the lower value. Lowering it does not retroactively kill rows that are already `ready` at or above the new limit; such a row is no longer claimed and stays `ready` until an operator deletes or re-enqueues it. A wrong name, a non-number, a value above the ceiling (10000 for attempts and concurrency, 86400 s for budget and lease) or a lease under that rule makes the worker refuse to start. |
| `VEKTRA_ASSESS_PARSER_SOCKET` | path | `/run/vektra-assess-parser/parser.sock` | Unix socket of the parser sidecar (worker and parser only). |
| `VEKTRA_ASSESS_PARSER_POOL` | int | `3` | Concurrent child processes of the parser sidecar (one item per fresh process). Sized with the container `mem_limit` (768M): pool 3 within the measured per-child peak. |
| `VEKTRA_ASSESS_PARSER_TIMEOUT_S` | float | `10` | Wall-clock budget per file in the sidecar; the child is SIGKILLed at the deadline and the file becomes `non letto: file danneggiato o non elaborabile`. The worker client waits this plus a 15 s queue allowance before treating the sidecar as unavailable. |
| `VEKTRA_ASSESS_PARSER_MAX_CHARS` | int | `200000` | Character cap of the text returned per file; beyond it the file is `letto parzialmente`. |
| `VEKTRA_ASSESS_PARSER_MAX_FILE_BYTES` | int | `10485760` | Largest file the sidecar accepts (REQ-101: 10 MB); larger is `non letto: troppo grande` with no child forked. The sidecar also holds at most four such payloads in memory at once and answers `BUSY` beyond that (the client retries). |
| `VEKTRA_ASSESS_LLM_MODEL` | str | - | Dedicated discussion model (litellm id). Required when assess is enabled. No fallback model is ever used: `VEKTRA_LLM_FALLBACK_MODEL` is ignored for assess, because a fallback would answer students with a model that never passed the NFR-023 activation. |
| `VEKTRA_ASSESS_LLM_API_BASE` | str | - | API base of the discussion model (Ollama or OpenAI-compatible endpoint). A cloud key is read by litellm from its own provider variable. |
| `VEKTRA_LLM_API_KEY` (assess use) | secret str | - | The discussion model's endpoint key is the SAME variable the RAG pipeline uses (OQ-026 decision of 2026-11-06: one key for the on-prem vLLM, no separate assess secret). The API reads it from `.env`; the worker, which has no `env_file`, gets it through its `environment` in `docker-compose.assess.yml`. Empty means no key. Never logged (SecretStr). |
| `VEKTRA_ASSESS_LLM_EXTRA_BODY` | JSON object | - | Passed as `extra_body` on every discussion-model call, in the API and in the worker (Part B). For Qwen3 on vLLM: `{"chat_template_kwargs": {"enable_thinking": false}}`. Empty or unset: none. The RAG's `VEKTRA_LLM_EXTRA_BODY` is never used for assess. Invalid JSON or a non-object refuses startup. |
| `VEKTRA_ASSESS_LLM_TEMPERATURE` | float | `0.2` | Sampling temperature of the discussion model (REQ-100 determinism). |
| `VEKTRA_ASSESS_LLM_MAX_TOKENS` | int | `600` | Completion tokens per reply, reserved out of the context window before history is kept. |
| `VEKTRA_ASSESS_LLM_CONTEXT_WINDOW` | int | `8192` | Context window of the model. History is trimmed oldest first; the context bundle is never trimmed (a bundle that does not fit is an error). Range `2048`..`1048576`. |
| `VEKTRA_ASSESS_LLM_SEED` | int | - | Passed to providers that support a seed. Range `0`..`2147483647`. |
| `VEKTRA_ASSESS_OUTAGE_TIMEOUT_S` | float | `45` | No first token within this time is a provider outage (REQ-086); a slow stream that keeps producing is not. |
| `VEKTRA_ASSESS_OUTAGE_G_S` | int | `20` | REQ-086 G. An outage moves the session to OUTAGE (timer frozen, input locked); the worker probes the provider every 5-10 s with one `max_tokens=1` call while any session is in OUTAGE. At the first success the outage ends: an interval of at least G seconds is credited in full, a shorter one earns nothing, and the cumulative credit never exceeds C = min(15 min, half the session duration). Cumulative outage above C, or a third consecutive failed call, closes the session `service_failure`. |
| `VEKTRA_ASSESS_PRIVACY_NOTICE_URL` | url | unset | URL of the privacy notice, returned to the plugin in `GET /state` `pre_session.privacy.notice_url` (with `VEKTRA_ASSESS_PRIVACY_NOTICE_VERSION`). |
| `VEKTRA_ASSESS_RETRY_RPM` | int | `6` | `POST /sessions/{sid}/retry` calls per minute per session; above it 429 (`retry_rate`). The retry only makes the API try the pending turn now; it never ends an OUTAGE or changes the state. |
| `VEKTRA_ASSESS_UI_PAGE_RPM` | int | `60` | Plan 9: `GET /api/v1/assess/ui` (the public discussion page) per minute per client address; above it 429 with `Retry-After`, before any database read. A site's framing origin is cached 30 s per API process, so a repeated request costs no database connection and a change of `moodle_origin` or a disabled site is honoured within 30 s. |
| `VEKTRA_ASSESS_FLAG_RPM` | int | `10` | REQ-103: `POST /sessions/{sid}/turns/{turn_no}/flag` calls per minute per session; above it 429 `flag_rate`. |
| `VEKTRA_ASSESS_MESSAGES_PER_MIN` | int | `4` | REQ-083: student messages per minute per session; above it 429 `ERR-ASSESS-012` ("Attendi qualche secondo"), before any model call, deadline unchanged. Sized in plan 8 M5.1 (see below). |
| `VEKTRA_ASSESS_SESSION_TOKEN_BUDGET` | int | `500000` | REQ-083: estimated tokens (prompt + completion, from `TurnUsage`) one session may use; `budget_warning` at 80%, close `budget_exhausted` at 100% (one transcript job). Outage retries and earlier sessions do not count. Sized in plan 8 M5.1 (see below). |
| `VEKTRA_ASSESS_START_RATE_PER_HOUR` | int | `5` | REQ-071: Start attempts per (student, activity) per hour; the next is refused 429 `start_rate_cap` with `retry_after` ("Riprova tra X minuti"), nothing consumed, audited `assess_start_rate_cap_hit`. |
| `VEKTRA_ASSESS_ROLLBACK_NOTIFY_AFTER` | int | `3` | REQ-071: this many consecutive SYSTEM rollbacks (start_timeout, capture_failed, part_b_rejected, provider_unavailable, internal_error, commit_failed) of one (student, activity) enqueue `teacher_notify` and `operator_notify` once (audited `assess_rollback_cap_hit`); the student reads `technical_problem_persistent` ("Problema tecnico persistente") while the streak lasts. Worker and API. |
| `VEKTRA_ASSESS_START_BREAKER_ROLLBACKS` | int | `10` | Per-site Start breaker: this many system rollbacks of one site within the window open it (worker-written `assess_start_breaker`, audited `assess_start_breaker_open`); the API then refuses Starts 503 `start_breaker_open` without consuming anything. |
| `VEKTRA_ASSESS_START_BREAKER_WINDOW_S` | int | `300` | The breaker's counting window. |
| `VEKTRA_ASSESS_START_BREAKER_COOLDOWN_S` | int | `120` | Seconds the breaker stays fully open; then half-open (one Start at a time goes through). A commit closes it (`assess_start_breaker_close`); another system rollback re-opens it. |
| `VEKTRA_ASSESS_LLM_PRICE_PER_1K_IN` / `VEKTRA_ASSESS_LLM_PRICE_PER_1K_OUT` | float | - | Optional price table for the per-turn cost estimate; unset (on-prem) leaves the cost empty, not 0. Range `0`..`1000`. |
| `VEKTRA_ASSESS_SAFEGUARD_MODE` | `presidio` \| `none` | `presidio` | Safeguard of the discussion runner in the API. With `presidio`, startup fails if Presidio cannot load (no silent passthrough); `none` must be set explicitly. The worker's Part B runner always uses an explicit passthrough (its import ceiling excludes Presidio; Part B has no student input). |
| `VEKTRA_ASSESS_STORE_TRACES` | bool | `true` | Store the runner's content-free QueryTrace (pipeline and namespace `assess-discussion`). |
| `VEKTRA_ASSESS_PROVIDER_LOCALITY` | `onprem` \| `cloud` | `cloud` | Where the model runs. With `cloud` (the safe default) the eval suite refuses any dataset that is not synthetic before a single request. |
| `VEKTRA_ASSESS_LLM_ALLOW_UNACTIVATED` | bool | `false` | Development and CI only: serve discussions while the (model, prompt_version) has no passing NFR-023 activation (without it the API starts degraded and refuses discussions). Honoured only with `VEKTRA_ENV=development`; refused at startup otherwise. |
| `VEKTRA_ASSESS_TOKEN_ISSUER` | str | `vektra-assess` | `iss` claim of every discussion token; `aud` is always `discussion` (ARCH-067). |
| `VEKTRA_ASSESS_PRESESSION_TTL_S` | int | `120` | Lifetime of a pre-session token minted by `POST /tokens`, 10-120 s; a value above 120 refuses startup. |
| `VEKTRA_ASSESS_SESSION_TOKEN_TTL_S` | int | `900` | Upper bound of a session-bound token's lifetime; it also never outlives the session deadline plus `VEKTRA_ASSESS_REPLY_GRACE_S`. Resume (`GET /sessions/{sid}` with a fresh pre-session token) re-mints it. |
| `VEKTRA_ASSESS_REPLY_GRACE_S` | int | `60` | Seconds after the deadline an in-flight reply may still finish (REQ-070); session tokens expire at deadline plus this grace at the latest. |
| `VEKTRA_ASSESS_MIN_PLUGIN_VERSION` | str | `0.1.0` | Lowest `local_vektradiscuss` release accepted at mint (ARCH-067). A site's own `assess_sites.min_plugin_version` applies when it is higher. |
| `VEKTRA_ASSESS_START_T_S` | int | `60` | REQ-071 commit window T: a Start not committed within T of the request is rolled back (pre-flight guard, budget cancellation and STARTING reaper). The pre-flight job's budget and lease are derived from it. The worker reads it too: `docker-compose.assess.yml` passes the `.env` value (default 60) to the worker's environment, as it does for `VEKTRA_ASSESS_ADMISSION_K`, `VEKTRA_ASSESS_RENDER_FRESH_S` and the model parameters. |
| `VEKTRA_ASSESS_ADMISSION_K` | int | `10` | NFR-020 admission limit: live sessions (ACTIVE, OUTAGE, and STARTING sessions already in pre-flight) admitted at once. The API refuses a Start at or over K before writing anything; the worker pre-flight re-checks under a lock and is authoritative. 10 until the burst test sets it with evidence. |
| `VEKTRA_ASSESS_PRIVACY_NOTICE_VERSION` | str | `1` | Version of the privacy notice shown before Start, recorded on the session row and on the `start` audit event. |
| `VEKTRA_ASSESS_ENROLMENT_FRESHNESS_S` | int | `150` | Gate step 6: a student turn needs an `enrolled` re-check no older than this (database clock). The worker re-checks every live session every 60 s (`enrolment_recheck` jobs, WS pool); a student no longer enrolled is closed `access_revoked`; an unreadable enrolment or a worker stopped longer than this rejects turns with 503 (fail closed, nothing closed). |
| `VEKTRA_ASSESS_MAX_MESSAGE_CHARS` | int | `2000` | Longest student message accepted (REQ-083). A longer message, or one made only of whitespace, is refused with 400 `ERR-ASSESS-014` (`details.reason` `message_too_long` / `message_empty`) before any model call; the deadline is unchanged. Sized in plan 8 M5.1 (see below). |
| `VEKTRA_ASSESS_RENDER_FRESH_S` | int | `300` | Age under which a worker-computed button state is fresh for `GET /state`. Older or missing: `/state` answers `unavailable` (reason `checking`) and queues one render check per student and activity. A Start is refused before writing anything only on a FRESH negative state of a Moodle-side gate; otherwise the pre-flight decides (authoritative). The gate-refresh tick re-queues stale pairs asked about in the last day. |
| `VEKTRA_ASSESS_STATE_RPM` | int | `30` | `GET /state` calls per minute per site, student and activity (a pre-session token changes on every page view, so it is not the key). Assess-local limiter whose empty windows the housekeeping sweep removes; the global per-key limiter stays dormant. |
| `VEKTRA_ASSESS_SITE_TIMEZONE` | str | `Europe/Rome` | IANA zone of the transcript's timestamps and of the date in its file name (BR-016: the date of the close in the site time zone). An unknown zone is refused at startup. |
| `VEKTRA_ASSESS_DOWNLOAD_TOKEN_TTL_S` | int | `60` | Lifetime of a transcript download token (`aud=transcript`, single use), 5-120 s (ARCH-067). The plugin mints it server-side right before the browser fetches the file. |
| `VEKTRA_ASSESS_DOWNLOAD_RPM` | int | `10` | Transcript operations per minute per transcript and requester, download-token mints and downloads drawing on ONE budget (a complete download costs 2; minimum 2); above it 429 `download_rate`. Range `2`..`600`. |
| `VEKTRA_ASSESS_RESEND_MIN_INTERVAL_S` | int | `600` | Minimum time between two delivery rounds of the same transcript (generation's round 1 included); each round mails up to 3 recipients. Sooner: 429 `resend_interval` with `Retry-After`. Checked by `assess_enqueue_resend` under the per-transcript lock. Range `0`..`86400`. |
| `VEKTRA_ASSESS_RESEND_RPM` | int | `6` | Resend calls per minute per API key (assess-local limiter); above it 429 `resend_rate`. Range `1`..`600`. |
| `VEKTRA_ASSESS_TEACHER_GATE_MAX_AGE_S` | int | `120` | Freshness bound of the grader list the render worker writes into `assess_gate_inputs`: a teacher's download-token mint, download or resend is checked against it. Older or absent: 403 `ERR-ASSESS-021` and a refresh is queued, so a retry seconds later succeeds (fail closed). The same bound applies to the activity facts (editors, graders, close date, team submission, grading method, override facts) the `teacher_gate` job writes into `assess_teacher_gate` for the configuration routes and the teacher operations (session list, reset, terminate, extra time, opening fallback), and to the student's render evaluation a reset re-checks (older: the reset answers `gate_inputs_stale` and queues a render check). Range `10`..`3600`. |
| `VEKTRA_ASSESS_RAG_ENABLED` | bool | `false` | Discussion retrieval from course material (ARCH-072). **Not available in this release**: `true` is refused at startup by the API and the worker, and a teacher's RAG ON is the field error `rag_unavailable`. The database makes RAG ON unrepresentable (`CHECK (rag_enabled = false)`). |
| `VEKTRA_ASSESS_RESET_NOTICE_ENABLED` | bool | `false` | REQ-092 student notice of a reset. **Out of v1** (product owner 2026-11-08): `true` is refused at startup by the API and the worker ("not available in this release"), like the RAG flag, so no notice path exists: a reset queues nothing and the teacher list shows `studente non avvisato via email` for every reset. |
| `VEKTRA_ASSESS_SMTP_HOST` | str | - | Worker only. SMTP relay of the transcript emails (REQ-075). Unset: delivery is not bound and the worker refuses to start, unless `VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND=true` (development). The account is NOT a setting: it is the worker-only secret `assess_smtp`, `{"username": "...", "password": "..."}` (or `{}` for a relay that takes no AUTH; one field without the other refuses startup). The API refuses to start when that secret is mounted or named in its environment. |
| `VEKTRA_ASSESS_SMTP_PORT` | int | `587` | Relay port (465 with `implicit`). |
| `VEKTRA_ASSESS_SMTP_TLS_MODE` | str | `starttls` | `starttls`: STARTTLS is required (a relay that does not offer it gets nothing, the row stays `RETRYING` with class `tls` and an alert); `implicit`: TLS from the first byte. There is no plaintext mode; AUTH runs only on an encrypted transport. |
| `VEKTRA_ASSESS_SMTP_FROM` | str | - | From address and envelope sender. Required with the host. |
| `VEKTRA_ASSESS_SMTP_CA_FILE` | path | - | CA bundle that verifies the relay's certificate (an on-prem relay with its own CA; mount it into the worker). Unset: the system store. Certificates and host names are always verified, TLS 1.2 minimum. |
| `VEKTRA_ASSESS_SMTP_MAX_CONNECTIONS` | int | `2` | Concurrent relay sessions per worker (1-20), shared by deliveries and the probe. |
| `VEKTRA_ASSESS_SMTP_TIMEOUT_S` | float | `30` | Hard timeout of one send or one probe (connect, TLS, AUTH, DATA). |
| `VEKTRA_ASSESS_SMTP_RETRY_SCHEDULE` | str | `1m,5m,15m,1h` | Waits after the 1st, 2nd, ... failed attempt (`s`, `m`, `h`); the last one repeats until the window closes. A malformed value refuses startup. |
| `VEKTRA_ASSESS_SMTP_RETRY_WINDOW_HOURS` | int | `72` | Hours a delivery row is retried, from its creation, before it is `FAILED` with an alert (the last attempt runs exactly at the end of the window). A synchronous 5xx is `FAILED` at once (a bounce, REQ-076). |
| `VEKTRA_ASSESS_MESSAGE_ID_DOMAIN` | str | domain of the From | Right-hand side of the deterministic `Message-ID: <sha256(transcript, round, recipient ref)[:32]@domain>`: a resend after a crash between the relay's 250 and the `SENT` commit carries the same id (OQ-029) and is audited `assess_delivery_possible_duplicate`. |
| `VEKTRA_ASSESS_RECIPIENT_DOMAINS` | str | empty | Comma list of the domains a transcript may be mailed to (exact match, no subdomains implied). **Empty refuses every address**: each delivery row is `FAILED` with `domain_not_allowed` and an alert. The teacher enters 1 to 3 addresses in the plugin's activity form (REQ-082 as amended); the plugin validates them against the site admin's copy of this list, the API's configuration PUT refuses an address outside it (field error naming the allowed domains, so the API needs this variable too) and the worker re-checks it at send time. Keep the lists equal. |
| `VEKTRA_ASSESS_OPERATOR_EMAIL` | str | - | Worker and API (the API only checks that it is set, for the SMTP test send). The platform operator's address: the content-free operator alerts (REQ-071: a student's Starts rolled back `VEKTRA_ASSESS_ROLLBACK_NOTIFY_AFTER` times in a row, the LMS unreadable during a Start, at most once per site per 15 minutes) are mailed to it through the same relay, schedule and window as the transcripts. Required with the relay: unset, the `operator_notify` slot stays unbound and the worker refuses to start (unless `VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND=true` in development). Not subject to `VEKTRA_ASSESS_RECIPIENT_DOMAINS`. The teacher notifications (a `service_failure` close, the same rollback streak) go to the activity's configured transcript recipients, under the allowlist. Every attempt and result is audited (`assess_notify_attempt` / `_sent` / `_failed` / `_possible_duplicate`, refs only). It is also the only recipient of the operator's SMTP test send (`POST /api/v1/assess/ops/smtp-test`): set it on the API too, which only checks that it is set (without it the test answers 503 `smtp_relay`). |
| `VEKTRA_ASSESS_PRIVACY_ALLOW_UNSET` | bool | `false` | Development and CI only: let the Start gate BR-006 (h) pass while the REQ-089 privacy configuration does not exist yet (its store arrives with plan assess-privacy-retention). Without it every Start is rolled back with `privacy_not_configured` (fail closed). Honoured only with `VEKTRA_ENV=development`; the worker refuses to start with it set elsewhere. The dev overlay `docker-compose.assess.dev.yml` sets it. |
| `VEKTRA_ASSESS_ERASURE_MODE` | `erase` \| `retain_exam_evidence` | - | API. The DPO's decision on an erasure request (OQ-033, OQ-020): `erase` deletes the subject's discussions (see `DELETE /privacy/subjects/{userid}`); `retain_exam_evidence` keeps them under the site's configured legal basis and answers `outcome=retained`, audited, nothing deleted. Mandatory while assess is enabled outside development: the API refuses to start without it (no implicit choice). |
| `VEKTRA_ASSESS_ERASURE_WAIT_S` | float | `4` | API. How long an erasure request waits for the worker before answering `503 ERR-ASSESS-027` `pending` (0.5-30; the same request again is idempotent). |
| `VEKTRA_ASSESS_PRIVACY_RPM` | int | `30` | API. Privacy export, listing and erasure requests per minute per key; 429 beyond it. |
| `VEKTRA_ASSESS_DB_BACKUP_RETENTION_DAYS` | int | unset | Worker. How long database backups are kept. Crypto-shredding removes a session's wrapped key from the database, not from a backup taken before the purge: the purge covers a backup only once that backup has expired (ARCH-079, amended by plan 12). Unset: a startup warning. |
| `VEKTRA_ASSESS_JUDGE_MODEL` / `VEKTRA_ASSESS_JUDGE_API_BASE` | str | - | Judge model of `make eval-assess` only (never read by the API or the worker). |
| `VEKTRA_ASSESS_JUDGE_LOCALITY` | str | `cloud` | `make eval-assess` only: where the judge runs. A non-synthetic dataset is refused before any request unless it is `onprem`. |
| `VEKTRA_ASSESS_PARSER_MAX_PAGES` | int | `50` | PDF page cap (REQ-101: 50); a caller cannot ask for more. |
| `VEKTRA_ASSESS_PARSER_RLIMIT_AS_MB` | int | `256` | Address-space limit (RLIMIT_AS) of each parsing child, in MiB. A file that needs more dies in its own process, not in the sidecar. |

#### Connection budget (assess)

Postgres `max_connections` is 100. The shipped defaults add up to 65, and a test
(`vektra-app/tests/test_connection_budget.py`) fails above 85, so a changed pool default cannot
silently exhaust the server:

| Holder | Connections | Setting |
|---|---|---|
| Main API pool | 30 (10 + 20 overflow) | `vektra_shared.db` defaults, not changed by assess |
| Assess API pool | 25 (15 + 10 overflow) | `VEKTRA_ASSESS_API_POOL_SIZE` |
| Assess worker pool | 5 (no overflow) | `VEKTRA_ASSESS_WORKER_POOL_SIZE` |
| Assess worker control connection | 1 | fixed (LISTEN and advisory locks, outside the pool) |
| `migrate` (Alembic) | 1 | while it runs |
| Reserved | 3 | Postgres `superuser_reserved_connections` |
| **Total** | **65** | ceiling 85 |

The worker's 5 + 1 is the INT-002 budget. The 5 are split: a job pool of 4 serves the job SQL and
the handlers' transactions, and a liveness engine of 1 serves only the heartbeat, the lease reaper
and the STARTING reaper, so handlers that hold every job connection never delay a beat. With
`VEKTRA_ASSESS_WORKER_POOL_SIZE=1` the pool cannot be split and the worker opens 1 + 1 + 1. INT-002 assumed about 15 for the API; the main API pool alone is larger by default,
which is why the margin is tracked here.

### Secret files and the key matrix

Each secret is a Docker secret file. Keys are JSON keyrings, `{"active": "<kid>", "keys": {"<kid>":
"<base64 of 32 bytes>"}}`: rotation adds a kid and flips `active`. `assess_ws_tokens` and `assess_smtp`
are JSON objects (credentials); the two role passwords are one line of text (at least 24 characters).
Every file must be a regular file (no symlink), owned by the process uid or root, mode at most `0440`.
`make assess-keys` writes a complete generated set to `./secrets/assess/` (mode `0400`, never
overwrites; `secrets/` is gitignored).

Each process checks the matrix at start, in both directions, and refuses to start naming every key that
is missing (required) or present (forbidden). A forbidden key counts as present when its file exists OR
when an environment variable carries its name. The API applies the forbidden column even with assess
disabled (ARCH-066).

| Secret | API | Worker | Parser |
|---|---|---|---|
| `assess_token_signing` | required | forbidden | forbidden |
| `assess_handoff_private` (X25519) | required | forbidden | forbidden |
| `assess_handoff_public` | allowed | required | forbidden |
| `assess_transcript_kek` | required | required | forbidden |
| `assess_teacher_config_public` | required | allowed | forbidden |
| `assess_teacher_config_private` | forbidden | required | forbidden |
| `assess_kek` | forbidden | required | forbidden |
| `assess_ws_tokens` | forbidden | required | forbidden |
| `assess_smtp` | forbidden | required | forbidden |
| `assess_db_api_password` | required | forbidden | forbidden |
| `assess_db_worker_password` | forbidden | required | forbidden |

The one-shot `vektra-assess-db-init` service holds only the two role passwords; it sets `LOGIN` and the
passwords of `vektra_assess_api` and `vektra_assess_worker` (re-run it to rotate a password). The parser
holds no secret, no DSN and has no network. Postgres runs with `log_statement=none` and no parameter
logging (ARCH-078).

## Observability

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VEKTRA_MAX_CONVERSATION_TURNS` | int | `10` | Maximum conversation history turns retained per conversation |
| `VEKTRA_AUDIT_RETENTION_DAYS` | int | `90` | Audit log retention period in days |
| `VEKTRA_RETENTION_DAYS` | int | - | Soft-deleted record retention period (cleanup job) |
| `VEKTRA_ANALYTICS_RETENTION_DAYS` | int | - | QueryTrace storage retention days |
| `VEKTRA_ANALYTICS_STORE_TRACES` | bool | - | Persist QueryTrace to the database. Unset = auto (on in dev, off in prod). |
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
| `CMD_TARGET` | str | `server` | Container entrypoint target: `server` (default), `migrate`, `assess-worker` or `assess-parser`. The assess targets never run migrations. |

## Variable count summary

| Category | Count |
|----------|-------|
| VEKTRA_* variables in `VektraSettings` and sub-configs (Pydantic-validated) | 66 |
| VEKTRA_* variables read directly from env (`VEKTRA_CORS_ORIGINS`) | 1 |
| External API keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) | 2 |
| Infrastructure (`POSTGRES_PASSWORD`, `CMD_TARGET`) | 2 |
| VEKTRA_ASSESS_* variables in `VektraAssessSettings` (assess processes only; also `VEKTRA_ENV` and `VEKTRA_LLM_API_KEY`, counted above) | 101 |
| **Total documented** | **172** |

Not counted: the `VEKTRA_ASSESS_POOL_<POOL>_CONCURRENCY` and `VEKTRA_ASSESS_JOB_<KIND>_*` families and their alias `VEKTRA_ASSESS_PREFLIGHT_CONCURRENCY` (read by the worker's job policy, `vektra_assess.worker.policy`), and `VEKTRA_ASSESS_JUDGE_MODEL`, `VEKTRA_ASSESS_JUDGE_API_BASE` and `VEKTRA_ASSESS_JUDGE_LOCALITY` (read by `make eval-assess` only). `vektra-assess/tests/test_reference_docs.py` fails when a `VektraAssessSettings` variable is missing from this page or from `.env.example`, or when this page names an assess variable the code does not read.

The 66 Pydantic-validated VEKTRA_* variables are declared across `VektraSettings` (flat aggregation) and sub-configs (`RewriteConfig`, `RerankConfig`, `WebhookConfig`, `IngestConfig` extensions). Sub-configs are validated independently at startup, not aggregated into VektraSettings. `VEKTRA_CORS_ORIGINS` is read directly via `os.environ.get` at app startup (see `vektra-app/src/vektra_app/main.py`). Infrastructure variables (`POSTGRES_PASSWORD`, `CMD_TARGET`) are used by Docker Compose or the entrypoint script.

### Assess key purposes and rotation

Every secret is a JSON keyring `{"active": "<kid>", "keys": {"<kid>": "<base64>"}}` (the role passwords are one-line files). Each ciphertext stores the kid it was written under, so rotation adds a kid and flips `active`; older kids keep opening older data until nothing references them.

| Secret | Held by | Protects |
|---|---|---|
| `assess_kek` | worker only | per-session DEKs (`assess_session_keys.kek_wrapped`) |
| `assess_handoff_public` / `assess_handoff_private` | worker seals with the public key, API opens with the private key | the DEK of a LIVE session (`assess_session_handoff.sealed_dek`) |
| `assess_transcript_kek` | API and worker | per-transcript DEKs |
| `assess_teacher_config_public` / `_private` | API seals with the public key, only the worker holds the private key | the solution or pasted rubric: the API can write it, never read it |

Rotation (the full procedure per key, with the two-phase rollout of the keys the API and the
worker share, is the [keys runbook](../guides/assess-keys-runbook.md#key-rotation)):

1. add the new kid to the keyring and make it `active`; deploy;
2. move the rows still under the old kid: `python -m vektra_assess.keys rewrap --what session-keys --from-kid <old>` (`assess_kek`) and `--what transcripts` (`assess_transcript_kek`: the transcript DEKs and the teachers' reasons), run in the worker container until it exits 0. The hand-off rows go away as the live sessions close. `reseal_teacher_configs` has no command yet, so the old teacher-config private kid cannot be retired;
3. remove the old kid from the keyring only after `rewrap` exited 0 and `python -m vektra_assess.keys verify` exits 0 in both containers.

The envelope AAD binds each ciphertext to its row (`snapshot` to the session, `turn` to session and turn number, and so on), so a ciphertext copied to another row fails authentication.

### Sizing of the REQ-083 limits (plan 8 M5.1)

Reference: a scripted 120-minute session (`vektra-assess/tests/test_limits_sizing.py`), one
student message every 2 minutes (60 turns), each message 400 characters (the eval set's
questions are 38-76 characters; 400 stands for an argued answer), replies of about 350
characters, the default model window (8192 tokens, 600 completion), token estimates at
3.5 characters per token, through the real `DiscussionTurnRunner`. It uses about 373,000
tokens: the history fills the window after a few dozen turns, then every turn costs about one
window.

- `VEKTRA_ASSESS_MAX_MESSAGE_CHARS` = 2000: about 25 times the longest eval question; about 570
  tokens, so one message can never crowd the bundle out of the prompt.
- `VEKTRA_ASSESS_MESSAGES_PER_MIN` = 4: twice the pace of the reference session; one reply at a
  time already bounds a real student below it.
- `VEKTRA_ASSESS_SESSION_TOKEN_BUDGET` = 500,000: 1.34 times the reference session, and above
  60 full windows (491,520), so a normal 120-minute session never reaches it. A scripted
  client sending at the rate limit (4 messages a minute, which needs replies faster than 15 s)
  with a full window per turn (about 8,200 tokens) uses about 33,000 tokens a minute and reaches
  it in roughly 15-20 minutes; slower replies or shorter prompts stretch that.

The test fails if a default drifts below these margins. A different model window changes the
reference: re-run the test with the deployment's `VEKTRA_ASSESS_LLM_CONTEXT_WINDOW` before
changing the budget.
