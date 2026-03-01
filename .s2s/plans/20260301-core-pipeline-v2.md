# Implementation Plan: vektra-core - AdvancedQueryPipeline, safeguards, streaming trace

**ID**: 20260301-core-pipeline-v2
**Status**: pending
**Branch**: N/A
**Created**: 2026-03-01T14:30:09Z
**Updated**: 2026-03-01T14:30:09Z

## Traceability

**Source**: core-pipeline-v2
**Source Type**: architecture

## Provides / Requires

**Provides**:
- AdvancedQueryPipeline implementation with query rewriting, reranking, hybrid search (consumers: component-analytics, component-learn)
- PresidioPIISafeguard with pre_response anonymization and post_retrieval filtering (consumers: component-learn, infra-phase2)
- Streaming QueryTrace emission via structlog (consumers: component-analytics)
- Enhanced graceful degradation with per-step fallback matrix (consumers: infra-phase2)

**Requires**:
- 20260301-shared-protocols-phase2.md: AdvancedQueryPipeline Protocol definition, extended SafeguardHook types
- 20260301-index-hybrid.md: hybrid search (SearchMode.HYBRID), SparseEmbeddingProvider registered in ProviderRegistry
- 20260301-core-conversations.md: persistent ConversationStore with get_history() returning encrypted turns

## References

### Requirements
- REQ-003: RAG query workflow @.s2s/requirements.md
- REQ-042: Streaming query responses @.s2s/requirements.md
- REQ-044: Safeguard hook interface @.s2s/requirements.md
- REQ-049: Multi-turn conversation context @.s2s/requirements.md
- REQ-051: Operator privacy: no conversation content access @.s2s/requirements.md
- REQ-053: QueryPipeline Protocol @.s2s/requirements.md
- REQ-059: LLM graceful degradation @.s2s/requirements.md
- REQ-060: QueryTrace for RAG observability @.s2s/requirements.md
- REQ-065: Prompt versioning @.s2s/requirements.md

### Architecture
- ARCH-036: QueryPipeline - AdvancedQueryPipeline with reranking, hybrid search @.s2s/architecture.md
- ARCH-041: Audit/analytics separation - QueryTrace structure and emission @.s2s/architecture.md
- ARCH-043: Graceful degradation - fallback model, context-only response @.s2s/architecture.md
- ARCH-048: Prompt versioning - SHA-256[:8] hash in QueryTrace @.s2s/architecture.md
- ARCH-049: SafeguardResult content modification - Presidio PII anonymization @.s2s/architecture.md
- ARCH-054: Composable Jinja2 prompt templates @.s2s/architecture.md
- ARCH-055: Token budget allocation @.s2s/architecture.md
- ARCH-056: Retrieval quality controls @.s2s/architecture.md
- ARCH-061: Conversational query rewriting - pre-retrieval LLM call @.s2s/architecture.md

### Decisions
- ADR-0014: QueryPipeline abstraction @.s2s/decisions/ADR-0014-query-pipeline-abstraction.md
- ADR-0017: Audit/analytics separation via QueryTrace @.s2s/decisions/ADR-0017-audit-analytics-separation.md
- ADR-0023: Conversational query rewriting @.s2s/decisions/ADR-0023-conversational-query-rewriting.md

### Dependencies
- 20260301-shared-protocols-phase2.md
- 20260301-index-hybrid.md
- 20260301-core-conversations.md

## Overview

This plan implements the AdvancedQueryPipeline, replacing the Phase 1 SimpleQueryPipeline as the primary RAG pipeline when `VEKTRA_QUERY_PIPELINE=advanced`. The advanced pipeline adds three pre-LLM steps: conversational query rewriting (resolving pronouns and ellipsis via an LLM call), hybrid search (dense + sparse retrieval with RRF fusion), and cross-encoder reranking (narrowing top-20 candidates to top-5). These steps address the retrieval quality gaps observed during Phase 1 manual testing, where multi-turn conversations degraded after the first turn due to unresolved anaphoric references.

The plan also resolves two technical debt items. DEBT-002: streaming queries now collect StepTrace entries during the async generator lifecycle and emit a full QueryTrace via structlog after the stream completes. DEBT-003: the post_retrieval safeguard trust boundary is called in both execute() and execute_stream() paths, between retrieval filter and prompt construction.

A concrete SafeguardHook implementation, PresidioPIISafeguard, replaces the Phase 1 PassthroughSafeguard when `VEKTRA_SAFEGUARD_MODE=presidio`. Presidio detects PII in LLM responses (pre_response) and sets modified_content with anonymized text. The post_retrieval boundary filters chunks containing PII before they enter the prompt. The graceful degradation matrix is extended to handle failures in each new pipeline step (rewriting, reranking) with clear skip-and-continue semantics.

## Design Notes

**AdvancedQueryPipeline step sequence**:
```
Step 0: query_rewrite    - LLM call with rewrite.j2 template (skip if no conversation history)
Step 1: embed_query      - EmbeddingProvider.embed_query(rewritten_query)
Step 2: sparse_embed     - SparseEmbeddingProvider.embed_query(rewritten_query) (skip if not registered)
Step 3: vector_search    - VectorStoreProvider.search(mode=HYBRID if sparse available, else DENSE)
Step 4: rerank           - cross-encoder reranking via rerankers library (skip if model not configured)
Step 5: retrieval_filter - score threshold + overlap dedup (reuses Phase 1 _apply_retrieval_filter)
Step 6: safeguard.post_retrieval - chunk-level PII filtering (DEBT-003)
Step 7: build_prompt     - token budget allocation + Jinja2 rendering (reuses Phase 1 logic)
Step 8: llm_call         - LLM with graceful degradation (reuses _call_llm_with_fallback)
Step 9: safeguard.pre_response - PII anonymization on LLM output (ARCH-049)
```

**Query rewriting (ARCH-061, ADR-0023)**:
- Template: `rewrite.j2` in the templates directory (alongside system.j2, context.j2, conversation.j2)
- Input: question + conversation history (bounded by VEKTRA_MAX_CONVERSATION_TURNS)
- Output: self-contained rewritten query
- Skip condition: conversation_id is None or history is empty
- StepTrace metadata: `{"original_query_hash": str, "rewritten": bool, "history_turns_used": int}` (no plaintext query in trace per REQ-051)
- Config: `VEKTRA_QUERY_REWRITE_ENABLED` (default true)

**Reranking**:
- Library: `rerankers` (Answer.AI) - backend-agnostic cross-encoder wrapper
- Model: configurable via `VEKTRA_RERANKER_MODEL` (default: `cross-encoder/ms-marco-MiniLM-L-6-v2`)
- Flow: take top-20 from vector search, rerank, return top-K (K = query.top_k, default 5)
- Skip condition: VEKTRA_RERANKER_MODEL not set or empty string
- Graceful degradation: if reranker fails, fall through to unreranked results with a warning log

**Post-retrieval safeguard (DEBT-003)**:
- Called after retrieval_filter, before build_prompt
- SafeguardResult.filtered_ids: list of chunk IDs to exclude
- Pipeline removes matching chunks from the selected set
- PassthroughSafeguard returns allowed=True with no filtered_ids (existing behavior preserved)

**PresidioPIISafeguard**:
- Location: `vektra_core/safeguards/presidio.py`
- pre_query: pass-through (Presidio is for output, not input filtering)
- post_retrieval: scan chunk text for PII entities, return filtered_ids for chunks exceeding PII threshold
- pre_response: scan LLM answer for PII, set modified_content with anonymized text (e.g., "John" -> "<PERSON>")
- Config: `VEKTRA_SAFEGUARD_MODE=presidio` (default: passthrough)
- Dependencies: `presidio-analyzer`, `presidio-anonymizer` added to vektra-core
- Graceful degradation: if Presidio fails to load (missing model), fall back to PassthroughSafeguard with warning

**Streaming QueryTrace (DEBT-002)**:
- `_stream()` collects StepTrace entries for each step (embed, search, filter, build_prompt, token streaming)
- After the last token is yielded (or on error/cancellation), assemble QueryTrace and emit via structlog
- Yield `QueryChunk(type="trace", data={...})` as the penultimate SSE event (before "done")
- This allows the SSE consumer (e.g., component-analytics) to capture trace data from the stream

**Prompt versioning for A/B testing (ARCH-048)**:
- TemplateRenderer already computes prompt_version as SHA-256[:8]
- AdvancedQueryPipeline includes rewrite.j2 in the hash computation (4 templates instead of 3)
- StepTrace metadata for build_prompt includes per-template hashes

**Pipeline selection**:
- `VEKTRA_QUERY_PIPELINE=simple` -> SimpleQueryPipeline (Phase 1, default)
- `VEKTRA_QUERY_PIPELINE=advanced` -> AdvancedQueryPipeline
- Both registered in ProviderRegistry under "query_pipeline" with name "simple" or "advanced"
- The active pipeline is selected by name at startup; "default" alias points to the configured value

**Graceful degradation matrix (ARCH-043, extended)**:
| Step | Failure | Behavior |
|------|---------|----------|
| query_rewrite | LLM call fails or times out | Skip rewriting, use original query, log warning |
| sparse_embed | SparseEmbeddingProvider not registered or fails | Fall back to DENSE search mode |
| rerank | Reranker model fails to load or score | Skip reranking, use vector search order, log warning |
| safeguard.post_retrieval | Safeguard raises exception | Skip filtering, use all chunks, log error |
| llm_call | Primary + fallback fail | Context-only response (existing behavior) |
| safeguard.pre_response | Safeguard raises exception | Return unmodified answer, log error |

## Tasks

- [ ] Create `vektra_core/safeguards/` package: `__init__.py` with factory function `create_safeguard(mode: str) -> SafeguardHook` that returns PassthroughSafeguard for "passthrough" and PresidioPIISafeguard for "presidio"; move PassthroughSafeguard import from vektra_shared (already there, no code move needed - just re-export through factory)
- [ ] Implement `vektra_core/safeguards/presidio.py`: PresidioPIISafeguard class implementing SafeguardHook Protocol; lazy-load `presidio_analyzer.AnalyzerEngine` and `presidio_anonymizer.AnonymizerEngine` on first call; `pre_query()` returns allowed=True (pass-through); `post_retrieval()` scans each chunk's text_snippet for PII entities, returns filtered_ids for chunks with entity count above VEKTRA_PII_CHUNK_THRESHOLD (default 3); `pre_response()` runs anonymizer on answer text, sets modified_content with anonymized output, records entity types in annotations dict
- [ ] Add `presidio-analyzer>=2.2` and `presidio-anonymizer>=2.2` to vektra-core/pyproject.toml dependencies; add `rerankers>=0.5` to dependencies
- [ ] Create `rewrite.j2` template in `vektra_core/templates/`: follow the template from ADR-0023; variables: `history` (list of turn dicts), `question` (string); output: single rewritten question
- [ ] Implement `vektra_core/reranker.py`: thin wrapper around `rerankers.Reranker`; `RerankerService.__init__(model_name: str)` loads cross-encoder model; `async rerank(query: str, results: list[SearchResult], top_k: int) -> list[SearchResult]` runs reranking in asyncio.to_thread() (CPU-bound inference), returns top_k results sorted by reranker score; handle import errors and model load failures by returning None from factory
- [ ] Implement `vektra_core/advanced_pipeline.py` as AdvancedQueryPipeline implementing QueryPipeline Protocol:
  - Constructor: same dependencies as SimpleQueryPipeline plus optional SparseEmbeddingProvider, optional RerankerService, rewrite_enabled flag
  - `execute(query)`: run the 10-step sequence (query_rewrite -> embed -> sparse_embed -> search -> rerank -> retrieval_filter -> post_retrieval safeguard -> build_prompt -> llm_call -> pre_response safeguard); collect StepTrace for each step; return (QueryResponse, QueryTrace)
  - Reuse `_apply_retrieval_filter()`, `allocate_token_budget()`, `_call_llm_with_fallback()` from existing pipeline module (import, do not copy)
  - Handle all skip conditions and graceful degradation per the matrix above
- [ ] Implement `execute_stream()` in AdvancedQueryPipeline: same pre-LLM steps as execute(); stream LLM tokens via SSE; collect StepTrace entries during generator lifecycle; after stream completes (or on error), assemble QueryTrace and yield as `QueryChunk(type="trace", data=trace_dict)` before yielding "done" event (DEBT-002)
- [ ] Backport streaming trace to SimpleQueryPipeline._stream(): add StepTrace collection for each step (embed, search, filter, build_prompt, llm_stream); emit QueryTrace via structlog after stream completes; yield QueryChunk(type="trace") before "done" (DEBT-002)
- [ ] Add post_retrieval safeguard call to SimpleQueryPipeline.execute() and _stream(): after retrieval_filter and before build_prompt, call `safeguard.post_retrieval(query_ref, filtered_results, sg_ctx)`; remove chunks whose IDs appear in SafeguardResult.filtered_ids; add StepTrace entry (DEBT-003)
- [ ] Update TemplateRenderer to include rewrite.j2 in prompt_version hash when the template exists: make _TEMPLATE_NAMES configurable or check for optional templates; rewrite.j2 is optional (only loaded by AdvancedQueryPipeline)
- [ ] Update `vektra_core/api.py` _sse_generator to handle new QueryChunk type="trace": serialize trace data as JSON SSE event
- [ ] Add VEKTRA_QUERY_PIPELINE, VEKTRA_QUERY_REWRITE_ENABLED, VEKTRA_RERANKER_MODEL, VEKTRA_SAFEGUARD_MODE, VEKTRA_PII_CHUNK_THRESHOLD to config classes in vektra_shared (QueryPipelineConfig or new AdvancedPipelineConfig)
- [ ] Write unit tests for AdvancedQueryPipeline: query rewriting with mock LLM (verify rewritten query used for embedding), reranking with mock cross-encoder, hybrid search mode selection, post_retrieval safeguard filtering, graceful degradation for each failure path (6 scenarios from matrix), streaming trace emission
- [ ] Write unit tests for PresidioPIISafeguard: post_retrieval filters chunks with PII above threshold, pre_response anonymizes PII entities, annotations record entity types, graceful fallback when Presidio model unavailable
- [ ] Write integration tests: full advanced pipeline query with mock providers, streaming with trace event, pipeline selection via VEKTRA_QUERY_PIPELINE env var, SimpleQueryPipeline post_retrieval boundary (DEBT-003 regression test)

## Acceptance Criteria

- [ ] `VEKTRA_QUERY_PIPELINE=advanced` activates AdvancedQueryPipeline; `simple` retains Phase 1 behavior
- [ ] Query rewriting resolves pronouns in multi-turn conversations: rewritten query used for embedding (verifiable via StepTrace metadata)
- [ ] Reranking narrows vector search results from top-20 to top-K using cross-encoder scores
- [ ] Hybrid search mode used when SparseEmbeddingProvider is registered; falls back to DENSE when not available
- [ ] post_retrieval safeguard called in both execute() and execute_stream() for both pipeline implementations (DEBT-003)
- [ ] PresidioPIISafeguard anonymizes PII in LLM responses via modified_content (REQ-044)
- [ ] Streaming queries emit QueryTrace via structlog and as SSE event before "done" (DEBT-002)
- [ ] QueryTrace contains no query text or response text (REQ-051)
- [ ] Each new pipeline step has a graceful degradation path: failure in any step does not cause a 5xx error
- [ ] SimpleQueryPipeline unchanged in behavior when VEKTRA_QUERY_PIPELINE=simple (no regression)
- [ ] prompt_version includes rewrite.j2 hash when AdvancedQueryPipeline is active (ARCH-048)

## Testing Approach

Unit tests cover each new component in isolation. AdvancedQueryPipeline tests use mock providers (LLM, embedding, sparse embedding, vector store, reranker, safeguard) to verify the 10-step flow, skip conditions, and all 6 degradation paths. PresidioPIISafeguard tests use a real Presidio engine with synthetic text containing known PII patterns. Streaming trace tests verify that StepTrace entries accumulate during the async generator lifecycle and that the trace event is emitted before "done". Integration tests run the full pipeline through the FastAPI router with httpx, verifying JSON and SSE responses. Regression tests confirm SimpleQueryPipeline behavior is unchanged when VEKTRA_QUERY_PIPELINE=simple.

## Integration Notes

AdvancedQueryPipeline is registered in ProviderRegistry by the app entrypoint (lifespan function in vektra-app). The entrypoint reads VEKTRA_QUERY_PIPELINE and instantiates the selected pipeline class, injecting all dependencies from the registry. SparseEmbeddingProvider and RerankerService are optional: if not registered (e.g., fastembed or rerankers not installed), AdvancedQueryPipeline falls back to dense-only search and no reranking.

The PresidioPIISafeguard is created by the safeguard factory based on VEKTRA_SAFEGUARD_MODE. It replaces PassthroughSafeguard in the registry. The factory handles import errors gracefully: if presidio-analyzer is not installed, it logs a warning and returns PassthroughSafeguard.

component-analytics (Wave 3) will consume QueryTrace from both structlog output and the new SSE trace event. The trace contract (QueryTrace dataclass fields) must remain stable. component-learn (Wave 4) will use AdvancedQueryPipeline with course-scoped namespace filtering.

## Notes

<!-- Progress notes during implementation -->
