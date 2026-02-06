# ADR-0014: QueryPipeline Protocol for RAG pipeline abstraction

**Status**: accepted
**Date**: 2026-02-06
**Context**: Architectural review 2026-02-06

## Context

The RAG query flow in vektra-core is procedural code: embed query, search vectors, build prompt, call LLM, return response. This works for Phase 1 but cannot accommodate Phase 2 features (hybrid search, reranking, query classification, confidence scoring, numerical verification) without significant refactoring.

Additionally, there is no structured observability for the RAG pipeline. ARCH-008 provides correlation IDs and OpenTelemetry spans, but RAG-specific tracing (which model was used, how many chunks were retrieved with what scores, which template was used, how long each step took) requires a dedicated trace type.

## Decision

Introduce `QueryPipeline` as a Protocol in vektra_shared that abstracts the RAG query flow and produces both a response and a structured trace.

```python
class QueryPipeline(Protocol):
    async def execute(query: QueryRequest) -> tuple[QueryResponse, QueryTrace]
    async def execute_stream(query: QueryRequest) -> AsyncIterator[QueryChunk]
```

**Phase 1 implementation**: `SimpleQueryPipeline`
1. `embed_query()` via EmbeddingProvider
2. `vector_search()` via VectorStoreProvider (dense only, with optional metadata filters)
3. `safeguard.post_retrieval()` via SafeguardHook
4. `build_prompt()` using Jinja2 template (prompt_version tracked)
5. `llm_call()` via LLMProvider (with graceful degradation per ARCH-043)
6. `safeguard.pre_response()` via SafeguardHook
7. Return `(QueryResponse, QueryTrace)`

Each step is timed and recorded as a `StepTrace` in `QueryTrace`. The trace is emitted via structlog, separate from the audit log (ARCH-041).

**Phase 2 implementation**: `AdvancedQueryPipeline` adding:
- Query classification (factual, summary, comparison)
- Hybrid search (dense + sparse + RRF fusion)
- Cross-encoder reranking (top-20 -> top-5)
- Confidence scoring and numerical verification
- Response synthesis strategies

Both implementations share the same Protocol. Selection via `VEKTRA_QUERY_PIPELINE=simple|advanced`.

**Dependencies injected via constructor**: EmbeddingProvider, VectorStoreProvider, LLMProvider, SafeguardHook, EventEmitter. The pipeline is a composition of other Protocols, not a standalone monolith.

## Options Considered

### Procedural code in vektra-core (status quo)

**Pros**:
- Simpler Phase 1, fewer abstractions

**Cons**:
- Phase 2 features require rewriting the core query handler
- No structured RAG trace without intrusive logging
- No way to A/B test different pipeline strategies

### QueryPipeline Protocol (chosen)

**Pros**:
- Phase 2 pipeline swap is a config change
- Structured QueryTrace from day one
- Dependencies explicit via constructor injection
- Testable in isolation (mock dependencies)

**Cons**:
- One additional Protocol and implementation
- Slight indirection in code navigation

### LlamaIndex as pipeline framework

**Pros**:
- Hybrid search, reranking, query routing already implemented
- Large ecosystem of integrations

**Cons**:
- Heavy dependency (~hundreds of MB with extras)
- Opaque debugging (stack traces through dozens of internal files)
- Version churn (API changes between minor versions)
- Opinionated abstractions conflict with infrastructure platform goals
- With LLM-assisted development, direct implementation is feasible

See ADR-0016 for the full LlamaIndex exclusion rationale.

## Consequences

### Positive

- RAG pipeline is swappable via configuration
- QueryTrace provides per-step observability from day one
- Pipeline composition makes dependencies explicit and testable
- Phase 2 features (reranking, classification) are additive, not rewriting
- A/B testing of pipeline strategies possible via config

### Negative

- One more Protocol and factory to maintain
- execute_stream() must emit QueryTrace at stream completion (slightly more complex than non-streaming)
