---
provides_requires:
  provides:
    - "LitellmProvider:class"
    - "SimpleQueryPipeline:class"
---
# Implementation Plan: vektra-core - QueryPipeline, LLM abstraction, streaming, conversations

**ID**: 20260217-component-core
**Status**: completed
**Branch**: N/A
**Created**: 2026-02-17T22:42:39Z
**Updated**: 2026-02-17T22:42:39Z

## Traceability

**Source**: component-core
**Source Type**: architecture

## References

### Requirements
- REQ-003: WF-QUERY-001: RAG Query workflow @.s2s/requirements.md
- REQ-013: vektra-core Phase 1 API surface @.s2s/requirements.md
- REQ-042: Streaming query responses @.s2s/requirements.md
- REQ-043: Configurable prompt templates @.s2s/requirements.md
- REQ-044: Safeguard hook interface @.s2s/requirements.md
- REQ-047: Multi-provider LLM support @.s2s/requirements.md
- REQ-049: Multi-turn conversation context @.s2s/requirements.md
- REQ-051: Operator privacy: no conversation content access @.s2s/requirements.md
- REQ-053: QueryPipeline Protocol @.s2s/requirements.md
- REQ-055: Response and citation traceability @.s2s/requirements.md
- REQ-059: LLM graceful degradation @.s2s/requirements.md
- REQ-060: QueryTrace for RAG observability @.s2s/requirements.md
- REQ-065: Prompt versioning @.s2s/requirements.md
- NFR-001: Query latency targets @.s2s/requirements.md
- NFR-011: Concurrent query handling @.s2s/requirements.md

### Architecture
- ARCH-024: LLMProvider abstraction via litellm @.s2s/architecture.md
- ARCH-028: Conversation management @.s2s/architecture.md
- ARCH-036: SimpleQueryPipeline steps @.s2s/architecture.md
- ARCH-041: QueryTrace structure @.s2s/architecture.md
- ARCH-043: Graceful degradation @.s2s/architecture.md
- ARCH-046: Standalone RAG evaluation strategy @.s2s/architecture.md
- ARCH-048: prompt_version as template hash @.s2s/architecture.md
- ARCH-054: Composable Jinja2 prompt templates @.s2s/architecture.md
- ARCH-055: Token budget allocation @.s2s/architecture.md
- ARCH-056: Retrieval quality controls @.s2s/architecture.md

### Decisions
- ADR-0008: LLM abstraction with litellm @.s2s/decisions/ADR-0008-llm-abstraction-litellm.md
- ADR-0014: QueryPipeline abstraction @.s2s/decisions/ADR-0014-query-pipeline-abstraction.md
- ADR-0017: Audit/analytics separation via QueryTrace @.s2s/decisions/ADR-0017-audit-analytics-separation.md
- ADR-0019: Three-tier RAG evaluation strategy @.s2s/decisions/ADR-0019-rag-evaluation-strategy.md
- ADR-0020: Composable Jinja2 prompt templates @.s2s/decisions/ADR-0020-prompt-template-architecture.md
- ADR-0021: Retrieval quality controls in QueryPipeline @.s2s/decisions/ADR-0021-retrieval-quality-controls.md

### Dependencies
- 20260217-component-shared
- 20260217-infra-database
- 20260217-component-index

## Overview

Implements the core RAG pipeline. SimpleQueryPipeline executes embed_query → vector_search → retrieval_filter → build_prompt → llm_call → safeguard steps. LitellmProvider abstracts OpenAI, Anthropic, and Ollama behind a unified interface. In-memory conversation management enables multi-turn queries. SSE streaming delivers first tokens within 2 seconds. QueryTrace records per-step timing and is emitted via structlog. Prompt templates are composable Jinja2 files with prompt_version hashing.

## Design Notes

**docs-008 dependency**: The `no_relevant_context: bool` field in QueryResponse (ARCH-056) must be decided before implementation. If the field is added (QueryResponse.no_relevant_context = False by default), it must be present in the Pydantic type in vektra_shared before this plan begins. Resolve docs-008 first (it's Wave 0).

- SimpleQueryPipeline step names for StepTrace: `embed_query`, `vector_search`, `retrieval_filter`, `build_prompt`, `llm_call`, `safeguard`
- Retrieval quality controls (ARCH-056): minimum relevance score threshold (VEKTRA_MIN_RELEVANCE_SCORE, default 0.3), overlap deduplication (remove chunks with > 80% token overlap), no_relevant_context detection when all chunks fall below threshold
- Token budget allocation (ARCH-055): priority order: system prompt tokens > question tokens > reserve buffer > chunks (60% of remaining context window) > conversation history
- Conversation history: in-memory dict keyed by conversation_id (UUID). Max turns = VEKTRA_MAX_CONVERSATION_TURNS (default 10). Lost on restart (Phase 1 design per REQ-049).
- prompt_version = `SHA-256(template_content)[:8]` per template file, recorded per-template in StepTrace.metadata (ARCH-048).
- Graceful degradation (REQ-059): primary timeout → fallback model attempt → context-only response (sources array, answer=null, context_only=true). All steps recorded in QueryTrace.
- QueryTrace does NOT contain query text or response text (REQ-051 / ADR-0017).

## Tasks

- [x] Implement `vektra_core/providers/litellm_provider.py` as LitellmProvider implementing LLMProvider Protocol: `complete(messages, model, max_tokens)` → dict, `stream(messages, model)` → AsyncIterator[str], `health_check()` → ok/unavailable, `count_tokens(messages)` → int; wrap litellm calls with timeout (fallback_timeout_ms from LLMConfig); support OpenAI, Anthropic, Ollama via litellm model string
- [x] Implement `vektra_core/templates.py`: load three Jinja2 templates (system.j2, context.j2, conversation.j2) from VEKTRA_TEMPLATE_DIR (fallback to built-in defaults); compute prompt_version = SHA256(content)[:8] per template on load; expose render_system(), render_context(chunks), render_conversation(history) functions; ARCH-057 step 8: validate all templates load on startup
- [x] Implement `vektra_core/budget.py`: token budget allocator (ARCH-055) given model context window, system prompt tokens, question tokens, conversation history tokens: allocate 60% of remaining to chunks, trim oldest history turns first, trim chunks from lowest score first
- [x] Implement `vektra_core/pipeline.py` as SimpleQueryPipeline implementing QueryPipeline Protocol:
  - `execute(query_request) → (QueryResponse, QueryTrace)`: embed_query → vector_search → retrieval_filter (relevance threshold, overlap dedup, no_relevant_context) → build_prompt (token budget) → llm_call (with fallback) → safeguard.pre_response → assemble QueryResponse (response_id UUID, sources with citation_id UUIDs)
  - `execute_stream(query_request) → AsyncIterator[str]`: same flow but llm_call uses `stream()` for SSE, safeguard applied after stream completes
  - Track per-step duration in StepTrace; emit full QueryTrace via structlog at end
- [x] Implement graceful degradation in pipeline: wrap primary `llm.complete()` in `asyncio.wait_for(timeout_ms)`, on timeout try fallback model, on second failure return context-only response if context_only_enabled=True
- [x] Implement `vektra_core/conversation.py`: in-memory `ConversationStore` dict; `get_history(conversation_id)` returns list of turn dicts; `add_turn(conversation_id, question, answer)`; `prune(conversation_id)` enforces max turns; thread-safe access (asyncio.Lock)
- [x] Create `vektra_core/api.py` with FastAPI router:
  - `POST /api/v1/query`: accept {question, conversation_id?, namespace?, top_k?}; check Accept header for SSE (text/event-stream) vs JSON; run safeguard.pre_query(); call pipeline.execute() or execute_stream(); save conversation turn; return QueryResponse or SSE stream; require `query` or `admin` scope
  - `GET /api/v1/providers`: list LLM providers with name, status, model; require `query` or `admin` scope
- [x] Implement SSE response: use FastAPI `StreamingResponse` with `text/event-stream` content type; format events as `data: {token}\n\n`; send final `data: [DONE]\n\n`; handle client disconnect to cancel LLM request
- [x] Create built-in template files in `vektra_core/templates/`: `system.j2` (system instructions), `context.j2` (chunk injection), `conversation.j2` (history formatting); document available variables in each
- [x] Write unit tests: token budget allocation, retrieval filter (threshold, overlap dedup, no_relevant_context), prompt rendering with variable substitution, QueryTrace field coverage (no query/response text), conversation turn management (max turns prune), graceful degradation flow — 50/50 PASS
- [x] Write integration tests (API layer): full query JSON response, SSE streaming, auth enforcement, providers endpoint — included in 50 tests above

## Acceptance Criteria

- [ ] `POST /query` returns response with response_id (UUID) and sources with citation_ids
- [ ] First SSE token delivered within 2 seconds of request start (NFR-001 streaming)
- [ ] Client disconnect cancels LLM request (no orphan async task)
- [ ] QueryTrace emitted via structlog contains no query text or response text (REQ-051)
- [ ] Retrieval filter excludes chunks below VEKTRA_MIN_RELEVANCE_SCORE; QueryResponse.no_relevant_context=true when all chunks excluded
- [ ] Token budget respects 60% allocation to chunks; oldest conversation turns pruned first
- [ ] prompt_version = SHA-256(template_content)[:8] present in StepTrace for build_prompt step
- [ ] Graceful degradation: primary LLM timeout → fallback model → context-only response (never returns a 5xx to client on LLM failure)
- [ ] In-memory conversation history lost on restart (documented behavior, not a bug)

## Testing Approach

Unit tests cover all pure logic (budget, filter, trace). Integration tests use a mock LLM (litellm mock or in-process Ollama if available). SSE streaming tested with `httpx` async client. Graceful degradation tested by patching litellm to raise `asyncio.TimeoutError`. Performance: measure p95 query latency over 100 warm queries with Ollama (target < 10s per NFR-001, measured as a benchmark, not a hard CI gate per EX-004).

## Integration Notes

vektra-core uses the shared EmbeddingProvider (SentenceTransformersProvider) from ProviderRegistry for query embedding. It calls vektra-index via the VectorStoreProvider Protocol (registered PgvectorProvider) for chunk retrieval. The SafeguardHook (PassthroughSafeguard by default) is injected via ProviderRegistry. Conversation content (question + answer) must never reach the audit_log table (REQ-051); only response_id and request metadata are logged.
