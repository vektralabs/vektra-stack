# Phase 2 implementation plan index

**Generated**: 2026-02-28
**Updated**: 2026-03-01
**Total plans**: 11 (168 tasks total)
**Status legend**: `pending` | `in_progress` | `completed`

---

## Session start prompt

Copy-paste this as the first message in every implementation session:

> Read `.s2s/plans/INDEX-PHASE2.md`. Find the `in_progress` plan, or the first `pending` plan in the lowest-numbered wave. Read the full plan file. If any tasks are already checked `[x]`, read the corresponding source files to understand what was actually implemented before continuing. Then proceed with the first unchecked `[ ]` task.

That's all that's needed. The plan file contains the full context for that component. CONTEXT.md (loaded automatically via CLAUDE.md) covers the overall architecture.

---

## Plan generation approach

Phase 2 plans follow a three-phase generation process (lesson learned from Phase 1, where dependency gaps between plans were only discovered during implementation):

1. **Scoping plan**: map Phase 2 features to work groups, identify dependency chains, define wave structure with explicit provides/requires contracts
2. **Dependency validation**: run SPV L1 (topology) and L3 (provides/requires) checks before generating detailed plans
3. **Detailed plans**: generate per-component plans with provides/requires YAML front-matter from the start

After generation, all plans are validated against:
- Intra-plan checks: CHK-DEP-*, CHK-PROTO-1, CHK-TEST-1, CHK-NFR-1, CHK-COMM-1, CHK-STARTUP-1
- Cross-plan checks: CHK-COORD-1 through CHK-COORD-6

---

## How to work with these plans

### Starting a plan

1. Read this file - find the first `pending` plan in the current wave.
2. Read the full plan file to understand scope and acceptance criteria before writing any code.
3. Set `**Status**` in the plan file to `in_progress`. Update the table row in this file to match.

### Working through tasks

- Check off tasks as you complete them: `- [ ]` -> `- [x]`.
- Do not move to the next task until the current one is testable and verified.
- All acceptance criteria must pass before marking the plan `completed` - not just all tasks checked.

### When context runs out mid-plan

The plan file is the resume point. Before ending the session:

1. Add a note in the `## Notes` section of the plan: what was done, what's next, any open issues.
2. Commit the plan file with checked tasks and the note (`git add .s2s/plans/<plan>.md && git commit`).

To resume in a new session:
1. Read this INDEX-PHASE2.md - find the `in_progress` plan.
2. Read the plan file - go to `## Notes` for last known state.
3. Continue from the first unchecked `[ ]` task.

### Completing a plan

1. All tasks `[x]`, all acceptance criteria verified.
2. Relevant tests written and passing.
3. Set `**Status**` in the plan file to `completed`.
4. Update the table row in this file to `completed`.
5. Commit. Then return here to pick the next plan.

### Extending plans

If during implementation you discover sub-tasks not in the plan: **add them to the existing plan file** as additional `[ ]` tasks. Create a new plan only if the scope change is significant (more than ~5 new tasks that represent a distinct deliverable).

---

## Prerequisites

Before generating Phase 2 plans:

- [x] Phase 1 released (v0.1.0)
- [x] DOCS-007 resolved (ADR-0024, ADR-0025, ARCH-062/063/064)
- [x] Scoping plan generated ([phase2-scoping-plan.md](phase2-scoping-plan.md))
- [x] Dependency validation (SPV L1 + L3 checks)
- [x] Detailed plans generated with provides/requires (11 plans, 168 tasks, validated CHK-*/CHK-COORD-* + manual codebase review)

---

## Execution order

### Wave 0 - Foundation

| # | Plan | Title | Tasks | Status |
|---|------|-------|-------|--------|
| 1 | [shared-protocols-phase2](20260301-shared-protocols-phase2.md) | Protocol additions and import boundaries | 11 | completed |
| 2 | [database-phase2](20260301-database-phase2.md) | New tables, migrations, TOCTOU fix | 11 | completed |

---

### Wave 1 - Component enhancements

| # | Plan | Title | Tasks | Status |
|---|------|-------|-------|--------|
| 3 | [core-conversations](20260301-core-conversations.md) | Persistent conversations, feedback | 12 | pending |
| 4 | [admin-enforcement](20260301-admin-enforcement.md) | RLS, scope enforcement, rate limiting | 14 | pending |
| 5 | [index-hybrid](20260301-index-hybrid.md) | Hybrid search, Qdrant, reindex API | 19 | pending |
| 6 | [ingest-phase2](20260301-ingest-phase2.md) | OCR, dual chunking, versioning, batch ops | 32 | pending |

---

### Wave 2 - Advanced features

| # | Plan | Title | Tasks | Status |
|---|------|-------|-------|--------|
| 7 | [core-pipeline-v2](20260301-core-pipeline-v2.md) | Advanced pipeline, safeguards, streaming trace | 15 | pending |
| 8 | [admin-ui](20260301-admin-ui.md) | HTMX + Jinja2 admin dashboard | 12 | pending |

---

### Wave 3 - Analytics

| # | Plan | Title | Tasks | Status |
|---|------|-------|-------|--------|
| 9 | [component-analytics](20260301-component-analytics.md) | QueryTrace storage, metrics, reporting API | 12 | pending |

---

### Wave 4 - E-learning vertical

| # | Plan | Title | Tasks | Status |
|---|------|-------|-------|--------|
| 10 | [component-learn](20260301-component-learn.md) | LMS-agnostic API, chatbot widget | 18 | pending |

---

### Wave 5 - Integration

| # | Plan | Title | Tasks | Status |
|---|------|-------|-------|--------|
| 11 | [infra-phase2](20260301-infra-phase2.md) | App entrypoint, Docker, CI | 12 | pending |

---

## Dependency graph (compact)

```
shared-protocols-phase2 ──┬──► index-hybrid ─────────────┐
                          ├──► admin-enforcement ──► admin-ui
                          └──► ingest-phase2        │     │
                                                    │     │
database-phase2 ──────────┬──► core-conversations ──┤     │
                          ├──► admin-enforcement     │     │
                          └──► ingest-phase2         │     │
                                                     ▼     │
                              core-pipeline-v2 ◄─────┘     │
                                    │                       │
                                    ▼                       │
                             component-analytics            │
                                    │                       │
                                    ▼                       │
                             component-learn                │
                                    │                       │
                                    ▼                       │
                              infra-phase2 ◄────────────────┘
```

---

## Phase 1 reference

Phase 1 plans (15 plans, Waves 0-7, all completed) are in [INDEX-PHASE1.md](INDEX-PHASE1.md).
