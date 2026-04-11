# Implementation Plan: RAG retrieval quality improvements

**ID**: 20260325-rag-retrieval-quality
**Status**: completed
**Branch**: fix/conversation-persistence
**PR**: #52 (merged 2026-04-03)
**Created**: 2026-03-24T00:00:00Z
**Updated**: 2026-04-03T18:00:00Z

## Traceability

**Source**: BUG-015, BUG-016, TECH-002, DEBT-010
**Source Type**: backlog
**Analysis**: `vektra-internal/stack/20260324-reranker-threshold-gap-analysis.md`

## References

### Architecture
- ARCH-056: Retrieval quality controls in QueryPipeline @.s2s/architecture.md
- ARCH-036: RerankerService abstraction @.s2s/architecture.md

### Decisions
- ADR-0021: Retrieval quality controls @.s2s/decisions/ADR-0021-retrieval-quality-controls.md
- ADR-0019: Three-tier RAG evaluation strategy @.s2s/decisions/ADR-0019-rag-evaluation-strategy.md

## Overview

Four interconnected issues affecting retrieval quality, discovered during
conversation analysis (conv `5bf50682`, namespace `ita-100`):

1. **BUG-015**: Reranker scores discarded after reranking. The threshold filter
   applied to cosine similarity scores, not reranker scores, making the reranker
   and threshold effectively disconnected.
2. **BUG-016**: Default reranker (ms-marco-MiniLM-L-12-v2) is English-only.
   Italian content produces random scores, degrading retrieval.
3. **TECH-002**: No automated evaluation harness. Component interactions never
   tested systematically, allowing BUG-015/016 to go undetected.
4. **DEBT-010**: Relevance threshold 0.3 was set for cosine similarity with
   the Phase 1 embedding model and never recalibrated.

## Tasks (all completed)

### BUG-015: Fix reranker score propagation
- [x] `RerankerService.rerank()` propagates reranker scores via `dataclasses.replace()`
- [x] Sigmoid normalization for cross-encoder scores (0-1 range)
- [x] `SearchResult.original_score` field preserves pre-reranker score
- [x] Commit: 838b40e

### BUG-016: Switch to multilingual reranker
- [x] Default changed to `cross-encoder/BAAI/bge-reranker-v2-m3` (568M params, 100+ languages)
- [x] Config: `VEKTRA_RERANK_PROVIDER=cross-encoder`, `VEKTRA_RERANK_MODEL=BAAI/bge-reranker-v2-m3`
- [x] Commit: 861eae8

### DEBT-010: Recalibrate threshold
- [x] Threshold lowered from 0.3 to 0.15 (safety net, top-k is primary control)
- [x] bge-m3 scores are bimodal: <0.15 (irrelevant) or >0.30 (relevant), no scores in gap
- [x] Commit: 861eae8

### TECH-002: Build evaluation harness
- [x] 55-question bilingual dataset (Italian Constitution + English source)
- [x] `eval_retrieval.py`: retrieval-only evaluation (no LLM)
- [x] `eval_e2e.py`: full pipeline evaluation with RAGAS metrics
- [x] `make eval-retrieval` and `make eval-e2e` targets
- [x] Baseline recorded for Combo D configuration
- [x] Commits: 3484d00, 54ee412, 08ccf95

## Key findings

- bge-reranker-v2-m3 scores are bimodal: either below 0.15 or above 0.30.
  Threshold 0.15 acts as pure safety net. Top-k is the effective control.
- Eval baseline: factual 90% hit rate, reasoning 80%, multi-chunk 10%.
- ms-marco-MultiBERT-L-12 (flashrank multilingual) scored 26.91 NDCG -- not viable.

## Spawned issues

- BUG-017: Context window fallback silently truncates prompt (litellm falls back
  to 4096 for unknown models). Discovered during same analysis session.
- DEBT-011: Conversation and query trace observability gaps (led to
  20260328-core-trace-observability plan).
