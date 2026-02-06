# ADR-0019: Three-tier RAG evaluation strategy

**Status**: accepted
**Date**: 2026-02-06
**Context**: Integration readiness analysis 2026-02-06

## Context

ARCH-046 (LlamaIndex deferral) identified RAG quality evaluation as a gap that standalone frameworks (RAGAS or DeepEval) should address. QueryTrace (ARCH-041) captures performance metrics (timing, chunk scores, model info) but does not evaluate response quality - it cannot answer "is the response faithful to the retrieved documents?" or "are the retrieved chunks relevant to the query?".

Both RAGAS and DeepEval require access to three text inputs:

| Input | Where it lives in Vektra | GDPR constraint |
|-------|-------------------------|----------------|
| Question (query text) | Transient in pipeline, not persisted | REQ-051: operator cannot access query content |
| Answer (response text) | Encrypted via pgcrypto (ARCH-031) | Not accessible post-query without decryption key |
| Contexts (chunk texts) | Transient in pipeline | Only chunk_id + score in QueryTrace |

QueryTrace deliberately excludes these texts for GDPR compliance (REQ-051). This creates a tension: evaluation frameworks need text data that the system is designed not to persist.

### RAGAS capabilities

RAGAS (Apache 2.0) is a standalone RAG evaluation framework:
- Reference-free: does not require gold-standard answers for every query
- Metrics: Faithfulness, Context Relevancy, Answer Relevancy, Context Recall, Context Precision
- Testset generation: builds knowledge graph from documents, synthesizes diverse questions (reasoning, conditioning, multi-context) via evolutionary generation
- Integration: accepts dict/DataFrame input, no framework lock-in

### DeepEval capabilities

DeepEval is a pytest-style LLM evaluation framework:
- CI/CD integration: runs as pytest tests, natural fit for CI pipelines
- Synthesizer class: generates synthetic datasets when production data is unavailable or privacy-restricted
- Debuggable metrics: explanations for each score, not just numbers
- No framework lock-in: accepts plain Python objects

## Decision

Adopt a three-tier evaluation strategy that respects GDPR constraints at each tier:

### Tier 1 - CI: synthetic test suite (regression gate)

RAGAS or DeepEval generates a synthetic test dataset from ingested documents. This runs in CI on every PR/merge as a quality regression gate.

- Input: synthetic questions + expected contexts generated from test documents
- RAG pipeline executes queries, evaluation framework scores results
- Threshold: configurable minimum scores (e.g., faithfulness > 0.7, relevancy > 0.6)
- No real user data involved - only synthetic questions from test corpus
- Catches regressions: prompt template changes, chunking strategy changes, model swaps

### Tier 2 - Staging: evaluation mode (periodic batch assessment)

Configuration flag `VEKTRA_EVAL_MODE=true` enables temporary text capture in a dedicated buffer for batch evaluation. Not active in production.

- Captures question, answer, and context texts for a configurable sample of queries
- Buffer is ephemeral: auto-purged after evaluation batch completes
- Runs on staging environment with anonymized or consented test data
- Evaluates with real pipeline behavior (models, configs, prompts) against realistic queries
- Frequency: before major releases or configuration changes

### Tier 3 - Production: metrics-only (continuous monitoring)

QueryTrace captures timing, scores, and metadata without text content. Feedback via response_id/citation_id (REQ-055) provides human quality signals.

- No query/response text persisted (REQ-051 compliance)
- Metrics: latency percentiles, chunk relevance score distribution, model usage, error rates
- Feedback: binary signals (thumbs up/down) correlated with QueryTrace metadata
- Detects: latency degradation, score distribution shifts, model failures
- Does not detect: faithfulness or relevancy degradation (that's what Tier 1 and 2 cover)

## Options considered

### Three-tier hybrid (chosen)

**Pros**:
- GDPR compliant at every tier
- Synthetic tests catch regressions automatically in CI
- Staging evaluation uses real pipeline with controlled data
- Production has zero text storage overhead
- Each tier covers what the others cannot

**Cons**:
- Tier 1 synthetic tests may not represent real query distribution ("over-reliance on synthetic data risks creating a feedback loop where the RAG system performs well on artificial examples but fails in production")
- Tier 2 requires a staging environment and operational overhead
- Gap between synthetic and real query quality remains

### Inline evaluation (Pattern A)

Evaluation framework runs inside the pipeline, scoring every query before texts are discarded.

**Pros**:
- Evaluates real production traffic
- No separate evaluation infrastructure

**Cons**:
- +500ms to 2s latency per query (LLM judge call)
- LLM cost for every query (judge model invocation)
- Blocks response delivery on evaluation completion
- Evaluation failures could impact user experience

### Evaluation mode only (Pattern B)

All evaluation happens in a special mode. No CI synthetic tests, no production metrics.

**Pros**:
- Simpler: one evaluation mechanism

**Cons**:
- No automated regression detection in CI
- Evaluation only happens when someone remembers to run it
- No continuous production monitoring

### Synthetic test suite only (Pattern C)

Only CI synthetic tests. No staging evaluation, no production metrics.

**Pros**:
- Simplest: fully automated, no operational overhead

**Cons**:
- Synthetic queries diverge from real usage patterns
- No way to detect production-specific issues (model config, data drift)
- "Quality theater": passing synthetic tests while real quality degrades

## Consequences

### Positive

- GDPR-compliant evaluation at every tier (no text persistence in production)
- CI regression gate prevents quality degradation on code changes
- Staging catches issues that synthetic tests miss
- Production metrics provide continuous operational monitoring
- Feedback API (REQ-055) closes the loop with human signals
- Pattern is framework-agnostic: works with RAGAS, DeepEval, or future alternatives

### Negative

- Requires maintaining a synthetic test dataset (generated from test documents, needs refresh when test corpus changes)
- Staging tier adds operational complexity (dedicated environment, anonymized data, evaluation scheduling)
- No real-time faithfulness monitoring in production (only proxy metrics via QueryTrace scores)
