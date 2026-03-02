# Wave boundary checklist — SPV Level 5

**Wave**: 1
**Plans entering this wave**: core-conversations, admin-enforcement, index-hybrid, ingest-phase2
**Date**: 2026-03-02 (retroactive, filled after implementation of 57/77 tasks)
**Reviewer**: Claude Opus 4.6

---

Instructions: answer each item YES / NO / N/A.
- A NO requires a **required action** listed at the bottom before proceeding.
- All items must be YES or N/A to mark the wave as READY.
- Run automated checks (J, K, L) as actual commands, not estimates.

---

## A — Ownership completeness

Every shared artifact written across plans has exactly one owning plan.

| # | Question | Answer | Note |
|---|----------|--------|------|
| A1 | Does every new class or module have exactly one plan that creates it? | YES | PersistentConversationStore/FeedbackOrm → core-conversations; RateLimiter/TTLCache → admin-enforcement; BM25SparseProvider/QdrantProvider/ReindexService → index-hybrid; UnstructuredExtractor/DualStrategyChunking → ingest-phase2 |
| A2 | Does every `ProviderRegistry.register()` call have exactly one plan that performs it? | YES | New providers defined by component plans; registration at startup owned by infra-phase2 (Wave 5) |
| A3 | Does every middleware component have one plan that implements it and a (possibly different) plan that registers it on the app? | YES | RateLimiter integrated in require_scope(), not as middleware. AuditMiddleware unchanged from Phase 1. |
| A4 | Is `vektra_shared/audit.py` creation assigned to exactly one plan (not duplicated)? | N/A | Phase 1 artifact, not modified in Wave 1 |

## B — Assembly conflicts

No component plan assumes another plan has wired shared infrastructure on its behalf.

| # | Question | Answer | Note |
|---|----------|--------|------|
| B1 | Is there a single "assembly" plan that owns all `ProviderRegistry.register()` calls at startup lifespan? | YES | infra-phase2 (Wave 5) owns startup assembly |
| B2 | Do all component plans avoid calling `ProviderRegistry.register()` in their own code (leaving this exclusively to the assembly plan)? | YES | Verified: no register() calls in any Wave 1 component code |
| B3 | Is `app.add_middleware(AuditMiddleware)` called by the assembly plan only? | YES | Phase 1 infra-app-entrypoint, unchanged |

## C — Background infrastructure

All background job infrastructure is explicitly assigned to a plan.

| # | Question | Answer | Note |
|---|----------|--------|------|
| C1 | Is the arq worker service (CMD_TARGET=worker) assigned to a specific plan (infra-docker)? | YES | infra-phase2 (Wave 5) |
| C2 | Are all arq task function definitions (`@arq` decorated) listed in a specific plan? | YES | Reindex job → index-hybrid T16-T17 |
| C3 | Is the `docker/entrypoint.sh` single-image two-role pattern documented in a plan? | YES | infra-phase2 (Wave 5) |

## D — Import boundary enforcement

No plan imports from a sibling component at the module level (ADR-0005).

| # | Question | Answer | Note |
|---|----------|--------|------|
| D1 | Do all cross-component calls go through `vektra_shared` protocols or ProviderRegistry? | YES | All Wave 1 modules use shared types and config only |
| D2 | Is import-linter configured to enforce component boundaries for all packages in this wave? | YES | Phase 1 configuration still active, covers all packages |
| D3 | Are there no new circular import paths introduced by this wave? | YES | All new modules follow existing unidirectional import pattern |

## E — Decision closure

No open design decisions block implementation of Wave N plans.

| # | Question | Answer | Note |
|---|----------|--------|------|
| E1 | Are all "TBD", "TODO", and open questions in Wave N plans resolved? | YES | All 4 plan files checked, no open questions |
| E2 | Is the health aggregator strategy explicitly decided (ProviderRegistry "health/*" keys — NOT direct module calls)? | YES | Phase 1 decision, unchanged |
| E3 | Is the key cache invalidation strategy fully specified (immediate update on create/revoke + TTL as fallback)? | YES | TTLCache(maxsize=512, ttl=300) with immediate invalidation on create/revoke (admin-enforcement) |

## F — Cache write completeness

Every write path that affects a cache has an explicit cache-update step.

| # | Question | Answer | Note |
|---|----------|--------|------|
| F1 | Does `create_key()` update the in-memory key cache immediately (not waiting for TTL reload)? | YES | Implemented in admin-enforcement |
| F2 | Does `revoke_key()` mark the key revoked in the cache immediately? | YES | Implemented in admin-enforcement |
| F3 | Does startup load all non-revoked keys into the cache before the first request is served? | YES | Phase 1 behavior, unchanged |

## G — Protocol contracts

Every Protocol interface used in this wave has a concrete registered implementation.

| # | Question | Answer | Note |
|---|----------|--------|------|
| G1 | Is `VectorStoreProvider` backed by `VectorStoreServiceAdapter` registered at startup? | YES | Phase 1 (pgvector). QdrantProvider added as alternative in index-hybrid. |
| G2 | Is `EmbeddingProvider` backed by `SentenceTransformersProvider` registered at startup? | YES | Phase 1, unchanged |
| G3 | Is `LLMProvider` backed by `LitellmProvider` registered at startup? | YES | Phase 1, unchanged |
| G4 | Do all `SafeguardHook` implementations return `SafeguardResult` (not `bool`)? | YES | No changes in Wave 1 |
| G5 | Is `QueryPipeline.execute()` signature consistent with the Protocol definition in vektra_shared? | YES | No changes in Wave 1 |

## H — Test infrastructure

Test infrastructure for all Wave N plans is in place.

| # | Question | Answer | Note |
|---|----------|--------|------|
| H1 | Do all async test files have `asyncio_mode = "auto"` in their pytest config? | YES | All pyproject.toml files set asyncio_mode = "auto" |
| H2 | Do integration tests requiring PostgreSQL use testcontainers (not a shared external DB)? | YES | test_integration.py uses testcontainers PostgresContainer |
| H3 | Do session fixtures create a per-test async engine (no asyncpg event-loop contamination)? | YES | fresh_engine fixture creates per-test engine with dispose() |
| H4 | Is there at least one test per acceptance criterion for each Wave N plan? | YES | 12 new test files across 4 plans covering all acceptance criteria |

## I — NFR operationalization

All NFRs targeted by Wave N plans have explicit, runnable measurement methods.

| # | Question | Answer | Note |
|---|----------|--------|------|
| I1 | Is NFR-007 (100% audit coverage) tested with an integration test that counts `audit_log` rows? | N/A | Phase 1 test, not targeted by Wave 1 plans |
| I2 | Is NFR-004 (startup < 60s) measured with a timer from process start to first `/health` 200? | N/A | Phase 1 test, not targeted by Wave 1 plans |
| I3 | Is NFR-009 (100% error actionability) enforced by the feature-error-codes registry at CI time? | YES | New error codes ERR-AUTH-004 and ERR-QUOTA-001 added to registry with remediation text |
| I4 | Is NFR-003 (ingest < 30s for 10-page PDF) measured by a timed benchmark test? | N/A | Phase 1 test, not targeted by Wave 1 plans |

## J — RTM coverage (automated)

Run: `uv run python .s2s/scripts/verify_traceability.py`

| # | Question | Answer | Note |
|---|----------|--------|------|
| J1 | Script exits 0 (PASS)? | YES | `PASS: 79 entries (5 BR, 13 NFR, 61 REQ) -- no gaps` |
| J2 | Every REQ/NFR covered by Wave N plans has a non-empty `plan_task` field? | YES | All plan_task fields populated |

## K — Provides/requires (automated)

Run: `uv run python .s2s/scripts/verify_provides_requires.py`

| # | Question | Answer | Note |
|---|----------|--------|------|
| K1 | Script exits 0 (PASS — no GAP, CONFLICT, or ORDER violations)? | YES | `PASS: 5 plans with provides/requires, 14 tokens provided, 4 requires -- all satisfied` |
| K2 | All Wave N plans that produce shared artifacts have provides_requires front-matter? | YES | All 4 Wave 1 plans have provides_requires YAML front-matter |

## L — State machine completeness (automated)

Run: `uv run python .s2s/scripts/verify_state_machines.py`

| # | Question | Answer | Note |
|---|----------|--------|------|
| L1 | Script exits 0 (PASS)? | YES | `PASS: 5 state machine(s) -- all matrices complete` |
| L2 | Every new stateful component introduced in Wave N is defined in `.s2s/state-machines.yaml`? | YES | conversation state machine pre-existed; no new state machines in Wave 1 |
| L3 | Every FAILED terminal state has a documented recovery path (restart, admin action, or explicit "unrecoverable")? | YES | All pre-existing, verified complete |

---

## Summary

| Section | Items | YES | NO | N/A |
|---------|-------|-----|-----|-----|
| A Ownership | 4 | 3 | 0 | 1 |
| B Assembly | 3 | 3 | 0 | 0 |
| C Background | 3 | 3 | 0 | 0 |
| D Import boundary | 3 | 3 | 0 | 0 |
| E Decision closure | 3 | 3 | 0 | 0 |
| F Cache writes | 3 | 3 | 0 | 0 |
| G Protocol contracts | 5 | 5 | 0 | 0 |
| H Test infrastructure | 4 | 4 | 0 | 0 |
| I NFR measurement | 4 | 1 | 0 | 3 |
| J RTM | 2 | 2 | 0 | 0 |
| K Provides/requires | 2 | 2 | 0 | 0 |
| L State machines | 3 | 3 | 0 | 0 |
| **Total** | **39** | **35** | **0** | **4** |

## Required actions before proceeding

(none - all items YES or N/A)

## Status

**READY TO PROCEED**

Blocking items: none
