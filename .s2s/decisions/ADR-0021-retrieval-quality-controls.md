# ADR-0021: Retrieval quality controls in QueryPipeline

**Status**: accepted
**Date**: 2026-02-07
**Context**: Pipeline quality analysis 2026-02-07

## Context

SimpleQueryPipeline (ARCH-036) performs "embed -> search -> prompt -> LLM". The search step always returns top_k results regardless of quality. This creates three problems:

### Problem 1: No minimum quality floor

VectorStoreProvider.search() returns top_k chunks even when all scores are low. A query about a topic not covered by indexed documents produces 5 irrelevant chunks. The LLM synthesizes an answer from this irrelevant context, producing hallucination presented with source citations. This is worse than no answer: the user trusts the citations.

Example: index contains only math documents, query is "who won the election?" Dense search returns the 5 least-distant chunks (still irrelevant). LLM produces a plausible-looking but fabricated answer.

### Problem 2: Overlap redundancy

FixedSizeChunking (REQ-016: 1000 tokens, 200 overlap) creates chunks that share 200 tokens with their neighbors. When adjacent chunks from the same document both rank in top-5, the prompt contains ~200 redundant tokens. With 5 chunks from a linear document, worst case is 800 wasted tokens out of 5000 - significant for small context windows (4K-8K).

### Problem 3: No "I don't know" path

ARCH-043 defines graceful degradation for LLM failure (fallback model, context-only response). No equivalent exists for retrieval failure (no relevant chunks). The system should be able to say "no relevant information found" instead of forcing synthesis from irrelevant context.

## Decision

Add a **retrieval_filter** step in SimpleQueryPipeline between vector_search and build_prompt. Three controls:

### 1. Minimum relevance threshold

```
VEKTRA_MIN_RELEVANCE_SCORE=0.3    # default, configurable
```

Chunks with score below threshold are excluded from prompt construction. The default (0.3 cosine similarity with all-MiniLM-L6-v2) is conservative - filters only noise. Operators can raise it for stricter quality.

### 2. Overlap deduplication

When two chunks from the same document have adjacent positions (distance < chunk_size based on ChunkMetadata.position), the lower-scored chunk is replaced by the next candidate in ranking. Uses existing metadata fields, no additional cost.

```
VEKTRA_CHUNK_DEDUP_ENABLED=true    # default
```

### 3. No-relevant-context detection

When no chunk passes the threshold, QueryResponse includes:

```python
no_relevant_context: bool = False    # True when no chunk passes threshold
```

This is orthogonal to `context_only` (ARCH-043):
- `context_only=True, no_relevant_context=False`: LLM down, relevant chunks available
- `no_relevant_context=True, context_only=False`: LLM available, no relevant chunks
- Both True: nothing available, empty response with explanation

ARCH-043 is amended to include this as a second degradation path.

### Diagnostics

A retrieval_filter StepTrace records:

```python
{
    "chunks_input": 5,
    "min_score": 0.12,
    "max_score": 0.87,
    "threshold_applied": 0.3,
    "chunks_above_threshold": 3,
    "chunks_deduplicated": 1,
    "chunks_output": 2,
    "no_relevant_context": false
}
```

## Options considered

### Filter in QueryPipeline (chosen)

Filtering between retrieval and prompt construction, inside the pipeline.

**Pros**:
- Pipeline controls its own quality
- VectorStoreProvider stays simple (returns results, doesn't judge quality)
- Different pipeline implementations can have different filter strategies
- Threshold configurable independently of vector store

**Cons**:
- Extra step in pipeline (~1ms, negligible)
- Score interpretation depends on embedding model and distance metric (0.3 means different things for different models)

### Filter in VectorStoreProvider

Push threshold into VectorStoreProvider.search() as a parameter.

**Pros**:
- Fewer results transferred from provider to pipeline
- Provider can optimize (e.g., Qdrant score_threshold parameter)

**Cons**:
- Mixes quality policy with storage contract
- VectorStoreProvider Protocol would need a new parameter (breaks separation of concerns)
- Cannot do overlap deduplication (provider doesn't know chunking strategy)
- Score threshold semantics vary by provider (Qdrant uses different scale than pgvector cosine)

### No filter (let LLM decide)

Send all top_k chunks to the LLM, let it judge relevance.

**Pros**:
- Simpler pipeline
- LLM can sometimes extract value from tangentially relevant chunks

**Cons**:
- Wastes context window on irrelevant content
- LLM hallucination risk increases with low-quality context
- No "I don't know" path - LLM always synthesizes an answer
- Higher latency and cost (more tokens processed by LLM)

## Consequences

### Positive

- System can decline to answer when context is irrelevant (reduces hallucination)
- Overlap deduplication frees context window for more useful content
- Operators can tune quality/recall trade-off via threshold
- QueryTrace provides full diagnostic chain (what was retrieved, what was filtered, why)
- Forward-compatible: no_relevant_context field can evolve into confidence tiers (OQ-014)

### Negative

- Threshold too high risks filtering valid chunks (mitigated: conservative default 0.3, configurable)
- Score interpretation varies by embedding model (mitigated: operator tunes after evaluation)
- Deduplication heuristic based on position index may miss non-adjacent overlapping chunks (acceptable: rare case with fixed-size chunking)
