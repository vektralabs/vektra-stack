# ADR-0023: Conversational query rewriting in AdvancedQueryPipeline

**Status**: proposed
**Date**: 2026-02-26
**Context**: Manual testing of Phase 1 multi-turn conversations (Aesop's fables corpus)

## Context

Phase 1 `SimpleQueryPipeline` embeds the user's literal query and runs vector search on that embedding. Conversation history is injected into the LLM prompt (via conversation.j2) but plays no role in retrieval. This works for single-turn queries but degrades in multi-turn conversations when the user refers to previous context.

Observed failure modes (tested with `make query` on an Italian Aesop's fables document):

1. **Anaphoric references fail retrieval**: "Quale di questi è il più pauroso?" - "questi" refers to animals listed in the previous answer. The embedding of the literal query has no semantic connection to those animals, so vector search returns unrelated chunks.
2. **Demonstratives are unresolvable**: "Intendevo tra gli animali che mi hai detto prima" - even explicit references to prior context do not improve retrieval because the embedding model sees only the current query.
3. **Ellipsis is lost**: follow-up questions that omit the subject ("E il più coraggioso?") embed as generic queries with low discriminative power.

These are not bugs. They are a structural consequence of the Phase 1 design: retrieval is query-only, context-unaware. The AdvancedQueryPipeline (ADR-0014) was planned for Phase 2 with query classification, hybrid search, and reranking, but it does not include a query resolution step.

## Decision

Add a **query rewriting step** (ARCH-061) as step 0 of the AdvancedQueryPipeline. Before embedding, the pipeline uses a single LLM call to rewrite the user's query into a self-contained form that resolves all references using conversation history.

### Pipeline flow (AdvancedQueryPipeline, updated)

```
Step 0: query_rewrite (new)
  - Input:  question + last N conversation turns
  - Action: LLM call with rewrite.j2 template
  - Output: rewritten self-contained query
  - Skip:   when conversation_id is None or history is empty

Step 1: query_classify (existing from ADR-0014)
  - Now operates on the rewritten query

Step 2: embed_query
  - Now embeds the rewritten query

Step 3-7: (unchanged) vector_search, rerank, build_prompt, llm_call, safeguard
```

### Rewriting template (`rewrite.j2`)

```jinja2
Given the following conversation history and a new user question, rewrite the
question so it is self-contained - a reader with no prior context should fully
understand what is being asked.

Rules:
- Replace all pronouns, demonstratives, and references with their concrete antecedents
- Preserve the original language of the question
- If the question is already self-contained, return it unchanged
- Output ONLY the rewritten question, nothing else

Conversation history:
{% for turn in history %}User: {{ turn.question }}
Assistant: {{ turn.answer }}
{% endfor %}

New question: {{ question }}

Rewritten question:
```

### Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `VEKTRA_QUERY_REWRITE_ENABLED` | `true` | Enable/disable query rewriting |

Rewriting is enabled by default in AdvancedQueryPipeline and unavailable in SimpleQueryPipeline. Operators who prioritize latency over conversational quality can disable it.

### Observability

The rewriting step emits a `StepTrace` with:
- `name`: `"query_rewrite"`
- `duration_ms`: LLM call latency
- `metadata`: `{"original_query": "...", "rewritten_query": "...", "history_turns_used": N}`

This enables correlation between rewriting quality and overall response quality via QueryTrace (ARCH-041).

## Options considered

### No rewriting (status quo)

**Pros**:
- No additional latency
- Simpler pipeline

**Cons**:
- Multi-turn conversations degrade after the first turn
- Users must rephrase questions manually to get good retrieval
- Violates user expectations for conversational interaction

### Rewriting via LLM call (chosen)

**Pros**:
- Handles pronouns, demonstratives, ellipsis, and implicit references
- Language-agnostic (works for Italian, English, any language the LLM supports)
- One additional LLM call (~200-500ms), skipped for single-turn queries
- Integrates cleanly as a pre-retrieval step in AdvancedQueryPipeline
- Observable via StepTrace

**Cons**:
- Additional LLM call adds latency (~200-500ms per conversational query)
- LLM may occasionally alter query intent during rewriting
- Requires a separate template to maintain

### Embedding-level fusion (concatenate history with query before embedding)

**Pros**:
- No additional LLM call
- Simple implementation

**Cons**:
- Embedding models have token limits (MiniLM: 256 tokens, e5-large: 512)
- Concatenation pollutes the embedding with irrelevant context
- Does not resolve pronouns - the embedding model does not "understand" coreference
- Quality is worse than LLM-based rewriting in practice

### HyDE (Hypothetical Document Embeddings)

**Pros**:
- LLM generates a hypothetical answer, then embeds that instead of the query
- Can improve retrieval for abstract or vague queries

**Cons**:
- Does not solve the anaphoric reference problem (the hypothetical answer may also contain unresolved references)
- Heavier than simple rewriting (LLM generates a full paragraph, not a short query)
- Better suited as a complementary technique, not a replacement for rewriting

## Consequences

### Positive

- Multi-turn conversations produce relevant retrieval from the second turn onward
- No change to SimpleQueryPipeline (Phase 1 behavior preserved)
- Observable: original and rewritten queries recorded in QueryTrace
- Configurable: operators can disable for latency-sensitive use cases
- Language-agnostic: works for any language the LLM supports

### Negative

- One additional LLM call per conversational query (~200-500ms)
- New template (rewrite.j2) to maintain alongside system.j2, context.j2, conversation.j2
- Risk of intent drift: LLM may subtly alter query meaning during rewriting
- Testing requires multi-turn fixtures (more complex test setup)
