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

`GET /api/v1/admin/analytics/...` endpoints aggregate counts, p50/p95 latency, retrieval/rerank score distributions, and grounding-mode breakdown per namespace.

Storage is optional and gated by `VEKTRA_ANALYTICS_STORE_TRACES`. Retention is independent (`VEKTRA_ANALYTICS_RETENTION_DAYS`) so traces can outlive the audit log or vice versa.

See [architecture.md](../.s2s/architecture.md) for the component specification.
