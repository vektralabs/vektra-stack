# Wave boundary checklist — SPV Level 5

**Wave**: {N}
**Plans entering this wave**: {list plan IDs}
**Date**: {YYYY-MM-DD}
**Reviewer**: {LLM model + date}

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
| A1 | Does every new class or module have exactly one plan that creates it? | | |
| A2 | Does every `ProviderRegistry.register()` call have exactly one plan that performs it? | | |
| A3 | Does every middleware component have one plan that implements it and a (possibly different) plan that registers it on the app? | | |
| A4 | Is `vektra_shared/audit.py` creation assigned to exactly one plan (not duplicated)? | | |

## B — Assembly conflicts

No component plan assumes another plan has wired shared infrastructure on its behalf.

| # | Question | Answer | Note |
|---|----------|--------|------|
| B1 | Is there a single "assembly" plan that owns all `ProviderRegistry.register()` calls at startup lifespan? | | |
| B2 | Do all component plans avoid calling `ProviderRegistry.register()` in their own code (leaving this exclusively to the assembly plan)? | | |
| B3 | Is `app.add_middleware(AuditMiddleware)` called by the assembly plan only? | | |

## C — Background infrastructure

All background job infrastructure is explicitly assigned to a plan.

| # | Question | Answer | Note |
|---|----------|--------|------|
| C1 | Is the arq worker service (CMD_TARGET=worker) assigned to a specific plan (infra-docker)? | | |
| C2 | Are all arq task function definitions (`@arq` decorated) listed in a specific plan? | | |
| C3 | Is the `docker/entrypoint.sh` single-image two-role pattern documented in a plan? | | |

## D — Import boundary enforcement

No plan imports from a sibling component at the module level (ADR-0005).

| # | Question | Answer | Note |
|---|----------|--------|------|
| D1 | Do all cross-component calls go through `vektra_shared` protocols or ProviderRegistry? | | |
| D2 | Is import-linter configured to enforce component boundaries for all packages in this wave? | | |
| D3 | Are there no new circular import paths introduced by this wave? | | |

## E — Decision closure

No open design decisions block implementation of Wave N plans.

| # | Question | Answer | Note |
|---|----------|--------|------|
| E1 | Are all "TBD", "TODO", and open questions in Wave N plans resolved? | | |
| E2 | Is the health aggregator strategy explicitly decided (ProviderRegistry "health/*" keys — NOT direct module calls)? | | |
| E3 | Is the key cache invalidation strategy fully specified (immediate update on create/revoke + TTL as fallback)? | | |

## F — Cache write completeness

Every write path that affects a cache has an explicit cache-update step.

| # | Question | Answer | Note |
|---|----------|--------|------|
| F1 | Does `create_key()` update the in-memory key cache immediately (not waiting for TTL reload)? | | |
| F2 | Does `revoke_key()` mark the key revoked in the cache immediately? | | |
| F3 | Does startup load all non-revoked keys into the cache before the first request is served? | | |

## G — Protocol contracts

Every Protocol interface used in this wave has a concrete registered implementation.

| # | Question | Answer | Note |
|---|----------|--------|------|
| G1 | Is `VectorStoreProvider` backed by `VectorStoreServiceAdapter` registered at startup? | | |
| G2 | Is `EmbeddingProvider` backed by `SentenceTransformersProvider` registered at startup? | | |
| G3 | Is `LLMProvider` backed by `LitellmProvider` registered at startup? | | |
| G4 | Do all `SafeguardHook` implementations return `SafeguardResult` (not `bool`)? | | |
| G5 | Is `QueryPipeline.execute()` signature consistent with the Protocol definition in vektra_shared? | | |

## H — Test infrastructure

Test infrastructure for all Wave N plans is in place.

| # | Question | Answer | Note |
|---|----------|--------|------|
| H1 | Do all async test files have `asyncio_mode = "auto"` in their pytest config? | | |
| H2 | Do integration tests requiring PostgreSQL use testcontainers (not a shared external DB)? | | |
| H3 | Do session fixtures create a per-test async engine (no asyncpg event-loop contamination)? | | |
| H4 | Is there at least one test per acceptance criterion for each Wave N plan? | | |

## I — NFR operationalization

All NFRs targeted by Wave N plans have explicit, runnable measurement methods.

| # | Question | Answer | Note |
|---|----------|--------|------|
| I1 | Is NFR-007 (100% audit coverage) tested with an integration test that counts `audit_log` rows? | | |
| I2 | Is NFR-004 (startup < 60s) measured with a timer from process start to first `/health` 200? | | |
| I3 | Is NFR-009 (100% error actionability) enforced by the feature-error-codes registry at CI time? | | |
| I4 | Is NFR-003 (ingest < 30s for 10-page PDF) measured by a timed benchmark test? | | |

## J — RTM coverage (automated)

Run: `uv run python .s2s/scripts/verify_traceability.py`

| # | Question | Answer | Note |
|---|----------|--------|------|
| J1 | Script exits 0 (PASS)? | | Paste output on NO |
| J2 | Every REQ/NFR covered by Wave N plans has a non-empty `plan_task` field? | | |

## K — Provides/requires (automated)

Run: `uv run python .s2s/scripts/verify_provides_requires.py`

| # | Question | Answer | Note |
|---|----------|--------|------|
| K1 | Script exits 0 (PASS — no GAP, CONFLICT, or ORDER violations)? | | Paste output on NO |
| K2 | All Wave N plans that produce shared artifacts have provides_requires front-matter? | | |

## L — State machine completeness (automated)

Run: `uv run python .s2s/scripts/verify_state_machines.py`

| # | Question | Answer | Note |
|---|----------|--------|------|
| L1 | Script exits 0 (PASS)? | | Paste output on NO |
| L2 | Every new stateful component introduced in Wave N is defined in `.s2s/state-machines.yaml`? | | |
| L3 | Every FAILED terminal state has a documented recovery path (restart, admin action, or explicit "unrecoverable")? | | |

---

## Summary

| Section | Items | YES | NO | N/A |
|---------|-------|-----|-----|-----|
| A Ownership | 4 | | | |
| B Assembly | 3 | | | |
| C Background | 3 | | | |
| D Import boundary | 3 | | | |
| E Decision closure | 3 | | | |
| F Cache writes | 3 | | | |
| G Protocol contracts | 5 | | | |
| H Test infrastructure | 4 | | | |
| I NFR measurement | 4 | | | |
| J RTM | 2 | | | |
| K Provides/requires | 2 | | | |
| L State machines | 3 | | | |
| **Total** | **39** | | | |

## Required actions before proceeding

List each NO item with its planned resolution:

- [ ] {item ID}: {description of fix}

## Status

**READY TO PROCEED** / **BLOCKED** (circle one)

Blocking items: {list item IDs that are NO, or "none"}
