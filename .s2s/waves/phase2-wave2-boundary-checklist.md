# Wave boundary checklist — SPV Level 5

**Wave**: 2
**Plans entering this wave**: 20260301-core-pipeline-v2, 20260301-admin-ui
**Date**: 2026-03-05
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
| A1 | Does every new class or module have exactly one plan that creates it? | YES | core-pipeline-v2: AdvancedQueryPipeline, PresidioPIISafeguard, RerankerService, rewrite.j2. admin-ui: ui.py, templates/, static/. No overlap. |
| A2 | Does every `ProviderRegistry.register()` call have exactly one plan that performs it? | YES | Both plans defer registration to infra-phase2 (Wave 5 assembly plan) |
| A3 | Does every middleware component have one plan that implements it and a (possibly different) plan that registers it on the app? | N/A | No new middleware in Wave 2 |
| A4 | Is `vektra_shared/audit.py` creation assigned to exactly one plan (not duplicated)? | N/A | Already exists from Phase 1 |

## B — Assembly conflicts

No component plan assumes another plan has wired shared infrastructure on its behalf.

| # | Question | Answer | Note |
|---|----------|--------|------|
| B1 | Is there a single "assembly" plan that owns all `ProviderRegistry.register()` calls at startup lifespan? | YES | infra-phase2 (Wave 5) is the assembly plan |
| B2 | Do all component plans avoid calling `ProviderRegistry.register()` in their own code (leaving this exclusively to the assembly plan)? | YES | Both plans create classes but don't register them |
| B3 | Is `app.add_middleware(AuditMiddleware)` called by the assembly plan only? | N/A | No new middleware in Wave 2 |

## C — Background infrastructure

All background job infrastructure is explicitly assigned to a plan.

| # | Question | Answer | Note |
|---|----------|--------|------|
| C1 | Is the arq worker service (CMD_TARGET=worker) assigned to a specific plan (infra-docker)? | YES | No new arq tasks in Wave 2 |
| C2 | Are all arq task function definitions (`@arq` decorated) listed in a specific plan? | N/A | No new arq tasks in Wave 2 |
| C3 | Is the `docker/entrypoint.sh` single-image two-role pattern documented in a plan? | N/A | Already done in Phase 1, no changes in Wave 2 |

## D — Import boundary enforcement

No plan imports from a sibling component at the module level (ADR-0005).

| # | Question | Answer | Note |
|---|----------|--------|------|
| D1 | Do all cross-component calls go through `vektra_shared` protocols or ProviderRegistry? | YES | core-pipeline-v2 uses vektra_shared Protocols; admin-ui uses ProviderRegistry |
| D2 | Is import-linter configured to enforce component boundaries for all packages in this wave? | YES | vektra-core and vektra-admin already have import-linter rules |
| D3 | Are there no new circular import paths introduced by this wave? | YES | Both plans import from vektra_shared only |

## E — Decision closure

No open design decisions block implementation of Wave N plans.

| # | Question | Answer | Note |
|---|----------|--------|------|
| E1 | Are all "TBD", "TODO", and open questions in Wave N plans resolved? | YES | No TBD/TODO markers in either plan |
| E2 | Is the health aggregator strategy explicitly decided (ProviderRegistry "health/*" keys — NOT direct module calls)? | YES | Decided in Phase 1, admin-ui uses existing health endpoints |
| E3 | Is the key cache invalidation strategy fully specified (immediate update on create/revoke + TTL as fallback)? | YES | Implemented in Wave 1 (TTLCache 512/300s) |

## F — Cache write completeness

Every write path that affects a cache has an explicit cache-update step.

| # | Question | Answer | Note |
|---|----------|--------|------|
| F1 | Does `create_key()` update the in-memory key cache immediately (not waiting for TTL reload)? | YES | Implemented in Wave 1 |
| F2 | Does `revoke_key()` mark the key revoked in the cache immediately? | YES | Implemented in Wave 1 |
| F3 | Does startup load all non-revoked keys into the cache before the first request is served? | YES | Implemented in Wave 1 |

## G — Protocol contracts

Every Protocol interface used in this wave has a concrete registered implementation.

| # | Question | Answer | Note |
|---|----------|--------|------|
| G1 | Is `VectorStoreProvider` backed by `VectorStoreServiceAdapter` registered at startup? | YES | PgvectorProvider registered |
| G2 | Is `EmbeddingProvider` backed by `SentenceTransformersProvider` registered at startup? | YES | |
| G3 | Is `LLMProvider` backed by `LitellmProvider` registered at startup? | YES | |
| G4 | Do all `SafeguardHook` implementations return `SafeguardResult` (not `bool`)? | YES | PresidioPIISafeguard returns SafeguardResult per plan design |
| G5 | Is `QueryPipeline.execute()` signature consistent with the Protocol definition in vektra_shared? | YES | AdvancedQueryPipeline implements QueryPipeline Protocol |

## H — Test infrastructure

Test infrastructure for all Wave N plans is in place.

| # | Question | Answer | Note |
|---|----------|--------|------|
| H1 | Do all async test files have `asyncio_mode = "auto"` in their pytest config? | YES | Configured in pyproject.toml |
| H2 | Do integration tests requiring PostgreSQL use testcontainers (not a shared external DB)? | YES | Existing pattern, Wave 2 tests use mocks |
| H3 | Do session fixtures create a per-test async engine (no asyncpg event-loop contamination)? | YES | Existing pattern |
| H4 | Is there at least one test per acceptance criterion for each Wave N plan? | YES | Both plans include unit + integration tests covering all ACs |

## I — NFR operationalization

All NFRs targeted by Wave N plans have explicit, runnable measurement methods.

| # | Question | Answer | Note |
|---|----------|--------|------|
| I1 | Is NFR-007 (100% audit coverage) tested with an integration test that counts `audit_log` rows? | YES | Already tested in Phase 1 |
| I2 | Is NFR-004 (startup < 60s) measured with a timer from process start to first `/health` 200? | YES | Already measured in Phase 1 |
| I3 | Is NFR-009 (100% error actionability) enforced by the feature-error-codes registry at CI time? | YES | Already enforced |
| I4 | Is NFR-003 (ingest < 30s for 10-page PDF) measured by a timed benchmark test? | N/A | No ingest changes in Wave 2 |

## J — RTM coverage (automated)

Run: `uv run python .s2s/scripts/verify_traceability.py`

| # | Question | Answer | Note |
|---|----------|--------|------|
| J1 | Script exits 0 (PASS)? | YES | PASS: 79 entries, no gaps (48 WARN for missing test refs, non-blocking) |
| J2 | Every REQ/NFR covered by Wave N plans has a non-empty `plan_task` field? | YES | REQ-003,006,022,025,042,044,049,051,053,059,060,065 all traced |

## K — Provides/requires (automated)

Run: `uv run python .s2s/scripts/verify_provides_requires.py`

| # | Question | Answer | Note |
|---|----------|--------|------|
| K1 | Script exits 0 (PASS — no GAP, CONFLICT, or ORDER violations)? | YES | PASS: 5 plans, 14 tokens provided, 4 requires, all satisfied |
| K2 | All Wave N plans that produce shared artifacts have provides_requires front-matter? | YES | Both plans have provides/requires sections |

## L — State machine completeness (automated)

Run: `uv run python .s2s/scripts/verify_state_machines.py`

| # | Question | Answer | Note |
|---|----------|--------|------|
| L1 | Script exits 0 (PASS)? | YES | PASS: 5 state machines, all matrices complete |
| L2 | Every new stateful component introduced in Wave N is defined in `.s2s/state-machines.yaml`? | N/A | No new stateful entities (pipeline is stateless per request, UI is stateless) |
| L3 | Every FAILED terminal state has a documented recovery path (restart, admin action, or explicit "unrecoverable")? | YES | Graceful degradation matrix in core-pipeline-v2 covers all failure paths |

---

## Summary

| Section | Items | YES | NO | N/A |
|---------|-------|-----|-----|-----|
| A Ownership | 4 | 2 | 0 | 2 |
| B Assembly | 3 | 2 | 0 | 1 |
| C Background | 3 | 1 | 0 | 2 |
| D Import boundary | 3 | 3 | 0 | 0 |
| E Decision closure | 3 | 3 | 0 | 0 |
| F Cache writes | 3 | 3 | 0 | 0 |
| G Protocol contracts | 5 | 5 | 0 | 0 |
| H Test infrastructure | 4 | 4 | 0 | 0 |
| I NFR measurement | 4 | 3 | 0 | 1 |
| J RTM | 2 | 2 | 0 | 0 |
| K Provides/requires | 2 | 2 | 0 | 0 |
| L State machines | 3 | 2 | 0 | 1 |
| **Total** | **39** | **32** | **0** | **7** |

## Required actions before proceeding

None. All items are YES or N/A.

## Status

**READY TO PROCEED**

Blocking items: none
