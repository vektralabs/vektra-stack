# Wave boundary checklist — SPV Level 5

**Wave**: 3
**Plans entering this wave**: 20260217-component-admin, 20260217-component-core, 20260217-component-ingest
**Date**: 2026-02-18
**Reviewer**: claude-sonnet-4-6 / 2026-02-18

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
| A1 | Does every new class or module have exactly one plan that creates it? | YES | vektra_admin/* → component-admin; vektra_core/* → component-core; vektra_ingest/* → component-ingest. No overlaps. |
| A2 | Does every `ProviderRegistry.register()` call have exactly one plan that performs it? | YES | Provider registration (LLM, Embedding, VectorStore) owned by infra-app-entrypoint (Wave 4). Key cache population in component-admin is a separate concern (key_hash store, not provider registration). |
| A3 | Does every middleware component have one plan that implements it and a (possibly different) plan that registers it on the app? | YES | AuditMiddleware: implemented in component-admin (middleware.py), registered by infra-app-entrypoint. Explicitly split. |
| A4 | Is `vektra_shared/audit.py` creation assigned to exactly one plan (not duplicated)? | YES | Only component-admin creates vektra_admin/audit.py and the vektra_shared/audit.py re-export. |

## B — Assembly conflicts

No component plan assumes another plan has wired shared infrastructure on its behalf.

| # | Question | Answer | Note |
|---|----------|--------|------|
| B1 | Is there a single "assembly" plan that owns all `ProviderRegistry.register()` calls at startup lifespan? | YES | infra-app-entrypoint (Wave 4). |
| B2 | Do all component plans avoid calling `ProviderRegistry.register()` in their own code (leaving this exclusively to the assembly plan)? | YES | Wave 3 plans don't call ProviderRegistry.register() for providers. Key cache uses ProviderRegistry as a shared store (different mechanism from provider registration). |
| B3 | Is `app.add_middleware(AuditMiddleware)` called by the assembly plan only? | YES | component-admin plan: "Do NOT register this middleware on the FastAPI app here — export AuditMiddleware class only. infra-app-entrypoint is responsible for calling app.add_middleware(AuditMiddleware)." |

## C — Background infrastructure

All background job infrastructure is explicitly assigned to a plan.

| # | Question | Answer | Note |
|---|----------|--------|------|
| C1 | Is the arq worker service (CMD_TARGET=worker) assigned to a specific plan (infra-docker)? | YES | infra-docker (Wave 4) owns the vektra-worker Docker service. |
| C2 | Are all arq task function definitions (`@arq` decorated) listed in a specific plan? | YES | `ingest_document_task` defined in component-ingest (vektra_ingest/jobs.py). Only one arq task in Wave 3. |
| C3 | Is the `docker/entrypoint.sh` single-image two-role pattern documented in a plan? | YES | infra-docker (Wave 4). |

## D — Import boundary enforcement

No plan imports from a sibling component at the module level (ADR-0005).

| # | Question | Answer | Note |
|---|----------|--------|------|
| D1 | Do all cross-component calls go through `vektra_shared` protocols or ProviderRegistry? | YES | component-ingest and component-core use ProviderRegistry for EmbeddingProvider and VectorStoreProvider. Audit log via vektra_shared.audit.log_event(), not direct vektra_admin import. |
| D2 | Is import-linter configured to enforce component boundaries for all packages in this wave? | NO | Wave 3 plans don't include a task to add vektra-admin, vektra-core, vektra-ingest to import-linter root_packages. Must be added as first task in component-admin. |
| D3 | Are there no new circular import paths introduced by this wave? | NO | DESIGN RISK: vektra_shared/audit.py described as "thin import alias" (from vektra_admin.audit import log_event). If implemented as a module-level alias, this creates a circular import: vektra_admin imports vektra_shared → vektra_shared.audit imports vektra_admin. Must use lazy import pattern instead (see required actions). |

## E — Decision closure

No open design decisions block implementation of Wave N plans.

| # | Question | Answer | Note |
|---|----------|--------|------|
| E1 | Are all "TBD", "TODO", and open questions in Wave N plans resolved? | YES | docs-008 (no_relevant_context field) completed in Wave 0. B-2 blocker (content_type fallback) resolved. No open questions found in three plans. |
| E2 | Is the health aggregator strategy explicitly decided (ProviderRegistry "health/*" keys — NOT direct module calls)? | YES | component-admin: "health check aggregator calling each component's health_check callable registered in ProviderRegistry under the 'health' category key. Do NOT call component modules directly (ADR-0005 violation)." |
| E3 | Is the key cache invalidation strategy fully specified (immediate update on create/revoke + TTL as fallback)? | YES | component-admin: "add to cache immediately" on create; "mark revoked in cache immediately" on delete. "Cache TTL (60s) is a fallback for external revocation only, not the primary invalidation mechanism." |

## F — Cache write completeness

Every write path that affects a cache has an explicit cache-update step.

| # | Question | Answer | Note |
|---|----------|--------|------|
| F1 | Does `create_key()` update the in-memory key cache immediately (not waiting for TTL reload)? | YES | "when POST /api-keys creates a key, add it to the cache immediately" |
| F2 | Does `revoke_key()` mark the key revoked in the cache immediately? | YES | "when DELETE /api-keys/{id} revokes a key, mark it revoked in the cache immediately" |
| F3 | Does startup load all non-revoked keys into the cache before the first request is served? | YES | "load all non-revoked api_keys from database into in-memory cache" as startup task. |

## G — Protocol contracts

Every Protocol interface used in this wave has a concrete registered implementation.

| # | Question | Answer | Note |
|---|----------|--------|------|
| G1 | Is `VectorStoreProvider` backed by `VectorStoreServiceAdapter` registered at startup? | YES | Registered by infra-app-entrypoint (Wave 4). Wave 3 plans correctly use it via ProviderRegistry without registering it themselves. |
| G2 | Is `EmbeddingProvider` backed by `SentenceTransformersProvider` registered at startup? | YES | Registered by infra-app-entrypoint (Wave 4). |
| G3 | Is `LLMProvider` backed by `LitellmProvider` registered at startup? | YES | LitellmProvider implemented in component-core (Wave 3); registered by infra-app-entrypoint (Wave 4). |
| G4 | Do all `SafeguardHook` implementations return `SafeguardResult` (not `bool`)? | N/A | No new SafeguardHook implementations in Wave 3. PassthroughSafeguard is in vektra_shared (Wave 1). |
| G5 | Is `QueryPipeline.execute()` signature consistent with the Protocol definition in vektra_shared? | YES | component-core: "execute(query_request) → (QueryResponse, QueryTrace)" consistent with Protocol. |

## H — Test infrastructure

Test infrastructure for all Wave N plans is in place.

| # | Question | Answer | Note |
|---|----------|--------|------|
| H1 | Do all async test files have `asyncio_mode = "auto"` in their pytest config? | NO | Wave 3 plans (component-admin, component-core, component-ingest) don't include a task to configure asyncio_mode = "auto". Must be added as first task in each component plan. |
| H2 | Do integration tests requiring PostgreSQL use testcontainers (not a shared external DB)? | YES | All three plans explicitly mention "testcontainers PostgreSQL". |
| H3 | Do session fixtures create a per-test async engine (no asyncpg event-loop contamination)? | YES | Pattern established in Wave 2 (component-index). All Wave 3 plans inherit this convention. |
| H4 | Is there at least one test per acceptance criterion for each Wave N plan? | YES | All three plans include explicit unit and integration test tasks covering each acceptance criterion. |

## I — NFR operationalization

All NFRs targeted by Wave N plans have explicit, runnable measurement methods.

| # | Question | Answer | Note |
|---|----------|--------|------|
| I1 | Is NFR-007 (100% audit coverage) tested with an integration test that counts `audit_log` rows? | YES | component-admin: "Audit log completeness test: make N authenticated requests, count audit_log rows, assert count matches." |
| I2 | Is NFR-004 (startup < 60s) measured with a timer from process start to first `/health` 200? | N/A | Startup time measured at container level; owned by infra-app-entrypoint/infra-docker (Wave 4). Not Wave 3 concern. |
| I3 | Is NFR-009 (100% error actionability) enforced by the feature-error-codes registry at CI time? | N/A | feature-error-codes is Wave 4. Error codes defined during Wave 3 implementation will be registered in Wave 4. |
| I4 | Is NFR-003 (ingest < 30s for 10-page PDF) measured by a timed benchmark test? | YES | component-ingest: "Benchmark ingestion of a 10-page text-extractable PDF (< 500KB); verify completion within 30s (NFR-003)." |

## J — RTM coverage (automated)

Run: `uv run python .s2s/scripts/verify_traceability.py`

| # | Question | Answer | Note |
|---|----------|--------|------|
| J1 | Script exits 0 (PASS)? | YES | PASS: 79 entries (5 BR, 13 NFR, 61 REQ) -- no gaps. 56 warnings (test refs missing, expected for Wave 3+). |
| J2 | Every REQ/NFR covered by Wave N plans has a non-empty `plan_task` field? | YES | All REQs referenced in Wave 3 plans have plan_task set in traceability.yaml. |

## K — Provides/requires (automated)

Run: `uv run python .s2s/scripts/verify_provides_requires.py`

| # | Question | Answer | Note |
|---|----------|--------|------|
| K1 | Script exits 0 (PASS — no GAP, CONFLICT, or ORDER violations)? | YES | PASS: 5 plans with provides/requires, 15 tokens provided, 5 requires -- all satisfied. |
| K2 | All Wave N plans that produce shared artifacts have provides_requires front-matter? | YES | component-admin, component-core, component-ingest all have provides_requires front-matter. |

## L — State machine completeness (automated)

Run: `uv run python .s2s/scripts/verify_state_machines.py`

| # | Question | Answer | Note |
|---|----------|--------|------|
| L1 | Script exits 0 (PASS)? | YES | PASS: 5 state machines -- all matrices complete. |
| L2 | Every new stateful component introduced in Wave N is defined in `.s2s/state-machines.yaml`? | YES | ingest_job, api_key, source_document all defined in state-machines.yaml (added during SPV setup). |
| L3 | Every FAILED terminal state has a documented recovery path (restart, admin action, or explicit "unrecoverable")? | YES | ingest_job.FAILED: re-submit same document (user action). source_document.FAILED: soft-deleted, re-ingest required. |

---

## Summary

| Section | Items | YES | NO | N/A |
|---------|-------|-----|-----|-----|
| A Ownership | 4 | 4 | 0 | 0 |
| B Assembly | 3 | 3 | 0 | 0 |
| C Background | 3 | 3 | 0 | 0 |
| D Import boundary | 3 | 1 | 2 | 0 |
| E Decision closure | 3 | 3 | 0 | 0 |
| F Cache writes | 3 | 3 | 0 | 0 |
| G Protocol contracts | 5 | 4 | 0 | 1 |
| H Test infrastructure | 4 | 3 | 1 | 0 |
| I NFR measurement | 4 | 2 | 0 | 2 |
| J RTM | 2 | 2 | 0 | 0 |
| K Provides/requires | 2 | 2 | 0 | 0 |
| L State machines | 3 | 3 | 0 | 0 |
| **Total** | **39** | **33** | **3** | **3** |

## Required actions before proceeding

- [ ] **D2**: Add vektra-admin, vektra-core, vektra-ingest to import-linter `root_packages` and boundary contracts. First task of component-admin implementation.
- [ ] **D3**: Implement `vektra_shared/audit.py` re-export as **lazy import** (not module-level alias) to avoid circular dependency. `vektra_admin` imports `vektra_shared`; a module-level `from vektra_admin.audit import log_event` in `vektra_shared/audit.py` would be circular. Correct pattern:
  ```python
  # vektra_shared/audit.py
  def log_event(*args, **kwargs):
      from vektra_admin.audit import log_event as _impl
      return _impl(*args, **kwargs)
  ```
  This must be documented as a constraint in component-admin before coding starts.
- [ ] **H1**: Add `asyncio_mode = "auto"` to pytest config for vektra-admin, vektra-core, vektra-ingest. First task of each component plan.

## Status

**BLOCKED**

Blocking items: D2, D3, H1

---

*Note: D3 is the most critical item — it's a design constraint that must be clarified before writing component-admin code. D2 and H1 can be resolved as the literal first tasks of each component's implementation session.*
