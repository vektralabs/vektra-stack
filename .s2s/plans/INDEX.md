# Phase 1 implementation plan index

**Generated**: 2026-02-17
**Total plans**: 15
**Status legend**: `pending` | `in_progress` | `completed`

---

## Session start prompt

Copy-paste this as the first message in every implementation session:

> Read `.s2s/plans/INDEX.md`. Find the `in_progress` plan, or the first `pending` plan in the lowest-numbered wave. Read the full plan file. If any tasks are already checked `[x]`, read the corresponding source files to understand what was actually implemented before continuing. Then proceed with the first unchecked `[ ]` task.

That's all that's needed. The plan file contains the full context for that component. CONTEXT.md (loaded automatically via CLAUDE.md) covers the overall architecture.

---

## How to work with these plans

### Starting a plan

1. Read this file - find the first `pending` plan in the current wave.
2. Read the full plan file to understand scope and acceptance criteria before writing any code.
3. For component-shared specifically: read `.s2s/architecture.md` sections 8.1-8.3 (Protocol signatures) before starting - 20 minutes that prevents backtracking.
4. Set `**Status**` in the plan file to `in_progress`. Update the table row in this file to match.

### Working through tasks

- Check off tasks as you complete them: `- [ ]` → `- [x]`.
- Do not move to the next task until the current one is testable and verified.
- All acceptance criteria must pass before marking the plan `completed` - not just all tasks checked.

### When context runs out mid-plan

The plan file is the resume point. Before ending the session:

1. Add a note in the `## Notes` section of the plan: what was done, what's next, any open issues.
2. Commit the plan file with checked tasks and the note (`git add .s2s/plans/<plan>.md && git commit`).

To resume in a new session:
1. Read this INDEX.md - find the `in_progress` plan.
2. Read the plan file - go to `## Notes` for last known state.
3. Continue from the first unchecked `[ ]` task.

### Completing a plan

1. All tasks `[x]`, all acceptance criteria verified.
2. Relevant tests written and passing.
3. Set `**Status**` in the plan file to `completed`.
4. Update the table row in this file to `completed`.
5. Commit. Then return here to pick the next plan.

### One at a time or parallel?

For a single developer: **one plan at a time**, completed before starting the next. Exception: Wave 0 has three independent plans - infra-003 (5 min) and docs-008 (spec decision) can be done in the same session before beginning component-shared.

### Extending plans

If during implementation you discover sub-tasks not in the plan: **add them to the existing plan file** as additional `[ ]` tasks. Create a new plan only if the scope change is significant (more than ~5 new tasks that represent a distinct deliverable).

Plans that may naturally expand if the component turns out larger than estimated:
- `component-core` may split into `component-core-pipeline` and `component-core-api`
- `component-index` may need a SQL spike task before full implementation

Plans to create when the time comes (currently blocked):
- DOCS-004 (traceability tables) and DOCS-005 (QA+BA roundtable on validation scenarios) - blocked until first Phase 1 milestone. Create them then.

---

## Execution order

### Wave 0 - no dependencies (start immediately, parallel)

| Plan | Title | Complexity | Status |
|------|-------|------------|--------|
| [20260217-docs-008](20260217-docs-008.md) | Resolve `no_relevant_context` REQ gap | small | completed |
| [20260217-infra-003](20260217-infra-003.md) | CODEOWNERS file | small | completed |
| [20260217-component-shared](20260217-component-shared.md) | vektra_shared: Protocols, types, config, auth, ProviderRegistry | medium | completed |

> **Note**: docs-008 must complete before Wave 3 (component-core uses the `QueryResponse.no_relevant_context` field decided here).

---

### Wave 1 - after component-shared

| Plan | Title | Complexity | Status |
|------|-------|------------|--------|
| [20260217-infra-database](20260217-infra-database.md) | Database schema and Alembic migrations | medium | completed |

> **Note**: infra-database codifies the BLOCKER resolutions from pre-implementation review (B-1/B-3: chunk deletion semantics + `chunks_removed` data source; B-2: `content_type` fallback to `application/octet-stream`).

---

### Wave 2 - after infra-database

| Plan | Title | Complexity | Status |
|------|-------|------------|--------|
| [20260217-component-index](20260217-component-index.md) | vektra-index: vector store, semantic search, metadata filtering | large | completed |

> **Note**: component-index loads the shared EmbeddingProvider (SentenceTransformersProvider). Both vektra-ingest and vektra-core use this shared instance via ProviderRegistry - no double model loading.

---

### Wave 3 - after component-index (parallel)

| Plan | Title | Complexity | Status |
|------|-------|------------|--------|
| [20260217-component-ingest](20260217-component-ingest.md) | vektra-ingest: document processing pipeline and async jobs | large | pending |
| [20260217-component-core](20260217-component-core.md) | vektra-core: QueryPipeline, LLM, streaming, conversations | large | pending |
| [20260217-component-admin](20260217-component-admin.md) | vektra-admin: health, API key management, audit log | medium | completed |

---

### Wave 4 - after all Wave 3 components (parallel)

| Plan | Title | Complexity | Status |
|------|-------|------------|--------|
| [20260217-infra-app-entrypoint](20260217-infra-app-entrypoint.md) | FastAPI assembly and startup validation (ARCH-057) | medium | pending |
| [20260217-feature-error-codes](20260217-feature-error-codes.md) | Error code registry and actionability enforcement | small | pending |

---

### Wave 5 - after infra-app-entrypoint

| Plan | Title | Complexity | Status |
|------|-------|------------|--------|
| [20260217-infra-docker](20260217-infra-docker.md) | Docker Compose stack, Dockerfile, TLS configs | medium | pending |

---

### Wave 6 - after infra-docker (parallel)

| Plan | Title | Complexity | Status |
|------|-------|------------|--------|
| [20260217-infra-ci](20260217-infra-ci.md) | CI pipeline: GitHub Actions, integration tests, NFR gates | medium | pending |
| [20260217-infra-makefile](20260217-infra-makefile.md) | Makefile targets and operator shell scripts | small | pending |

---

### Wave 7 - after infra-docker + Wave 3 (parallel)

| Plan | Title | Complexity | Status |
|------|-------|------------|--------|
| [20260217-docs-001](20260217-docs-001.md) | Diataxis documentation structure | small | pending |
| [20260217-docs-006](20260217-docs-006.md) | n8n periodic indexing reference workflow | small | pending |

---

## Dependency graph (compact)

```
docs-008 ──────────────────────────────────────────────► component-core
infra-003
component-shared ──► infra-database ──► component-index ──► component-ingest
                                                         ──► component-core
                                                         ──► component-admin
                                    (all Wave 3) ──► infra-app-entrypoint
                                                 ──► feature-error-codes
                                  infra-app-entrypoint ──► infra-docker
                                              infra-docker ──► infra-ci
                                                          ──► infra-makefile
                                                          ──► docs-001
                               component-ingest ──────────► docs-006
```

---

## Critical notes

- **BLOCKERs resolved in infra-database**: chunk deletion semantics (source_documents soft-delete, document_chunks hard-delete via explicit DELETE), `chunks_removed` data source (SELECT COUNT before DELETE in same transaction), `content_type` NOT NULL fallback.
- **docs-008 is Wave 0** but blocks Wave 3 (component-core). Complete it before starting component-core.
- **TECH-001 and INFRA-004** excluded: already completed (git commit `8ad8c00`). Update BACKLOG.md to mark them completed.
- **NFR hard gates** enforced in CI (infra-ci): NFR-002 search <500ms, NFR-004 startup <60s, NFR-007 100% audit log, NFR-009 100% error actionability.
