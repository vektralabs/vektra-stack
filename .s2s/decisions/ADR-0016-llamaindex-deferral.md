# ADR-0016: LlamaIndex deferral - direct RAG for Phase 1-2

**Status**: accepted (amended 2026-02-06)
**Date**: 2026-02-06
**Context**: Architectural review 2026-02-06, revised after feature-by-feature analysis

## Context

LlamaIndex was evaluated as an orchestrator for the RAG pipeline. LlamaIndex provides pre-built implementations of hybrid search, reranking, query transformation, response synthesis, and evaluation tools.

Vektra is designed as infrastructure, not a prototype. The RAG pipeline is abstracted behind the QueryPipeline Protocol (ADR-0014), with Phase 1 implementing `SimpleQueryPipeline` and Phase 2 adding `AdvancedQueryPipeline` with hybrid search, reranking, and query classification.

The question is whether LlamaIndex should be adopted for any phase of Vektra's development, and if so, for which features.

### LlamaIndex current state (February 2026)

Since LlamaIndex v0.10, the project follows a modular architecture: `llama-index-core` (~12 MB tarball) plus separate integration packages. This partially addresses the "monolithic dependency" concern.

However, LlamaIndex v0.14 introduced significant breaking changes: deprecated `QueryPipeline` in favor of Workflows, removed `AgentRunner`, `FunctionCallingAgent`, and `OpenAIAgent`, and changed the default chat engine. This confirms the version instability concern.

Core dependencies include: nltk (~130 MB with data), tiktoken, networkx, pillow, aiohttp, pydantic, sqlalchemy. Some overlap with Vektra's stack (pydantic, sqlalchemy, aiohttp), others do not. Total installed footprint with transitive dependencies: ~150-200 MB.

### Evaluation gap

The original analysis listed "QueryTrace + feedback API" as equivalent to LlamaIndex's evaluation framework. This was an oversimplification:

- **QueryTrace** (ARCH-041) captures performance metrics (timing, chunks, model) but does not evaluate response quality
- **Feedback API** (REQ-055) collects human feedback but does not perform automated evaluation
- Neither answers: "is the response faithful to the retrieved documents?" or "are the retrieved chunks relevant to the query?"

Standalone evaluation frameworks (RAGAS, DeepEval) address this gap without requiring LlamaIndex as a runtime dependency.

## Decision

LlamaIndex is **not adopted for Phase 1 and Phase 2**. RAG pipeline features (hybrid search, reranking, query routing, response synthesis) are implemented directly behind the QueryPipeline Protocol. RAG quality evaluation is addressed by standalone frameworks (RAGAS or DeepEval). Reassessment for Phase 3+ if sub-question decomposition or agentic RAG features are needed.

### Rationale

**1. Version instability (confirmed)**: LlamaIndex v0.14 deprecated QueryPipeline, removed three agent classes, and changed the default chat engine in a single minor release. For an infrastructure platform where downstream applications depend on API stability, this level of churn is incompatible with Vektra's stability goals (see R-01 in architecture.md).

**2. Dependency weight (partially confirmed)**: `llama-index-core` alone is ~12 MB, not "hundreds of MB". But with transitive dependencies (nltk, tiktoken, networkx, pillow), installed footprint is ~150-200 MB. On a 4 GB RAM target, this is significant. More importantly, transitive dependency conflicts are a real operational risk (see [issue #13441](https://github.com/run-llama/llama_index/issues/13441)).

**3. Debugging opacity (confirmed)**: LlamaIndex's Workflows system is event-driven with internal state machines. Stack traces traverse framework internals. For an infrastructure platform, transparent code paths remain essential.

**4. Abstraction mismatch**: Vektra's Protocol-based design (8 typed Protocols with dependency injection) and LlamaIndex's Workflows (event-driven, implicit state) are architecturally incompatible. Adopting LlamaIndex would require an adapter layer that adds complexity without eliminating our own abstractions.

### Feature-by-feature assessment

**Features where direct implementation is clearly better:**

| Feature | Direct effort | Why not LlamaIndex |
|---------|--------------|-------------------|
| Dense retrieval | ~50 lines | pgvector + SQL, trivial |
| Sparse retrieval (BM25) | ~100 lines | bm25s library (~100 KB) is lighter than LlamaIndex wrapper |
| Hybrid search (RRF) | ~200 lines | RRF algorithm is ~30 lines, rest is SQL integration |
| Cross-encoder reranking | ~100 lines | sentence-transformers (already in stack), LlamaIndex wraps same lib |
| Query classification | ~150 lines | LLM call with structured output, framework unnecessary |
| Prompt templates | ~100 lines | Jinja2, already in stack |
| Graceful degradation | ~100 lines | Vektra-specific logic (ARCH-043), no framework equivalent |
| QueryTrace/observability | ~200 lines | Our design (ARCH-041) is cleaner than LlamaIndex's callback chain |

**Features where standalone alternatives are better than LlamaIndex:**

| Feature | Standalone alternative | Why better |
|---------|----------------------|-----------|
| RAG evaluation (faithfulness, relevancy) | RAGAS (Apache 2.0) or DeepEval | Focused, lightweight, reference-free, no runtime dependency, better debuggability |
| Semantic chunking | semantic-text-splitter (Rust-backed) | Lighter, faster, no framework overhead |

**Features where LlamaIndex has genuine value (Phase 3+ candidates):**

| Feature | Effort from scratch | LlamaIndex advantage |
|---------|-------------------|---------------------|
| Response synthesis (tree, refine, compact) | ~300-400 lines | Battle-tested edge case handling for long documents |
| Sub-question decomposition | ~400 lines | Orchestration of multiple sub-queries with synthesis |
| Agentic RAG (tool use in retrieval) | ~1000+ lines | Mature Workflows/Agent system |

### Evaluation strategy

Automated RAG quality evaluation will use a standalone framework, selected before Phase 2 implementation:

- **RAGAS**: Apache 2.0, reference-free evaluation, metrics (Faithfulness, Context Relevancy, Answer Relevancy, Context Recall, Context Precision). Framework-agnostic (works with dict/dataframe input).
- **DeepEval**: Pytest-style interface, CI/CD integration, debuggable metrics with explanations. Better for production workflows.

Both integrate with QueryTrace data (response_id, chunk references, model info) without requiring LlamaIndex as a runtime dependency.

**litellm is NOT affected**: litellm remains as the LLM abstraction layer (ADR-0008). This decision concerns only LlamaIndex.

## Options Considered

### LlamaIndex as mandatory dependency

**Pros**:
- Pre-built retrieval strategies
- Large ecosystem of integrations

**Cons**:
- ~150-200 MB installed footprint with transitive dependencies
- Breaking changes in minor releases (v0.14 deprecated QueryPipeline)
- Event-driven Workflows incompatible with Vektra's Protocol-based design
- Dependency conflicts documented in production use

### LlamaIndex as optional QueryPipeline implementation

**Pros**:
- Available for users who want it
- Behind Protocol, not mandatory

**Cons**:
- Still requires testing and maintaining compatibility across LlamaIndex releases
- Two pipelines to support increases surface area
- Adapter layer needed for Protocol-to-Workflows mapping

### Direct implementation + standalone evaluation (chosen)

**Pros**:
- Full control over code and debugging
- No framework dependency in runtime pipeline
- Transparent stack traces for operator diagnostics
- Stable APIs (Vektra controls the interface)
- Standalone evaluation tools (RAGAS/DeepEval) are better for quality assessment than LlamaIndex's built-in evaluators
- Lightweight standalone libraries (bm25s, sentence-transformers) cover retrieval needs

**Cons**:
- Must implement retrieval features directly (hybrid search, reranking, response synthesis)
- Advanced features (sub-question decomposition, agentic RAG) require significant effort if needed in Phase 3+

## Consequences

### Positive

- No framework dependency in the runtime pipeline
- Transparent, debuggable RAG pipeline code
- Stable internal APIs independent of external framework releases
- Standalone evaluation frameworks provide better quality assessment than LlamaIndex built-in tools
- Lower container image size (~150-200 MB saved)
- No risk of transitive dependency conflicts

### Negative

- Phase 2 must implement hybrid search, reranking, response synthesis directly (~1000 lines total)
- If Phase 3+ requires sub-question decomposition or agentic RAG, significant effort or framework adoption needed (reassessment point)

### Phase 3+ reassessment criteria

Revisit this decision if any of the following apply:
- Vektra requires sub-question decomposition for complex multi-source queries
- Agentic RAG (tool use within retrieval) becomes a requirement
- LlamaIndex achieves API stability (no breaking changes across 3+ minor releases)
- A lighter RAG framework emerges that aligns with Protocol-based design

### Phase 3+ framework landscape (February 2026)

Beyond LlamaIndex, two other RAG frameworks were assessed for potential Phase 3+ adoption as internal QueryPipeline implementations:

**Haystack (deepset)** - preferred candidate if framework adoption becomes necessary:
- Pipeline-based architecture (DAG), closely aligned with Vektra's QueryPipeline model
- Typed components with declared inputs/outputs, similar to our Protocol contracts
- Production-focused (deepset is an enterprise RAG company)
- Serializable pipelines, deployable via REST (Hayhooks)
- Multi-query retrieval (QueryExpander + MultiQueryRetriever) built-in
- Haystack 2.x has reached architectural stability

Integration consideration: if Haystack manages its own retrievers/embedders internally, there is duplication with our EmbeddingProvider/VectorStoreProvider. Two integration strategies exist: (a) Haystack wraps our Protocols (Haystack as orchestrator, Vektra Protocols as components) preserving our contracts but adding indirection, or (b) Haystack uses its own adapters (Haystack as opaque pipeline) reducing indirection but bypassing our Protocol layer.

**LangChain / LangGraph** - assessed in detail (February 2026, see design-vektra-integration-6.md):

LangChain core (v1.2.9): not recommended for any phase. Feature-by-feature analysis shows near-total overlap with Vektra's 9 Protocol interfaces (6 of 9 Protocols have LangChain equivalents that add no value over direct implementation). LangChain's abstractions (BaseChatModel, Embeddings, VectorStore, Document Loaders) duplicate what litellm, sentence-transformers, and pgvector already provide with less indirection. Specific Vektra features (token budget allocation ARCH-055, retrieval quality controls ARCH-056, prompt template versioning ARCH-054, SafeguardHook with content modification ARCH-049) have no LangChain equivalent.

Additional concerns:
- langsmith (observability SaaS client) is a hard dependency of langchain-core, conflicting with on-premises deployment requirements
- ~80-150 MB installed footprint with transitive dependencies (numpy, SQLAlchemy, aiohttp)
- Community trust remains low: 45% of developers who experiment with LangChain never use it in production (2025 survey)

LangGraph (v1.0.8): candidate for Phase 3+ agentic RAG only. Unlike LangChain core, LangGraph operates at a level ABOVE the RAG pipeline, not inside it. It would orchestrate when and how to call QueryPipeline.execute(), not replace the pipeline logic. Key capabilities relevant to Vektra:
- Stateful graphs with cycles (query refinement loops)
- Human-in-the-loop with automatic checkpointing
- Multi-agent orchestration (supervisor, hierarchical patterns)
- Fault recovery with state persistence

Integration path: Vektra's QueryPipeline becomes a "tool" in a LangGraph StateGraph. The adapter is ~20 lines wrapping execute() into a graph node. No architectural changes needed now: QueryResponse/QueryTrace (Pydantic models) are directly compatible with LangGraph's typed state, and EventEmitter (ARCH-038) provides observation hooks.

Note: langgraph (~158 KB) depends on langchain-core (~496 KB) but not the full langchain package. A minimal adoption would pull ~650 KB of framework code plus langsmith. The langsmith hard dependency remains the primary friction point for on-premises deployment.

**Framework adoption summary for Phase 3+:**

| Scenario | Candidate | Rationale |
|----------|-----------|-----------|
| Advanced deterministic pipeline | Haystack | DAG architecture aligns with QueryPipeline, typed components similar to Protocols |
| Agentic RAG (cycles, tool use) | LangGraph | Only mature option for stateful agent workflows with persistence |
| Sub-question decomposition | Haystack or direct | Manageable complexity (~400 lines) without framework |
| Response synthesis (long docs) | LlamaIndex or direct | LlamaIndex has battle-tested edge case handling |

Haystack and LangGraph are complementary, not alternatives: Haystack for pipeline internals, LangGraph for orchestration above the pipeline.
