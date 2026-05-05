# vektra-analytics

QueryTrace storage, metrics aggregation, and reporting API for the Vektra platform.

Phase 2 component (ADR-0017). Decouples observability data (per-query traces with retrieval/rerank/LLM metadata) from the audit log so privacy-sensitive content can be retained on a different schedule than operational metrics.

## What it stores

`QueryTrace` rows (`query_traces` table) carry, for every query that runs through the pipeline:

- Pipeline-level: `request_id`, `namespace`, `pipeline_version`, latency, `grounding_mode`, `query_pipeline`, `eval_mode` flag.
- Retrieval: number of chunks fetched, retained after dedup/threshold, reranker provider/scores when `VEKTRA_RERANK_ENABLED=true`.
- Generation: model name (including streaming responses), prompt/completion token counts, `no_relevant_context` and `context_only` flags.

Conversation question/answer text is captured **only** when `VEKTRA_EVAL_MODE=true` (staging-only batch evaluation use case, ARCH-050). Production traces are metadata-only.

## Reporting

The analytics router (prefix `/api/v1`) exposes three admin-scoped endpoints:

- `GET /api/v1/traces` — paginated `QueryTrace` listing. Filters: `namespace`, `from`, `to`, `model`, `min_duration_ms`, `limit` (max 500), `offset`.
- `GET /api/v1/traces/{response_id}` — single trace by `response_id` (`404 ERR-ANALYTICS-002` when missing).
- `GET /api/v1/metrics` — aggregated metrics over an optional `(namespace, from, to)` window: `total_queries`, `avg_latency_ms`, `p95_latency_ms`, `avg_retrieval_score`, `queries_per_hour`, and a `model_distribution` map (LLM model → query count).

Storage is optional and gated by `VEKTRA_ANALYTICS_STORE_TRACES`. Retention is independent (`VEKTRA_ANALYTICS_RETENTION_DAYS`) so traces can outlive the audit log or vice versa.

See [architecture.md](../.s2s/architecture.md) for the component specification.
