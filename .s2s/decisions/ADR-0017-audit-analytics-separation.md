# ADR-0017: Audit/analytics separation via QueryTrace

**Status**: accepted
**Date**: 2026-02-06
**Context**: Architectural review 2026-02-06

## Context

There is a fundamental tension between two data flows in Vektra:

**Audit logging** (REQ-022, REQ-051, NFR-007): records who did what, when. Must NOT contain query text or response content (GDPR compliance, operator privacy). Retention: 90+ days (NFR-008). Access: admin scope.

**RAG quality analytics**: records how the pipeline performed. Needs chunk IDs with relevance scores, model used, token counts, per-step latency, prompt template version. Used for debugging slow queries, improving retrieval quality, A/B testing prompts, and feeding the feedback loop. Does NOT need query text or response content either (chunk references and scores are sufficient).

Without explicit separation, Phase 2 faces two bad options:
1. Put analytics data in the audit log, violating REQ-051/GDPR
2. Have no data for RAG quality improvement

This is effectively a CQRS-lite pattern: same event (query executed), two projections with different schemas and privacy rules.

## Decision

Introduce `QueryTrace` as a structured type separate from the audit log. QueryTrace captures per-step pipeline performance data without containing query text or response content.

```python
class StepTrace:
    name: str           # embed_query | vector_search | build_prompt | llm_call | safeguard
    duration_ms: int
    metadata: dict      # step-specific: model, dimensions, top_k, token_count, etc.

class QueryTrace:
    response_id: UUID   # correlates with QueryResponse
    steps: list[StepTrace]
    total_duration_ms: int
    chunks_retrieved: list[ChunkRef]   # chunk_id + relevance_score
    llm_model: str
    prompt_version: str                # SHA-256[:8] of template content
    created_at: datetime
```

**Privacy properties**:
- QueryTrace does NOT contain query text (the user's question)
- QueryTrace does NOT contain response text (the LLM's answer)
- QueryTrace contains chunk_ids and scores (not chunk text)
- QueryTrace is GDPR-safe: no personal data, only system performance metrics

**Phase 1**: QueryTrace is emitted via structlog as a JSON event (`event: "query_trace"`). No dedicated storage. Searchable via log aggregation tools.

**Phase 2**: Dedicated `query_traces` table with retention shorter than audit (configurable via `VEKTRA_ANALYTICS_RETENTION_DAYS`). API for querying traces by response_id, time range, model, latency thresholds. Feeds dashboard for RAG quality monitoring.

**Relationship to audit log**:

| Property | Audit log | QueryTrace |
|----------|-----------|------------|
| Contains query/response text | Never | Never |
| Contains chunk content | Never | Never (only IDs + scores) |
| Contains operator identity | Yes (key_id) | No (correlated via response_id if needed) |
| Contains pipeline metrics | No | Yes (per-step timing, model, tokens) |
| GDPR classification | Compliance data | System telemetry |
| Retention | 90+ days (NFR-008) | Shorter, configurable |
| Phase 1 storage | Structured log file | Structured log event |
| Phase 2 storage | Audit table | Dedicated traces table |

**How QueryPipeline produces QueryTrace**: `QueryPipeline.execute()` returns `tuple[QueryResponse, QueryTrace]`. Each pipeline step wraps its execution in a timing context and appends a `StepTrace`. The caller (HTTP handler) emits the trace via structlog and returns only the QueryResponse to the client.

## Options Considered

### Single log stream for both audit and analytics

**Pros**:
- Simplest implementation
- Single place to look for data

**Cons**:
- Either violates REQ-051 (content in audit) or loses analytics data
- Different retention requirements impossible to enforce
- Audit compliance review becomes harder with mixed data

### Separate QueryTrace type (chosen)

**Pros**:
- Clean privacy boundary: audit has no content, trace has no identity
- Different retention policies possible
- Phase 2 analytics storage is additive (structlog emission continues)
- QueryPipeline naturally produces the trace as part of execution

**Cons**:
- Two data streams to understand
- Phase 1 relies on log aggregation for trace queries (no dedicated API)

### Analytics only in Phase 2

**Pros**:
- Less Phase 1 work

**Cons**:
- No historical trace data when Phase 2 analytics storage arrives
- QueryPipeline must be modified to emit traces (touching all steps)
- Cannot debug RAG quality issues in Phase 1

## Consequences

### Positive

- GDPR/REQ-051 compliance preserved: audit never contains content
- RAG observability from day one via structured log emission
- Phase 2 analytics storage is additive, not a rewrite
- response_id links traces to responses for feedback correlation
- prompt_version in trace enables A/B analysis of template changes
- Per-step timing identifies bottlenecks (embedding? LLM? search?)

### Negative

- Phase 1 trace queries require log aggregation tools (not a dedicated API)
- Two data streams require documentation to prevent confusion
- QueryTrace emission adds ~1ms overhead per query (structlog serialization)
