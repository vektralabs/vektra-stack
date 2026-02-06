# ADR-0016: LlamaIndex exclusion - direct RAG implementation

**Status**: accepted
**Date**: 2026-02-06
**Context**: Architectural review 2026-02-06

## Context

During the architectural review, LlamaIndex was evaluated as an orchestrator for the RAG pipeline. LlamaIndex provides pre-built implementations of hybrid search, reranking, query transformation, response synthesis, and evaluation tools.

Vektra is designed as infrastructure, not a prototype. The RAG pipeline is abstracted behind the QueryPipeline Protocol (ADR-0014), with Phase 1 implementing `SimpleQueryPipeline` and Phase 2 adding `AdvancedQueryPipeline` with hybrid search, reranking, and query classification.

The question is whether `AdvancedQueryPipeline` should wrap LlamaIndex or implement features directly.

## Decision

Exclude LlamaIndex from Vektra's dependency tree. Implement RAG pipeline features (hybrid search, reranking, query routing, confidence scoring) directly behind the QueryPipeline Protocol.

**Rationale**:

1. **Dependency weight**: LlamaIndex core + extras pulls hundreds of MB of dependencies. Vektra targets 4GB RAM deployments where every MB matters. litellm (~5MB) proves that lightweight abstractions are viable.

2. **Debugging opacity**: LlamaIndex stack traces traverse dozens of internal files. For an infrastructure platform where operators need to diagnose issues, transparent code paths are essential.

3. **Version instability**: LlamaIndex has a history of breaking API changes between minor versions. For a platform that downstream applications depend on, dependency stability is critical (see R-01 in architecture.md).

4. **Opinionated abstractions**: When the use case diverges from LlamaIndex's assumptions, developers fight the framework instead of solving the problem. Vektra's Protocol-based design is intentionally unopinionated.

5. **LLM-assisted development changes the equation**: The primary argument for LlamaIndex is "don't reinvent the wheel." But with LLM-assisted development, implementing hybrid search (~200 lines), cross-encoder reranking (~100 lines), and query classification (~150 lines) directly is a matter of hours, not weeks. The maintenance burden of direct code is lower than the maintenance burden of framework compatibility.

**What LlamaIndex provides that Vektra implements directly**:

| LlamaIndex feature | Vektra implementation | Protocol |
|--------------------|-----------------------|----------|
| Hybrid retrieval | pgvector dense + sparse + RRF fusion | VectorStoreProvider (SearchMode.HYBRID) |
| Cross-encoder reranking | sentence-transformers cross-encoder model | QueryPipeline step |
| Query transformation (HyDE) | Query classification + optional reformulation | QueryPipeline step |
| Response synthesis | Jinja2 templates with configurable strategies | QueryPipeline step |
| Evaluation tools | QueryTrace + feedback API | ARCH-041, REQ-055 |

**litellm is NOT affected**: litellm remains as the LLM abstraction layer (ADR-0008). litellm and LlamaIndex solve different problems; this decision excludes only LlamaIndex.

## Options Considered

### LlamaIndex as mandatory dependency

**Pros**:
- Pre-built retrieval strategies
- Large ecosystem of integrations
- Evaluation framework included

**Cons**:
- ~hundreds of MB dependency footprint
- Opaque debugging
- Version churn between releases
- Opinionated abstractions conflict with infrastructure goals
- Dependency on LlamaIndex release cycle for bug fixes

### LlamaIndex as optional QueryPipeline implementation

**Pros**:
- Available for users who want it
- Behind Protocol, not mandatory

**Cons**:
- Still requires testing and maintaining compatibility
- Two pipelines to support increases surface area
- Users who choose it inherit its debugging and version issues

### Direct implementation behind QueryPipeline (chosen)

**Pros**:
- Full control over code and debugging
- No external framework dependency
- Transparent stack traces
- Stable APIs (Vektra controls the interface)
- With LLM-assisted development, implementation cost is low

**Cons**:
- Must implement hybrid search, reranking, etc. from scratch
- No pre-built evaluation framework (but QueryTrace + feedback API provide the data)

## Consequences

### Positive

- No heavy framework dependency in the stack
- Transparent, debuggable RAG pipeline
- Stable internal APIs independent of external framework releases
- Operators can read and understand the pipeline code
- Lower container image size

### Negative

- Phase 2 development must implement hybrid search, reranking, query classification directly
- No access to LlamaIndex ecosystem integrations (mitigated by Protocol-based design allowing direct integration with external tools)
- Evaluation tools must be built from QueryTrace data rather than using LlamaIndex's evaluation framework
