# Phase 2 implementation plan index

**Generated**: 2026-02-28
**Total plans**: TBD
**Status legend**: `pending` | `in_progress` | `completed`

---

## Session start prompt

Copy-paste this as the first message in every implementation session:

> Read `.s2s/plans/INDEX-PHASE2.md`. Find the `in_progress` plan, or the first `pending` plan in the lowest-numbered wave. Read the full plan file. If any tasks are already checked `[x]`, read the corresponding source files to understand what was actually implemented before continuing. Then proceed with the first unchecked `[ ]` task.

That's all that's needed. The plan file contains the full context for that component. CONTEXT.md (loaded automatically via CLAUDE.md) covers the overall architecture.

---

## Plan generation approach

Phase 2 plans follow a three-phase generation process (lesson learned from Phase 1, documented in `.s2s/prompts/s2s-plan-phased-generation.md`):

1. **Scoping plan**: map Phase 2 features to work groups, identify dependency chains, define wave structure with explicit provides/requires contracts
2. **Dependency validation**: run SPV L1 (topology) and L3 (provides/requires) checks before generating detailed plans
3. **Detailed plans**: generate per-component plans with provides/requires YAML front-matter from the start

Existing validation checks apply to all generated plans:
- Intra-plan: CHK-DEP-*, CHK-PROTO-1, CHK-TEST-1, CHK-NFR-1, CHK-COMM-1, CHK-STARTUP-1 (see `prompts/s2s-plan-integration-readiness.md`)
- Cross-plan: CHK-COORD-1 through CHK-COORD-6 (see `prompts/s2s-plan-crossplan-coordination.md`)

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
- [ ] DOCS-007 resolved (OQ-018 learn-ui/admin-ui architecture, OQ-019 hardware minimum)
- [ ] Scoping plan generated and validated (phase 1 of the three-phase approach)

---

## Execution order

<!-- Populated by /s2s:plan after scoping phase -->

### Wave 0 - TBD

| Plan | Title | Complexity | Status |
|------|-------|------------|--------|

---

## Dependency graph (compact)

<!-- Populated after scoping plan defines wave structure -->

```
TBD
```

---

## Phase 1 reference

Phase 1 plans (15 plans, Waves 0-7, all completed) are in [INDEX-PHASE1.md](INDEX-PHASE1.md).
