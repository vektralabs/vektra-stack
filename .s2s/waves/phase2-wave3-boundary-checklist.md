# Wave boundary checklist - SPV Level 5

**Wave**: 3
**Plans entering this wave**: 20260301-component-analytics
**Date**: 2026-03-08
**Reviewer**: Claude Opus 4.6, 2026-03-08

---

Instructions: answer each item YES / NO / N/A.
- A NO requires a **required action** listed at the bottom before proceeding.
- All items must be YES or N/A to mark the wave as READY.
- Run automated checks (J, K, L) as actual commands, not estimates.

---

## A - Ownership completeness

Every shared artifact written across plans has exactly one owning plan.

| # | Question | Answer | Note |
|---|----------|--------|------|
| A1 | Does every new class or module have exactly one plan that creates it? | YES | Single plan: component-analytics owns all new files |
| A2 | Does every `ProviderRegistry.register()` call have exactly one plan that performs it? | N/A | No ProviderRegistry registration in this plan (wiring deferred to infra-phase2) |
| A3 | Does every middleware component have one plan that implements it and a (possibly different) plan that registers it on the app? | N/A | No middleware in this plan |
| A4 | Is `vektra_shared/audit.py` creation assigned to exactly one plan (not duplicated)? | N/A | audit.py not touched by this plan |

## B - Assembly conflicts

No component plan assumes another plan has wired shared infrastructure on its behalf.

| # | Question | Answer | Note |
|---|----------|--------|------|
| B1 | Is there a single "assembly" plan that owns all `ProviderRegistry.register()` calls at startup lifespan? | YES | infra-phase2 owns all wiring |
| B2 | Do all component plans avoid calling `ProviderRegistry.register()` in their own code (leaving this exclusively to the assembly plan)? | YES | component-analytics defines service, does not register it |
| B3 | Is `app.add_middleware(AuditMiddleware)` called by the assembly plan only? | N/A | No middleware changes in this wave |

## C - Background infrastructure

All background job infrastructure is explicitly assigned to a plan.

| # | Question | Answer | Note |
|---|----------|--------|------|
| C1 | Is the arq worker service (CMD_TARGET=worker) assigned to a specific plan (infra-docker)? | N/A | No arq changes in this wave |
| C2 | Are all arq task function definitions (`@arq` decorated) listed in a specific plan? | N/A | No arq tasks in this plan; delete_before() is a method called by infra-phase2's cleanup job |
| C3 | Is the `docker/entrypoint.sh` single-image two-role pattern documented in a plan? | N/A | No Docker changes in this wave |

## D - Import boundary enforcement

No plan imports from a sibling component at the module level (ADR-0005).

| # | Question | Answer | Note |
|---|----------|--------|------|
| D1 | Do all cross-component calls go through `vektra_shared` protocols or ProviderRegistry? | YES | vektra_analytics imports only from vektra_shared |
| D2 | Is import-linter configured to enforce component boundaries for all packages in this wave? | YES | Contract added for vektra_analytics, root_packages updated |
| D3 | Are there no new circular import paths introduced by this wave? | YES | vektra_analytics is a leaf component with no dependents |

## E - Decision closure

No open design decisions block implementation of Wave 3 plans.

| # | Question | Answer | Note |
|---|----------|--------|------|
| E1 | Are all "TBD", "TODO", and open questions in Wave 3 plans resolved? | YES | Plan is fully specified |
| E2 | Is the health aggregator strategy explicitly decided (ProviderRegistry "health/*" keys - NOT direct module calls)? | N/A | No health endpoints in this plan |
| E3 | Is the key cache invalidation strategy fully specified (immediate update on create/revoke + TTL as fallback)? | N/A | No cache changes in this wave |

## F - Cache write completeness

Every write path that affects a cache has an explicit cache-update step.

| # | Question | Answer | Note |
|---|----------|--------|------|
| F1 | Does `create_key()` update the in-memory key cache immediately (not waiting for TTL reload)? | N/A | No key management changes |
| F2 | Does `revoke_key()` mark the key revoked in the cache immediately? | N/A | No key management changes |
| F3 | Does startup load all non-revoked keys into the cache before the first request is served? | N/A | No startup changes in this wave |

## G - Protocol contracts

Every Protocol interface used in this wave has a concrete registered implementation.

| # | Question | Answer | Note |
|---|----------|--------|------|
| G1 | Is `VectorStoreProvider` backed by `VectorStoreServiceAdapter` registered at startup? | N/A | Not used by analytics |
| G2 | Is `EmbeddingProvider` backed by `SentenceTransformersProvider` registered at startup? | N/A | Not used by analytics |
| G3 | Is `LLMProvider` backed by `LitellmProvider` registered at startup? | N/A | Not used by analytics |
| G4 | Do all `SafeguardHook` implementations return `SafeguardResult` (not `bool`)? | N/A | Not used by analytics |
| G5 | Is `QueryPipeline.execute()` signature consistent with the Protocol definition in vektra_shared? | N/A | Analytics consumes QueryTrace output, does not implement QueryPipeline |

## H - Test infrastructure

Test infrastructure for all Wave 3 plans is in place.

| # | Question | Answer | Note |
|---|----------|--------|------|
| H1 | Do all async test files have `asyncio_mode = "auto"` in their pytest config? | YES | pyproject.toml has asyncio_mode = "auto" |
| H2 | Do integration tests requiring PostgreSQL use testcontainers (not a shared external DB)? | N/A | No integration tests in this plan |
| H3 | Do session fixtures create a per-test async engine (no asyncpg event-loop contamination)? | N/A | Unit tests use mocked sessions |
| H4 | Is there at least one test per acceptance criterion for each Wave 3 plan? | YES | Plan defines tests for all AC items |

## I - NFR operationalization

All NFRs targeted by Wave 3 plans have explicit, runnable measurement methods.

| # | Question | Answer | Note |
|---|----------|--------|------|
| I1 | Is NFR-007 (100% audit coverage) tested with an integration test that counts `audit_log` rows? | N/A | Not targeted by Wave 3 |
| I2 | Is NFR-004 (startup < 60s) measured with a timer from process start to first `/health` 200? | N/A | Not targeted by Wave 3 |
| I3 | Is NFR-009 (100% error actionability) enforced by the feature-error-codes registry at CI time? | N/A | Not targeted by Wave 3 |
| I4 | Is NFR-003 (ingest < 30s for 10-page PDF) measured by a timed benchmark test? | N/A | Not targeted by Wave 3 |

## J - RTM coverage (automated)

Run: `uv run python .s2s/scripts/verify_traceability.py`

| # | Question | Answer | Note |
|---|----------|--------|------|
| J1 | Script exits 0 (PASS)? | YES | PASS: 79 entries, no gaps, 45 warnings (test refs, pre-existing) |
| J2 | Every REQ/NFR covered by Wave 3 plans has a non-empty `plan_task` field? | YES | REQ-060, REQ-051 both have plan_task |

## K - Provides/requires (automated)

Run: `uv run python .s2s/scripts/verify_provides_requires.py`

| # | Question | Answer | Note |
|---|----------|--------|------|
| K1 | Script exits 0 (PASS - no GAP, CONFLICT, or ORDER violations)? | YES | PASS: 5 plans, 14 tokens, 4 requires, all satisfied |
| K2 | All Wave 3 plans that produce shared artifacts have provides_requires front-matter? | YES | component-analytics has provides/requires section |

## L - State machine completeness (automated)

Run: `uv run python .s2s/scripts/verify_state_machines.py`

| # | Question | Answer | Note |
|---|----------|--------|------|
| L1 | Script exits 0 (PASS)? | YES | PASS: 5 state machines, all matrices complete |
| L2 | Every new stateful component introduced in Wave 3 is defined in `.s2s/state-machines.yaml`? | N/A | No new stateful components (traces are append-only, no state machine) |
| L3 | Every FAILED terminal state has a documented recovery path (restart, admin action, or explicit "unrecoverable")? | N/A | No new stateful components |

---

## Summary

| Section | Items | YES | NO | N/A |
|---------|-------|-----|-----|-----|
| A Ownership | 4 | 1 | 0 | 3 |
| B Assembly | 3 | 2 | 0 | 1 |
| C Background | 3 | 0 | 0 | 3 |
| D Import boundary | 3 | 3 | 0 | 0 |
| E Decision closure | 3 | 1 | 0 | 2 |
| F Cache writes | 3 | 0 | 0 | 3 |
| G Protocol contracts | 5 | 0 | 0 | 5 |
| H Test infrastructure | 4 | 2 | 0 | 2 |
| I NFR measurement | 4 | 0 | 0 | 4 |
| J RTM | 2 | 2 | 0 | 0 |
| K Provides/requires | 2 | 2 | 0 | 0 |
| L State machines | 3 | 1 | 0 | 2 |
| **Total** | **39** | **14** | **0** | **25** |

## Required actions before proceeding

None.

## Status

**READY TO PROCEED**

Blocking items: none
