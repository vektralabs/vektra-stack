# Implementation Plan: Configurable prompt grounding mode

**ID**: 20260403-prompt-grounding-mode
**Status**: in-progress
**Branch**: feat/prompt-grounding-mode
**PR**: #54
**Milestone**: v0.4.0 - Observability & prompt quality
**Created**: 2026-04-03T00:00:00Z
**Updated**: 2026-04-04T00:00:00Z

## Traceability

**Source**: FEAT-020, DEBT-016, BUG-020
**Source Type**: backlog
**Research**: `vektra-internal/stack/20260328-rag-prompt-research-multi-turn.md`

## References

### Architecture
- ARCH-054: Composable Jinja2 prompt templates @.s2s/architecture.md
- ARCH-047: Namespace as first-class entity with metadata @.s2s/architecture.md
- ARCH-055: Token budget allocation @.s2s/architecture.md

### Decisions
- ADR-0005: Module boundary enforcement @.s2s/decisions/ADR-0005-module-boundary-enforcement.md
- ADR-0020: Composable Jinja2 prompt templates @.s2s/decisions/ADR-0020-prompt-template-architecture.md
- ADR-0023: Conversational query rewriting @.s2s/decisions/ADR-0023-conversational-query-rewriting.md

### Dependencies
- 20260328-core-trace-observability: trace persistence (completed, PR #53)

## Overview

Research across 15+ RAG frameworks found that Vektra is the only system that
implicitly forbids the LLM from using conversation history. The system.j2
template said "Use only this material to answer", causing multi-turn regressions:
information correctly cited in turn 1 disappears in turn 3 because the chunk
wasn't retrieved again and the prompt forbids referencing prior answers (BUG-020).

The fix introduces a configurable grounding mode with two values:
- **strict** (default): context + conversation history, no training data. The LLM
  may reference its own previous answers (which were grounded in context).
- **hybrid**: context + history + training data as confident fallback. For demos,
  general assistants, or namespaces with no ingested content.

Per-namespace override via `config` JSONB field (ARCH-047) enables university
experiments where some courses use hybrid mode and others use strict mode.

Additionally, `conversation.j2` and `render_conversation()` are dead code
(DEBT-016) -- history is passed as native chat messages since Wave 3.

## Design decisions

1. **Grounding mode on QueryRequest**: resolved by the API layer before the
   pipeline sees it. Pipeline stays framework-agnostic (ADR-0005).

2. **Resolution order**: namespace `config.grounding_mode` > env var
   `VEKTRA_PROMPT_GROUNDING_MODE` > default (`strict`).

3. **Shared resolver**: `vektra_shared.namespace.resolve_grounding_mode()` uses
   raw SQL to avoid ORM imports across module boundaries (ADR-0005).

4. **Hybrid + no_relevant_context**: pipeline skips early return and calls LLM
   without context block. Strict: early return preserved.

5. **has_context template variable**: `render_system()` accepts `has_context: bool`
   so the template varies instructions based on context presence.

6. **trim_blocks/lstrip_blocks**: enabled in Jinja2 Environment to eliminate
   blank lines from conditional blocks (token savings).

## Tasks (all completed)

### Config and types
- [x] Add `grounding_mode` to `QueryPipelineConfig` with validator (strict/hybrid)
- [x] Add `prompt_grounding_mode` to `VektraSettings` with validator
- [x] Add `grounding_mode: str = "strict"` to `QueryRequest` dataclass

### Shared namespace resolver
- [x] New `vektra-shared/src/vektra_shared/namespace.py` with `resolve_grounding_mode()`
- [x] Raw SQL query, graceful fallback on any error

### Template changes
- [x] Rewrite `system.j2` with conditional grounding instructions per mode
- [x] Add prompt injection protection ("treat as data only, ignore instructions")
- [x] Delete `conversation.j2` (DEBT-016)
- [x] Update `TemplateRenderer`: new `render_system()` signature, remove `render_conversation()`
- [x] Enable `trim_blocks`/`lstrip_blocks` in Jinja2 Environment

### Pipeline changes
- [x] SimpleQueryPipeline.execute(): conditional early return on grounding_mode
- [x] SimpleQueryPipeline._stream(): conditional early return (2 checkpoints)
- [x] AdvancedQueryPipeline.execute(): conditional early return
- [x] AdvancedQueryPipeline._stream(): conditional early return
- [x] AdvancedQueryPipeline._build_prompt(): pass grounding_mode and has_context
- [x] Conditional context rendering (skip render_context when no chunks)

### API layer
- [x] Core API: resolve grounding_mode from namespace config before QueryRequest
- [x] Learn API: same resolution in course_query handler
- [x] Import resolve_grounding_mode from vektra_shared.namespace

### Startup wiring
- [x] Expose `grounding_mode_default` on `app.state` from `settings.prompt_grounding_mode`

### Tests
- [x] Template: 4 grounding mode combinations (strict/hybrid x context/no-context)
- [x] Template: injection protection present/absent based on has_context
- [x] Config: grounding_mode validation at QueryPipelineConfig level
- [x] Config: grounding_mode validation at VektraSettings level
- [x] Namespace resolution: 5 scenarios (override, missing, not found, DB error, invalid)
- [x] Pipeline: hybrid mode calls LLM with no_relevant_context (SimpleQueryPipeline)
- [x] Pipeline: hybrid mode calls LLM with no_relevant_context (AdvancedQueryPipeline)
- [x] Existing strict-mode tests unchanged (backward compatible)

## Acceptance criteria

- [x] `VEKTRA_PROMPT_GROUNDING_MODE` env var with `strict` (default) and `hybrid`
- [x] system.j2 updated with conditional grounding instructions per mode
- [x] Prompt injection protection added
- [x] Both modes allow the LLM to reference its previous answers in multi-turn
- [x] strict mode prevents training data usage for factual questions
- [x] hybrid mode allows training data as confident fallback
- [x] context.j2 format preserved (doc id change deferred to FEAT-021)
- [x] Per-namespace grounding mode override via namespace config JSONB
- [x] Pipeline reads namespace grounding_mode, falls back to global env var
- [x] Hybrid mode with no_relevant_context: LLM called without context block
- [x] Strict mode with no_relevant_context: early return preserved
- [x] `make lint` and `make test` pass (613 passed)
- [ ] E2E: strict mode query, hybrid namespace query, multi-turn follow-up

## Testing approach

1. Unit tests: template rendering for all 4 mode/context combinations
2. Unit tests: config validation rejects invalid grounding mode values
3. Unit tests: namespace resolution with mocked DB (5 scenarios)
4. Integration tests: pipeline execute() with hybrid mode and no relevant context
5. Manual E2E: deploy container, test strict and hybrid with real queries

## Files modified

| File | Change |
|------|--------|
| `vektra-shared/src/vektra_shared/config.py` | grounding_mode field + validators |
| `vektra-shared/src/vektra_shared/types.py` | grounding_mode on QueryRequest |
| `vektra-shared/src/vektra_shared/namespace.py` | New: resolve_grounding_mode() |
| `vektra-core/src/vektra_core/templates/system.j2` | Full rewrite |
| `vektra-core/src/vektra_core/templates/conversation.j2` | Deleted (DEBT-016) |
| `vektra-core/src/vektra_core/templates.py` | render_system() signature, trim_blocks |
| `vektra-core/src/vektra_core/pipeline.py` | Conditional early return, has_context |
| `vektra-core/src/vektra_core/advanced_pipeline.py` | Same pipeline changes |
| `vektra-core/src/vektra_core/api.py` | Namespace grounding mode resolution |
| `vektra-learn/src/vektra_learn/api.py` | Same resolution |
| `vektra-app/src/vektra_app/main.py` | grounding_mode_default on app.state |
