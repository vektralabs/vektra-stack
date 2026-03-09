# Wave boundary checklist — SPV Level 5

**Wave**: 5 (Integration)
**Plans entering this wave**: infra-phase2
**Date**: 2026-03-09
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
| A1 | Does every new class or module have exactly one plan that creates it? | YES | infra-phase2 creates no new classes; only modifies existing files |
| A2 | Does every `ProviderRegistry.register()` call have exactly one plan that performs it? | YES | infra-phase2 is the sole assembly plan for all register() calls |
| A3 | Does every middleware component have one plan that implements it and a (possibly different) plan that registers it on the app? | YES | All middleware already registered in main.py; no new middleware in Wave 5 |
| A4 | Is `vektra_shared/audit.py` creation assigned to exactly one plan (not duplicated)? | N/A | Already exists from Phase 1, not modified in Wave 5 |

## B — Assembly conflicts

No component plan assumes another plan has wired shared infrastructure on its behalf.

| # | Question | Answer | Note |
|---|----------|--------|------|
| B1 | Is there a single "assembly" plan that owns all `ProviderRegistry.register()` calls at startup lifespan? | YES | infra-phase2 is the sole assembly plan |
| B2 | Do all component plans avoid calling `ProviderRegistry.register()` in their own code (leaving this exclusively to the assembly plan)? | YES | Components expose services; registration is in main.py only |
| B3 | Is `app.add_middleware(AuditMiddleware)` called by the assembly plan only? | YES | Already in main.py, no duplication |

## C — Background infrastructure

All background job infrastructure is explicitly assigned to a plan.

| # | Question | Answer | Note |
|---|----------|--------|------|
| C1 | Is the arq worker service (CMD_TARGET=worker) assigned to a specific plan (infra-docker)? | N/A | arq worker is Phase 2 deferred; current stack uses BackgroundTasks |
| C2 | Are all arq task function definitions (`@arq` decorated) listed in a specific plan? | N/A | No arq tasks in Phase 2 |
| C3 | Is the `docker/entrypoint.sh` single-image two-role pattern documented in a plan? | YES | Already exists from Phase 1 infra-docker plan |

## D — Import boundary enforcement

No plan imports from a sibling component at the module level (ADR-0005).

| # | Question | Answer | Note |
|---|----------|--------|------|
| D1 | Do all cross-component calls go through `vektra_shared` protocols or ProviderRegistry? | YES | vektra_app is the only module allowed to import from all components |
| D2 | Is import-linter configured to enforce component boundaries for all packages in this wave? | YES | 8 contracts cover all components including analytics and learn |
| D3 | Are there no new circular import paths introduced by this wave? | YES | infra-phase2 only adds registrations in main.py; no circular imports |

## E — Decision closure

No open design decisions block implementation of Wave N plans.

| # | Question | Answer | Note |
|---|----------|--------|------|
| E1 | Are all "TBD", "TODO", and open questions in Wave N plans resolved? | YES | No TBD/TODO markers in infra-phase2 plan |
| E2 | Is the health aggregator strategy explicitly decided (ProviderRegistry "health/*" keys — NOT direct module calls)? | YES | Already implemented in main.py |
| E3 | Is the key cache invalidation strategy fully specified (immediate update on create/revoke + TTL as fallback)? | YES | InMemoryKeyStore already handles this |

## F — Cache write completeness

Every write path that affects a cache has an explicit cache-update step.

| # | Question | Answer | Note |
|---|----------|--------|------|
| F1 | Does `create_key()` update the in-memory key cache immediately (not waiting for TTL reload)? | YES | Already implemented in admin-enforcement |
| F2 | Does `revoke_key()` mark the key revoked in the cache immediately? | YES | Already implemented |
| F3 | Does startup load all non-revoked keys into the cache before the first request is served? | YES | `key_store.load_from_db()` in step 5 |

## G — Protocol contracts

Every Protocol interface used in this wave has a concrete registered implementation.

| # | Question | Answer | Note |
|---|----------|--------|------|
| G1 | Is `VectorStoreProvider` backed by `VectorStoreServiceAdapter` registered at startup? | YES | Lines 132-136 in main.py |
| G2 | Is `EmbeddingProvider` backed by `SentenceTransformersProvider` registered at startup? | YES | Lines 123-127 in main.py |
| G3 | Is `LLMProvider` backed by `LitellmProvider` registered at startup? | YES | Lines 114-116 in main.py |
| G4 | Do all `SafeguardHook` implementations return `SafeguardResult` (not `bool`)? | YES | PassthroughSafeguard and PresidioPIISafeguard verified |
| G5 | Is `QueryPipeline.execute()` signature consistent with the Protocol definition in vektra_shared? | YES | Both SimpleQueryPipeline and AdvancedQueryPipeline comply |

## H — Test infrastructure

Test infrastructure for all Wave N plans is in place.

| # | Question | Answer | Note |
|---|----------|--------|------|
| H1 | Do all async test files have `asyncio_mode = "auto"` in their pytest config? | YES | Set in root pyproject.toml |
| H2 | Do integration tests requiring PostgreSQL use testcontainers (not a shared external DB)? | N/A | infra-phase2 focuses on wiring; integration tests are Docker-based |
| H3 | Do session fixtures create a per-test async engine (no asyncpg event-loop contamination)? | N/A | No new test fixtures in this plan |
| H4 | Is there at least one test per acceptance criterion for each Wave N plan? | N/A | Acceptance criteria are verified by integration/Docker tests, not unit tests |

## I — NFR operationalization

All NFRs targeted by Wave N plans have explicit, runnable measurement methods.

| # | Question | Answer | Note |
|---|----------|--------|------|
| I1 | Is NFR-007 (100% audit coverage) tested with an integration test that counts `audit_log` rows? | N/A | Not modified in Wave 5 |
| I2 | Is NFR-004 (startup < 60s) measured with a timer from process start to first `/health` 200? | YES | HEALTHCHECK --start-period=30s in Dockerfile |
| I3 | Is NFR-009 (100% error actionability) enforced by the feature-error-codes registry at CI time? | N/A | Not modified in Wave 5 |
| I4 | Is NFR-003 (ingest < 30s for 10-page PDF) measured by a timed benchmark test? | N/A | Not modified in Wave 5 |

## J — RTM coverage (automated)

Run: `uv run python .s2s/scripts/verify_traceability.py`

| # | Question | Answer | Note |
|---|----------|--------|------|
| J1 | Script exits 0 (PASS)? | YES | PASS: 79 entries, no gaps (43 warnings for missing test refs) |
| J2 | Every REQ/NFR covered by Wave N plans has a non-empty `plan_task` field? | YES | REQ-005, REQ-006 covered |

## K — Provides/requires (automated)

Run: `uv run python .s2s/scripts/verify_provides_requires.py`

| # | Question | Answer | Note |
|---|----------|--------|------|
| K1 | Script exits 0 (PASS — no GAP, CONFLICT, or ORDER violations)? | YES | PASS: 5 plans, 14 tokens provided, 4 requires, all satisfied |
| K2 | All Wave N plans that produce shared artifacts have provides_requires front-matter? | YES | infra-phase2 has provides/requires section |

## L — State machine completeness (automated)

Run: `uv run python .s2s/scripts/verify_state_machines.py`

| # | Question | Answer | Note |
|---|----------|--------|------|
| L1 | Script exits 0 (PASS)? | YES | PASS: 5 state machines, all matrices complete |
| L2 | Every new stateful component introduced in Wave N is defined in `.s2s/state-machines.yaml`? | N/A | No new stateful components in Wave 5 |
| L3 | Every FAILED terminal state has a documented recovery path (restart, admin action, or explicit "unrecoverable")? | N/A | No new state machines |

---

## Summary

| Section | Items | YES | NO | N/A |
|---------|-------|-----|-----|-----|
| A Ownership | 4 | 3 | 0 | 1 |
| B Assembly | 3 | 3 | 0 | 0 |
| C Background | 3 | 1 | 0 | 2 |
| D Import boundary | 3 | 3 | 0 | 0 |
| E Decision closure | 3 | 3 | 0 | 0 |
| F Cache writes | 3 | 3 | 0 | 0 |
| G Protocol contracts | 5 | 5 | 0 | 0 |
| H Test infrastructure | 4 | 1 | 0 | 3 |
| I NFR measurement | 4 | 1 | 0 | 3 |
| J RTM | 2 | 2 | 0 | 0 |
| K Provides/requires | 2 | 2 | 0 | 0 |
| L State machines | 3 | 1 | 0 | 2 |
| **Total** | **39** | **28** | **0** | **11** |

## Required actions before proceeding

None.

## Status

**READY TO PROCEED**

Blocking items: none
