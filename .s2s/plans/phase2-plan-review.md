# Phase 2 detailed plan review

**Date**: 2026-03-01
**Reviewed by**: Claude (post-generation codebase validation)
**Plans reviewed**: 11 (167 tasks total)
**Branch**: `chore/phase2-detailed-plans`

---

## Context

Phase 2 detailed plans were generated via `/s2s:plan` using the three-phase approach:
1. Scoping plan (11 plans, 6 waves, provides/requires mapped)
2. Dependency validation (SPV L1 + L3: 11 PASS, 1 FAIL fixed)
3. Detailed plan generation (5 parallel agents, one per wave group)

After generation, automated CHK-COORD-* validation caught one gap (missing `api_keys.expires_at` in database-phase2) and fixed it. Total: 167 tasks.

This review is the **fourth step**: manual cross-referencing of all 11 plans against the actual codebase. The goal is to catch issues that automated validation cannot: schema mismatches, naming inconsistencies, incorrect assumptions about existing code, and gaps between plan design notes and database DDL.

---

## What the generation process got right

- **Provides/requires contracts**: every plan declares its interfaces clearly. Cross-plan dependencies are correct.
- **Task granularity**: each task is specific, testable, and scoped to one deliverable.
- **Backward compatibility**: every plan explicitly addresses Phase 1 regression prevention.
- **Design notes with code**: SQL DDL, Python class sketches, and config examples reduce ambiguity.
- **Migration chain**: 0001 -> 0002 -> 0003 -> 0004 documented with clear ownership.
- **Graceful degradation**: core-pipeline-v2 has a 6-scenario failure matrix.
- **Session boundaries**: ingest-phase2 (32 tasks) recommends split at T11.
- **Wave ordering**: respects dependency topology and LLM context buildup.

## What the generation process got wrong

The agents generated plans from architecture.md + requirements.md + scoping plan, but did NOT systematically read the existing source code. This caused several categories of errors:

### Category 1: ORM sketches that contradict database DDL

The agents generated ORM model sketches in design notes without reading the corresponding DDL in the same plan set. core-conversations defines a `ConversationTurnOrm` with `sequence`, `role`, and `content_encrypted` fields, but database-phase2 defines `conversation_turns` with `turn_number`, `question` (BYTEA), and `answer` (BYTEA). Two separate encrypted columns, no role field.

### Category 2: config fields assumed missing but already exist

Multiple plans propose "adding" config fields or columns that Phase 1 already created via the forward-compatible data model (ARCH-040). Examples: `VEKTRA_CONVERSATION_KEY` already in VektraSettings, `rate_limit_rpm`/`revoked_at` already on `ApiKeyOrm`, `version`/`supersedes_id` already on `source_documents`.

### Category 3: cross-plan responsibility gaps

RLS policies are referenced by admin-enforcement ("policies created by database-phase2 migration") but database-phase2 has no such task. The responsibility fell between two plans.

### Category 4: naming inconsistencies between plans

database-phase2 uses `VEKTRA_ENCRYPTION_KEY` for the pgcrypto session variable. core-conversations uses `VEKTRA_CONVERSATION_KEY`. The existing codebase has `conversation_key`. Three names for the same thing.

### Category 5: import paths invented without reading existing code

infra-phase2 imports from `vektra_core.pipeline_v2` but the file is created as `vektra_core/advanced_pipeline.py` by core-pipeline-v2.

---

## Findings

### Critical (must fix before implementation)

| ID | Plan | Issue | Fix |
|----|------|-------|-----|
| C1 | core-conversations | ORM sketches contradict database-phase2 DDL: `sequence`/`role`/`content_encrypted` vs `turn_number`/`question`/`answer` | Add note: "ORM models must match database-phase2 DDL, not the sketch in design notes" |
| C2 | database-phase2 | downgrade() task omits `DROP COLUMN expires_at` and `DROP INDEX ix_api_keys_expires` | Add to downgrade task |
| C3 | admin-enforcement | RLS policy creation not in any plan. Design notes say "created by database-phase2 migration" but no such task exists | Add explicit RLS migration task to admin-enforcement |
| C4 | database-phase2 / core-conversations | Encryption key naming: `VEKTRA_ENCRYPTION_KEY` vs `VEKTRA_CONVERSATION_KEY` vs existing `conversation_key` | Align all to existing `VEKTRA_CONVERSATION_KEY` / `vektra.conversation_key` |

### High (fix before affected plan starts)

| ID | Plan | Issue | Fix |
|----|------|-------|-----|
| H1 | shared-protocols-phase2 | Provides says "AdvancedQueryPipeline Protocol" but it's a concrete class, not a Protocol | Fix Provides wording |
| H2 | shared-protocols-phase2 | ChunkingConfig(BaseSettings) proposed but chunking fields already inline in IngestConfig | Note: extend existing IngestConfig |
| H3 | index-hybrid | reindex_jobs migration: separate revision or combined with sparse_vector in 0003? | Clarify: combine into 0003 |
| H4 | component-learn | Migration for enrollments/dashboard_tokens says "or verify database-phase2 includes them" - it doesn't | Fix: explicitly create 0004 with down_revision="0003" |
| H5 | infra-phase2 | Import `vektra_core.pipeline_v2` but file is `advanced_pipeline.py` | Fix import path |
| H6 | core-conversations | T2 says "Add VEKTRA_CONVERSATION_KEY" but it already exists in config.py | Fix: "verify existing config field and add startup validation" |

### Medium (note for implementor)

| ID | Plan | Issue | Note |
|----|------|-------|------|
| M1 | multiple | Duplicate workspace/import-linter setup in shared-protocols-phase2, component-analytics, component-learn | Add "skip if already present" notes |
| M2 | ingest-phase2 | `version`/`supersedes_id` columns already exist on source_documents (Phase 1) | Acknowledge existing columns in plan |
| M3 | admin-ui | Dependency on admin-enforcement may be too restrictive for health/keys pages | Note: these pages could start with Phase 1 endpoints |
| M4 | root pyproject.toml | Coverage omit references `keystore.py` but actual file is `keys.py` | Minor, fix opportunistically |
| M5 | shared-protocols-phase2 | import-linter with missing root_packages: "skips gracefully" unverified | Verify before Wave 0 |

---

## Fixes applied

All Critical and High fixes were applied to the plan files after this review. Changes are committed on the same branch (`chore/phase2-detailed-plans`).

Summary of edits applied:
- **database-phase2**: downgrade task updated (C2), encryption key renamed `VEKTRA_ENCRYPTION_KEY` -> `VEKTRA_CONVERSATION_KEY` (C4)
- **core-conversations**: ORM mismatch disclaimer added to design notes (C1), encryption key description aligned (C4), config task changed from "Add" to "Verify existing" (H6)
- **admin-enforcement**: RLS policy creation task added as new task (C3), design notes updated to clarify ownership. Task count 13 -> 14.
- **shared-protocols-phase2**: Provides wording changed from "Protocol" to "configuration types" (H1), ChunkingConfig task notes IngestConfig (H2)
- **core-pipeline-v2**: Requires wording changed from "Protocol definition" to "configuration types" (H1)
- **index-hybrid**: T3 specifies migration 0003 explicitly, T14 states "same file as T3" (H3)
- **component-learn**: migration 0004 made explicit with `down_revision="0003"` (H4)
- **infra-phase2**: import path corrected `pipeline_v2` -> `advanced_pipeline` (H5)
- **component-analytics**: "skip if already present" note on workspace setup tasks (M1)
- **component-learn**: "skip if already present" note on workspace setup task (M1)
- **ingest-phase2**: note acknowledging existing `version`/`supersedes_id` columns (M2)
- **INDEX-PHASE2.md**: task count updated 167 -> 168 (admin-enforcement gained 1 task)

---

## Process assessment

### Three-phase generation: partially successful

The scoping -> validation -> detailed generation approach worked as designed. The scoping plan correctly identified 11 work groups, wave dependencies, and provides/requires contracts. Automated CHK-COORD validation caught the `expires_at` gap.

However, the detailed plan generation step has a structural weakness: agents read architecture.md and requirements.md but do not systematically read the existing source code. This means they can produce internally consistent plans that are inconsistent with the actual codebase.

### Time cost

| Step | Time |
|------|------|
| Scoping plan | ~30 min (previous session) |
| Dependency validation (SPV L1+L3) | ~10 min (previous session) |
| Detailed plan generation (5 parallel agents) | ~15 min |
| Automated CHK-COORD validation | ~5 min |
| **Manual codebase review (this step)** | **~30 min** |
| Fix application | ~15 min |
| **Total** | **~105 min** |

The manual review found 4 Critical and 6 High issues that automated validation missed. This step is not optional.

### Recommendation for future plan generation

A "Phase 4.5" should be added to the plan generation process:

1. Scoping plan
2. Dependency validation (SPV L1+L3)
3. Detailed plans
4. Automated CHK-* validation
5. **Codebase cross-reference** (new): for each plan, read the actual source files it modifies and verify DDL, types, config, and import paths match
