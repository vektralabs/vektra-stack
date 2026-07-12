# Implementation Plan: Sprint 3 — RAG quality

**ID**: 20260712-sprint3-rag-quality
**Status**: active
**Branch**: one branch/PR per item (`chore/debt-025-test-env-isolation`, `feat/feat-017-parent-chunk-expansion`, `feat/feat-021-namespace-citations`, FEAT-018 branch only if verification justifies it)
**Created**: 2026-07-12T08:39:46Z
**Updated**: 2026-07-12T09:35:00Z

## Traceability

**Source**: DEBT-025, FEAT-017, FEAT-018 (conditional on verification), FEAT-021
**Source Type**: backlog

Roadmap note: Sprint 3 in `vektra-internal/stack/20260321-implementation-roadmap-post-phase2.md`
listed FEAT-008, FEAT-013, FEAT-004, OQ-014. Deltas: FEAT-004 shipped in v0.5.0;
FEAT-013 is subsumed by FEAT-021 (citation titles/snippets); FEAT-008 stays out of
scope until it has a design (`/s2s:design`); OQ-014 not in this sprint.

## References

### Requirements
- REQ-055: response and citation traceability @.s2s/requirements.md (FEAT-021)
- REQ-051: no user text in traces (constrains multi-turn verification tooling; eval mode only)

### Architecture
- ARCH-037: ChunkingStrategy (dual parent/child) @.s2s/architecture.md
- ARCH-055: token budget allocation @.s2s/architecture.md
- ARCH-056: retrieval quality controls @.s2s/architecture.md
- ARCH-054: composable Jinja2 templates @.s2s/architecture.md
- ARCH-047: namespace as first-class entity with metadata @.s2s/architecture.md
- ARCH-050: three-tier evaluation strategy @.s2s/architecture.md

### Decisions
- ADR-0019: RAG evaluation strategy @.s2s/decisions/ADR-0019-rag-evaluation-strategy.md
- ADR-0021: retrieval quality controls @.s2s/decisions/ADR-0021-retrieval-quality-controls.md
- ADR-0023: conversational query rewriting @.s2s/decisions/ADR-0023-conversational-query-rewriting.md
- ADR-0020: prompt template architecture @.s2s/decisions/ADR-0020-prompt-template-architecture.md
- ADR-0025: learn chatbot widget @.s2s/decisions/ADR-0025-learn-chatbot-widget.md

### Dependencies
- none (TECH-002 eval harness merged in 20260325-rag-retrieval-quality)

## Overview

Sprint objective: improve RAG answer quality **and measure it**. Every quality change
is bracketed by eval-harness runs (TECH-002) on the same dataset and corpus config
(Combo D: multilingual MiniLM embeddings, chunk 500/100, hybrid BM25+dense, reranker
bge-reranker-v2-m3 top_k=5, threshold 0.15, advanced pipeline).

Order: (1) DEBT-025 warm-up so `make test` is green on dev machines, (2) baseline
eval, (3) FEAT-017 parent chunk expansion measured before/after, (4) FEAT-018
verification-first — implement only if multi-turn tests show FEAT-020 did not
already mitigate it, (5) FEAT-021 per-namespace citations.

## Design Notes

Ground truths from codebase analysis (2026-07-12, develop @ ca25717) that correct
the backlog assumptions:

- **Parent-child linkage is NOT stored** (FEAT-017 premise in backlog is wrong).
  `DualStrategyChunking` builds the hierarchy in memory (`vektra-ingest/chunking.py:259-313`)
  but `run_ingest` drops `chunk.parent_id` when building `ChunkEmbedding`
  (`vektra-ingest/pipeline.py:400-418`); `ChunkEmbedding` has no parent_id field
  (`vektra-shared/types.py:115-123`); Qdrant payload carries no parent_id
  (`vektra-index/providers/qdrant.py:181-187`); `DocumentChunkOrm.parent_id` is always
  NULL. Only `metadata.chunk_level` (parent|child) survives. FEAT-017 must plumb the
  id through and **reingest** with `VEKTRA_CHUNKING_STRATEGY=dual`.
- **Parents ARE indexed and searchable today** when dual is enabled: `run_ingest`
  embeds and upserts all chunks with no level filter; neither Qdrant `_build_filter`
  nor pgvector excludes `chunk_level=parent`. The "search excludes parents" AC is
  a real behavior change.
- **Qdrant mode keeps chunk text only in Qdrant**: with `VEKTRA_VECTOR_STORE_PROVIDER=qdrant`
  (local setup), `document_chunks` in Postgres is empty; parent text must be fetched
  via the vector store (Qdrant retrieve-by-id), not SQL.
- Parent size is hardcoded `chunk_size*3` at `vektra-ingest/pipeline.py:343-348`
  (backlog's "3000 tokens" only holds for chunk_size=1000; with Combo D 500 it is 1500).
- **Qdrant filter builder is must-only** (`qdrant.py:376-431`); FEAT-018 chunk
  exclusion needs `must_not` support. pgvector accepts `raw_filters` but silently
  drops them (`pgvector.py:100-130`) — paper escape hatch.
- **Per-turn chunk ids are recoverable**: `conversation_turns.response_id` is populated
  (BUG-013/DEBT-011) and `query_traces.chunks_retrieved` stores `[{chunk_id, score}]`
  keyed by response_id — but only when trace storage is on (auto: dev on / prod off).
- **FEAT-020 pattern to clone for FEAT-021**: `resolve_grounding_mode` /
  `resolve_show_sources` in `vektra-shared/namespace.py:19-82` read `namespaces.config`
  JSONB; the API layer resolves and passes the value on `QueryRequest`
  (`vektra-core/api.py:263-280`). Add `resolve_citations_enabled` the same way;
  admin `PATCH /admin/namespaces/{id}/config` whitelist must accept the new key.
- **context.j2 uses `<source id="{{ loop.index }}">`**, not the `<doc id title>` shown
  in the FEAT-021 backlog entry. `SearchResult.metadata` already carries `source_file`
  and `page` from the Qdrant payload; `document_name` comes from `_fetch_document_names`
  (currently applied to sources only, not to context rendering). Template edits change
  `prompt_version` (trace comparability across the sprint).
- **Widget has no citation rendering**: `renderMarkdown` (bold/italic/code/links/lists
  only) escapes literal `[1]`; inline citation markers hook into `renderInline`
  (`vektra-learn/widget/src/markdown.js:136-153`). Bundle via esbuild to
  `vektra-learn/static/vektra-chat.js`.
- **Eval harness**: HTTP clients only (`VEKTRA_API_URL` + `VEKTRA_API_KEY`; `.api-key`
  file no longer exists locally — use `.env` value). e2e computes answerability +
  latency; **RAGAS is not implemented** despite the TECH-002 AC text, and the dataset
  has no `ground_truth_answer` fields. Retrieval metrics: hit rate, MRR, precision@k.
  Dataset: 55 questions, all `namespace=default` (21 factual / 15 reasoning /
  10 multi-chunk / 9 adversarial; 37 IT / 18 EN). No multi-turn entries and no
  `conversation_id` support in the scripts: FEAT-018 verification needs a small
  multi-turn runner.
- **Local corpus state (2026-07-12)**: Qdrant collection `vektra` has 656 points;
  the 12 in namespace `default` are the intact excerpt eval corpus from 20260325
  (`costituzione_italiana.md` v2 + `udhr_excerpts.md` v2 + `sample.pdf`). Current
  local chunking is `fixed` 500/100. The full Costituzione PDF (97 chunks) was
  deliberately removed from `default` back then; FEAT-017 will reintroduce a
  larger corpus with `dual` chunking.

## Tasks

### 1. DEBT-025 — isolate unit tests from local .env (branch `chore/debt-025-test-env-isolation`)
- [x] Root cause confirmed: litellm import-time `load_dotenv()` during collection
- [x] Autouse fixture in `vektra-shared/tests/conftest.py` scrubs `VEKTRA_*` + external keys
- [x] `make test` green with populated `.env` (638 passed) + `make lint` green
- [x] Backlog entry updated (completed + resolution), changelog entry
- [x] PR created and merged (#84; #85 BUG-021, #86 backlog, #87 BUG-022 merged the same day)

### 2. Baseline eval (no branch; results recorded, not committed as code)
- [x] Eval corpus verified intact in namespace `default` (excerpt corpus, 12 chunks — no reingest needed)
- [x] BUG-021 discovered and fixed (search endpoint hardwired to pgvector — see Notes; branch `fix/bug-021-search-registry-providers`)
- [x] `make eval-retrieval` baseline recorded (hit rate, MRR, precision@k, per-category)
- [x] `make eval-e2e` baseline recorded (grounded rate, no-ctx, latency p50/p95)
- [x] Numbers logged in this plan (Notes) and in vektra-internal (`stack/20260712-sprint3-baseline-eval.md`)

### 3. FEAT-017 — parent chunk expansion (branch `feat/feat-017-parent-chunk-expansion`)
- [x] Propagate `parent_id` into `ChunkEmbedding` → Qdrant payload + pgvector column;
      keep deterministic child/parent ids at store time (pgvector now honors
      caller-provided uuid5 ids instead of generating uuid4)
- [x] Search excludes `chunk_level=parent` by default (both providers)
- [x] `VEKTRA_PARENT_EXPANSION_ENABLED` (default false) in `QueryPipelineConfig`
      + mirrored in `VektraSettings`
- [x] Expansion step in AdvancedQueryPipeline: fetch parent text by id via new
      `VectorStoreProvider.retrieve()`, replace child text **before** token
      budgeting (ARCH-055); children of the same parent collapse into the
      highest-scored one (runs after the 80% overlap dedup, so no interaction)
- [x] Trace metadata records expansion (children_expanded, siblings_merged,
      parents_fetched)
- [x] Reingest eval corpus with `dual` strategy; measured both (see Notes):
      A dual-no-expansion ≈ baseline on e2e but -6.5pp retrieval hit (boundary
      effect); B expansion-on: zero e2e flips, sources 1.4 → 1.0
- [x] Unit tests: store-time linkage, search filter, expansion logic, budget
      accounting (17 new tests; suite 658 passed)

### 4. FEAT-018 — verification first (no branch unless justified)
- [ ] Multi-turn scenario runner against `/api/v1/query` with `conversation_id`
      (same-topic follow-up, topic switch, "give me others", negation)
- [ ] Evaluate with FEAT-020 grounding modes: does history use already mitigate
      the "same chunks every turn" complaint?
- [ ] Decision recorded in backlog (implement with Qdrant `must_not` + turn-chunk
      tracking, or close as mitigated) — implementation only if tests justify it

### 5. FEAT-021 — per-namespace citations (branch `feat/feat-021-namespace-citations`)
- [x] `resolve_citations_enabled` in `vektra_shared/namespace.py` (clone of FEAT-020
      pattern), default false; admin config PATCH whitelist + GET resolved defaults
      extended
- [x] `QueryRequest.citations_enabled` resolved at API layer (core + learn), passed
      to renderer
- [x] `system.j2` Rule 1 conditional (cite `[id]` inline when enabled and context
      present)
- [x] `context.j2`: `title` attribute (document_name + page, composed at
      prompt-build time) when enabled; template tolerant of chunks without title
- [x] `SourceRef.title` in API responses (core sync + streaming, learn course
      response); `source_file`/`page` were already in `SearchResult.metadata`
- [x] Widget: `[n]` markers rendered as superscripts with source-title tooltip
      wired in `addSources()`; bundle built by the Docker widget-builder stage
      (static/ is gitignored)
- [x] Default-off behavior byte-identical prompts, asserted in template tests
      (prompt_version changes because template files changed — documented in
      changelog)
- [x] Unit tests: namespace resolution (4), templates (6), pipeline flow (2),
      admin integration (2 + resolved defaults); suite 672 passed

## State & Data Lifecycle

| State element | Created by | Updated by | Invalidated/Deleted by |
|---------------|-----------|------------|------------------------|
| Qdrant payload `parent_id` + `chunk_level` | ingest (FEAT-017) | reingest of document | document delete / collection reindex |
| `namespaces.config.citations_enabled` | admin config PATCH | admin config PATCH | key removal via PATCH null / namespace delete |
| `tests/eval/results_*.jsonl` | eval runs | overwritten per run | git history keeps baselines |
| conversation→chunk linkage (traces) | query pipeline (existing) | n/a (append-only) | analytics retention job |

## Acceptance Criteria

- [ ] Per-item ACs from `.s2s/BACKLOG.md` (DEBT-025 ✓, FEAT-017, FEAT-018-verification, FEAT-021)
- [ ] Baseline and post-change eval runs use the same dataset, corpus and config,
      differing only in the variable under test; runs with per-question errors are
      not accepted as baselines
- [ ] FEAT-018 go/no-go decision documented with test evidence in the backlog
- [ ] FEAT-021 default-off leaves current behavior unchanged (templates render
      identically when disabled)
- [ ] `make lint` + `make test` green before every push

## Testing Approach

Unit tests accompany every code change (chunk linkage, search filters, expansion,
template conditionals, namespace resolution). Quality is measured with the TECH-002
harness before/after each RAG-affecting change. Widget changes verified by rebuilding
the bundle and a manual smoke in the Moodle dev stack.

### Test Infrastructure

- **Required**: running stack (vektra + postgres + qdrant profile), eval corpus in
  namespace `default`, reachable LLM (local vLLM via Tailscale IP), `VEKTRA_API_URL`
  + `VEKTRA_API_KEY` env for the harness
- **Provided by**: `docker compose --profile qdrant up -d`; corpus reingest task in
  items 2-3; existing `.env` (Combo D)
- **False-pass guard**: eval scripts mark per-question errors and print error counts;
  a baseline with errors > 0 is rerun, not recorded. FEAT-018 verification failures
  block implementation, not silently skip it.

## Integration Notes

- FEAT-021 touches the widget served by vektra-stack (`/static/learn/vektra-chat.js`);
  the Moodle plugin embeds it unchanged — no vektra-moodle release needed unless new
  `data-*` attributes are added (not planned: citations are namespace-driven).
- FEAT-017 requires a corpus reingest wherever dual strategy is enabled; local only,
  no production deployments exist (no backward-compat constraints).
- Backlog drift found during analysis (FEAT-017 stored-linkage premise, FEAT-021
  template quote, TECH-002 RAGAS AC, DEBT-025 `_env_file` suggestion) is corrected
  in the backlog as each item lands.

## Notes

- 2026-07-12: DEBT-025 completed on `chore/debt-025-test-env-isolation`
  (fixture + docs, suite 638 passed / 3 skipped). Plan file rides the same PR.
- 2026-07-12: local stack recovered for eval: Qdrant container was down since ~May,
  restarted from compose profile with existing volume (collection green, 656 points).
  The 12 points in namespace `default` turned out to be the intact excerpt eval
  corpus (`costituzione_italiana.md` v2, 6 chunks + `udhr_excerpts.md` v2, 4 +
  `sample.pdf`, 2) — no reingest needed for the baseline.
- 2026-07-12: baseline eval was blocked twice, both fixed: (a) **BUG-021** —
  `/api/v1/search` was hardwired to pgvector and read the sparse provider from a
  never-populated `app.state` attribute → zero results for all 55 questions in
  qdrant mode, hybrid always degraded to dense. Fixed on
  `fix/bug-021-search-registry-providers` (registry-based resolution + endpoint
  tests). (b) stale vLLM model id in local `.env` (`qwen35-27b-fp8` after a
  `vllm-switch`; server serves `qwen36-35b-a3b-fp8`) → every LLM call failed with
  NotFoundError and the pipeline returned context-only answers (answer null).
  `.env` updated, container recreated with the BUG-021 fix baked in.
- 2026-07-12: **baseline recorded** (excerpt corpus, Combo D, qwen36-35b-a3b-fp8,
  grounding strict; full report in vektra-internal
  `stack/20260712-sprint3-baseline-eval.md`):
  - retrieval (`/api/v1/search` hybrid, top_k=5): hit rate 100% (46 scored),
    MRR 0.8957, precision@5 0.3261; MRR factual 0.8810 / multi-chunk 0.8200 /
    reasoning 0.9667; EN 0.8595 / IT 0.9115; RRF score p50 0.50. Hit rate
    saturates on the tiny corpus — MRR/precision are the sensitive metrics.
  - e2e (`/api/v1/query`, 55 questions, 0 errors, 178s): grounded 38/55 (69%),
    no_context 17/55, answered-without-context 0, avg sources 1.3, latency
    p50 3069ms / p95 4371ms. By category: factual 19/21, reasoning 12/15,
    **multi-chunk 3/10**, adversarial 4/9 (adversarial no_ctx is largely the
    desired refusal).
  - reading: multi-chunk is the bottleneck — 7/10 end in no_context despite 100%
    retrieval hit: chunks are found by search but cut by rerank+threshold before
    the prompt (avg sources 1.3). Primary target for FEAT-017. Consider
    reingesting the full Costituzione PDF (97 chunks) for a more discriminative
    FEAT-017 comparison.
  - March numbers (factual 90 / reasoning 80 / multi-chunk 10) are not comparable:
    different pipeline (pre BUG-015/016/017, pre FEAT-020) and per-question
    results were never versioned (gitignored file, since overwritten).
- 2026-07-12: **FEAT-017 measured** (full corpus `eval-full`, Combo D, same
  dataset/config as baseline; corpus reingested with `dual` 500/100, parent 1500:
  105 points = 22 parents + 83 children, parents excluded from search).
  Baseline to compare (fixed 500/100, 78 chunks): retrieval hit 89.1% /
  MRR 0.8062 / P@5 0.3174; e2e grounded 35/55, multi-chunk 0/10, avg sources 1.4.
  - **Measure A** (dual + parent exclusion, expansion off): retrieval hit 82.6% /
    MRR 0.7029 / P@5 0.3130; e2e grounded 35/55 (64%), multi-chunk 1/10, avg
    sources 1.4, p50 3935ms. Dual chunking itself costs ~6.5pp hit rate on this
    corpus: children no longer roll overlap across parent boundaries, and 3
    questions whose keywords straddle a 1500-token section edge flip to miss
    (IT-F-13, IT-R-02, EN-F-03).
  - **Measure B** (expansion on): e2e grounded 35/55, multi-chunk 1/10, avg
    sources 1.0 (sibling merge working: same grounding with a more compact
    prompt), p50 3813ms. Zero per-question flips vs A. Expansion verified live
    in traces (`parent_expansion` step, children_expanded/parents_fetched).
  - **Root cause of the multi-chunk collapse found (and it is upstream of
    FEAT-017)**: 9/10 multi-chunk questions end with `retrieval_filter
    before=5 after=0` — bge-reranker-v2-m3 scores each partial-answer chunk
    of a comparative/multi-part question below `min_relevance_score` 0.15
    (MC-01 with eval mode: max rerank score 0.088 while the raw RRF candidate
    was 0.61). The whole candidate set is wiped before expansion can run.
    Filed as TECH-007. Parent expansion works as designed but cannot touch
    this failure mode; the Costituzione corpus also understates its benefit
    (short self-contained articles — see TECH-005 collection 1).
  - Local dev `.env` now: `VEKTRA_CHUNKING_STRATEGY=dual`,
    `VEKTRA_PARENT_CHILD_LEVELS=1`, `VEKTRA_PARENT_EXPANSION_ENABLED=true`,
    `VEKTRA_EVAL_MODE=true` (left on for FEAT-018 trace inspection).
