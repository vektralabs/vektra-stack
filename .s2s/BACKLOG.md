# Vektra Backlog

**Updated**: 2026-02-17
**Format**: Single markdown file for tracking work items

---

## ID Conventions

| Prefix | Category | Example |
|--------|----------|---------|
| FEAT | Features | FEAT-001 |
| BUG | Bug fixes | BUG-001 |
| TECH | Technical tasks | TECH-001 |
| DEBT | Technical debt | DEBT-001 |
| DOCS | Documentation | DOCS-001 |
| INFRA | Infrastructure/setup | INFRA-001 |

**Status values**: `draft` | `planned` | `in_progress` | `blocked` | `completed`

---

## Planned

### DOCS-004: Complete traceability tables A.1, A.2, A.3 in architecture.md

**Status**: planned | **Priority**: medium | **Created**: 2026-02-17
**Blocked by**: First Phase 1 implementation milestone

**Context**: Appendix A of architecture.md has three traceability tables that are ~40% complete (measured during pre-implementation review, 2026-02-10):
- A.1: 26/60 Phase 1 REQs missing REQ -> ARCH decision mappings
- A.2: 24/60 ARCH decisions missing ARCH -> REQ mappings
- A.3: 21 REQs missing REQ -> validation scenario mappings

Best done with code in hand: mappings are more accurate when you can verify against the actual module that implements a requirement, not just claim it based on design intent. Also includes two minor INFO fixes from the pre-implementation review that were intentionally deferred: REF-06 (bootstrap key VEKTRA_ADMIN_BOOTSTRAP_KEY env var missing REQ cross-reference) and REF-07 (multi-scope API key schema note missing REQ cross-reference).

**Traceability**: architecture.md Appendix A, all 60 Phase 1 REQs

**Acceptance Criteria**:
- [ ] A.1 complete: all 60 Phase 1 REQs have at least one ARCH decision mapped
- [ ] A.2 complete: all 60 ARCH decisions have at least one motivating REQ mapped
- [ ] A.3 complete: every Phase 1 REQ covered by at least one validation scenario ID
- [ ] REF-06 fixed: VEKTRA_ADMIN_BOOTSTRAP_KEY config entry links to REQ-036
- [ ] REF-07 fixed: multi-scope key schema note links to REQ-031

---

### DOCS-005: Roundtable QA+BA on validation scenarios

**Status**: planned | **Priority**: medium | **Created**: 2026-02-17
**Blocked by**: First implementation sprint (need real tests to compare against)

**Context**: The 58 validation scenarios in validation-scenarios.md (v0.3) were written from an architect/developer perspective. A QA Lead + Business Analyst roundtable is needed to verify:
- Acceptance criteria are unambiguous and testable as automated tests
- Actors, triggers, and preconditions are realistic
- Scenarios correctly reflect the business intent of the underlying REQs

Specific items to address at the roundtable:
1. SC-C06 section placement: EventEmitter is in C (pipeline quality controls) but it is an extensibility/integration concern - evaluate whether it belongs in F (operational)
2. SC-F10 traceability: uses `ADR-0008` while all other scenarios use `ARCH-xxx` format - standardize
3. SRS gap: `no_relevant_context` behavior is defined in ARCH-056 but has no corresponding REQ - evaluate whether a REQ should be added before Phase 1 is closed (see also DOCS-008)
4. Testability audit: criteria that depend on "verifiable via debug log" or "verifiable via test double" need concrete test strategy

**Traceability**: validation-scenarios.md, requirements.md

**Acceptance Criteria**:
- [ ] All 58 scenarios reviewed by QA Lead and Business Analyst personas
- [ ] Ambiguous acceptance criteria rewritten with concrete measurable assertions
- [ ] SC-C06 placement decision made (keep in C or move to F)
- [ ] SC-F10 traceability format standardized
- [ ] no_relevant_context gap resolved (new REQ or explicit ARCH-only decision documented)
- [ ] Scenarios updated to v0.4

---

### DOCS-006: Document n8n periodic indexing reference workflow

**Status**: planned | **Priority**: low | **Created**: 2026-02-17

**Context**: n8n is the external pipeline orchestrator for Vektra (configuration over fork, no scheduling logic inside Vektra). The periodic indexing pattern - e.g., daily sync of course materials from an LMS - is an open question in requirements.md (OQ: "Periodic indexing pattern: n8n orchestrates ingestion, but the scheduling pattern needs documentation as a reference workflow"). SC-A02 describes the ingestion side but not the n8n workflow definition. This is needed for the MVP operator experience (SC-H01 target: 30 minutes to first working query, which implies n8n integration should be documentable).

**Traceability**: requirements.md OQ (periodic indexing), SC-A02, REQ-005 (onboarding)

**Acceptance Criteria**:
- [ ] Reference n8n workflow (JSON export) included in repository under docs/workflows/ or similar
- [ ] Workflow performs: poll source -> compute SHA-256 -> skip if exists -> POST /ingest -> poll job status
- [ ] README or guide explains how to import and configure the workflow
- [ ] OQ closed in requirements.md with pointer to the reference workflow

---

### DOCS-007: Resolve Phase 2 open questions before /s2s:design Phase 2

**Status**: planned | **Priority**: low (now) → high (before Phase 2 design) | **Created**: 2026-02-17
**Blocked by**: Phase 1 completion

**Context**: Two open questions from requirements.md are deferred to Phase 2 design but need resolution before the Phase 2 design roundtable can run:

**OQ-018** (learn-ui + admin-ui architecture):
- Is the chatbot widget a standalone npm package or served by the backend?
- Is admin a separate SPA or integrated into vektra-core?
- Decision affects vektra-learn component structure and deployment model

**OQ-019** (Phase 2 hardware minimum):
- Recommendation already exists (8GB RAM / 4 CPU, OQ-019 in architecture.md)
- Needs formal sign-off and propagation into Phase 2 NFRs
- Decision affects vektra-learn and vektra-analytics RAM allocation planning

**Traceability**: requirements.md OQ-018, OQ-019, architecture.md CONTEXT.md

**Acceptance Criteria**:
- [ ] OQ-018: architecture decision recorded (ADR or ARCH entry) for widget deployment model
- [ ] OQ-018: architecture decision recorded for admin-ui deployment model
- [ ] OQ-019: Phase 2 hardware NFR formally added to requirements.md (NFR-014 or similar)
- [ ] Both OQs marked as resolved in requirements.md and CONTEXT.md updated

---

### DOCS-008: Evaluate adding REQ for no_relevant_context behavior

**Status**: planned | **Priority**: low | **Created**: 2026-02-17

**Context**: The `no_relevant_context: bool` field in QueryResponse and the behavior "when all chunks score below VEKTRA_MIN_RELEVANCE_SCORE, return graceful no-context response" is defined in ARCH-056 and ADR-0021, but has no corresponding functional requirement in requirements.md. This was flagged in the validation scenarios gaps table (validation-scenarios.md). SC-B05 covers the behavior as a validation scenario, but traceability to a REQ is missing. The omission was noticed during pre-implementation review.

**Traceability**: ARCH-056, ADR-0021, SC-B05, validation-scenarios.md gaps table

**Acceptance Criteria**:
- [ ] Decision made: either add REQ-066 (no_relevant_context graceful fallback) to requirements.md, or explicitly note in ARCH-056 that this is architecture-derived behavior without a corresponding functional REQ
- [ ] If REQ added: traceability tables updated (ties to DOCS-004)
- [ ] Gaps table row in validation-scenarios.md updated to reflect resolution

---

### TECH-003: Phase 2 design roundtable (/s2s:design Phase 2)

**Status**: planned | **Priority**: low (now) → high (after Phase 1 stable) | **Created**: 2026-02-17
**Blocked by**: Phase 1 completion, DOCS-007

**Context**: Phase 1 uses a modular monolith design. Phase 2 introduces: vektra-analytics, vektra-learn, vektra-admin (full), hybrid search, persistent conversation storage, full scope enforcement, OAuth/OIDC, Qdrant as optional vector store. A full /s2s:design roundtable is needed before any Phase 2 implementation begins. DOCS-007 must be resolved first (UI architecture decisions feed the Phase 2 component design).

Key Phase 2 topics for the roundtable:
- vektra-learn component structure and LMS adapter interface
- vektra-analytics: QueryTrace storage, reporting API, alerting triggers
- Hybrid search: BM25/SPLADE via SparseEmbeddingProvider, Qdrant as VectorStoreProvider
- Persistent conversation storage with pgcrypto encryption (ADR-0011)
- Full API key scope enforcement (REQ-024 Phase 2)
- RLS binding activation (ADR-0009 feature flag)
- Phase 2 hardware profile: 8GB / 4CPU minimum

**Traceability**: architecture.md section 11.3, requirements.md EX-xxx items, ADR-0009, ADR-0011

**Acceptance Criteria**:
- [ ] Phase 2 architecture document updated (or new architecture.md v2.0)
- [ ] New ADRs for Phase 2 decisions (target: ADR-0023+)
- [ ] Phase 2 requirements reviewed and promoted from EX-xxx to REQ-xxx
- [ ] Phase 2 validation scenarios drafted (new /s2s:specs + /s2s:design cycle)

---

### INFRA-003: Create CODEOWNERS file

**Status**: planned | **Priority**: medium | **Created**: 2026-01-29

**Context**: Maps components to maintainers for automatic PR review assignment.

**Traceability**:
- **Implements**: REQ-016, REQ-018

**Acceptance Criteria**:
- [ ] CODEOWNERS file at repo root
- [ ] Each component has owner defined
- [ ] Docs ownership follows component ownership

---

### INFRA-004: Create component directories

**Status**: planned | **Priority**: high | **Created**: 2026-01-29

**Context**: Monorepo component folders don't exist yet. Need basic structure before coding starts.

**Traceability**:
- **Implements**: ADR-0001

**Acceptance Criteria**:
- [ ] vektra-core/ with README.md and pyproject.toml stub
- [ ] vektra-ingest/ with README.md and pyproject.toml stub
- [ ] vektra-index/ with README.md and pyproject.toml stub
- [ ] vektra-admin/ with README.md stub

---

### DOCS-001: Create documentation structure

**Status**: planned | **Priority**: medium | **Created**: 2026-01-29

**Context**: Diataxis-based docs structure per REQ-007, REQ-010.

**Traceability**:
- **Implements**: REQ-007, REQ-008, REQ-009, REQ-010

**Acceptance Criteria**:
- [ ] docs/getting-started/ exists with index
- [ ] docs/guides/integrators/ exists
- [ ] docs/guides/elearning/ exists
- [ ] docs/guides/contributors/ exists
- [ ] docs/reference/ exists (can be empty initially)
- [ ] docs/architecture/ exists with link to .s2s/decisions/

---

### DOCS-002: Choose static site generator

**Status**: draft | **Priority**: low | **Created**: 2026-01-29
**Blocked by**: Tech stack decision

**Context**: MkDocs Material or Docusaurus for docs hosting on GitHub Pages.

**Traceability**:
- **Implements**: REQ-012

**Acceptance Criteria**:
- [ ] Static site generator chosen
- [ ] Basic configuration in place
- [ ] Docs build integrated into CI

---

### DOCS-003: Configure API auto-generation

**Status**: draft | **Priority**: low | **Created**: 2026-01-29
**Blocked by**: Tech stack decision, initial code

**Context**: OpenAPI for HTTP APIs, docstrings for SDKs.

**Traceability**:
- **Implements**: REQ-013

**Acceptance Criteria**:
- [ ] OpenAPI spec generation configured
- [ ] Python docstring extraction configured
- [ ] Auto-generation runs in CI

---

### TECH-001: Setup uv workspace for monorepo

**Status**: planned | **Priority**: high | **Created**: 2026-01-29

**Context**: Python monorepo needs uv workspace configuration for dependency management.

**Acceptance Criteria**:
- [ ] Root pyproject.toml with [tool.uv.workspace]
- [ ] Each component is a workspace member
- [ ] `uv sync` works from root
- [ ] Cross-component imports work

---

### TECH-002: Create good-first-issue items

**Status**: planned | **Priority**: medium | **Created**: 2026-01-29
**Blocked by**: Initial code exists

**Context**: 5-10 good-first-issue items needed before public announcement.

**Traceability**:
- **Implements**: REQ-021

**Acceptance Criteria**:
- [ ] 5+ issues labeled "good first issue"
- [ ] Each issue has clear scope and acceptance criteria
- [ ] Mix of docs, tests, and small features

---

## In Progress

<!-- Move items here when work begins -->

---

## Completed

### INFRA-001: Create LICENSE file

**Status**: completed | **Completed**: 2026-01-29

**Context**: Apache 2.0 license file required at repo root.

**Traceability**:
- **Implements**: REQ-014

**Acceptance Criteria**:
- [x] LICENSE file at repo root

---

### INFRA-002: Create CODE_OF_CONDUCT.md

**Status**: completed | **Completed**: 2026-01-29

**Context**: Referenced by CONTRIBUTING.md. Standard for OSS projects.

**Traceability**:
- **Implements**: REQ-021 (community readiness)

**Acceptance Criteria**:
- [x] CODE_OF_CONDUCT.md at repo root (Contributor Covenant v2.1)

---

## Notes

**When to address each item:**

| Item | When | Reason |
|------|------|--------|
| ~~INFRA-001 (LICENSE)~~ | ~~Before /s2s:specs~~ | Done |
| ~~INFRA-002 (CoC)~~ | ~~Before /s2s:specs~~ | Done |
| INFRA-003 (CODEOWNERS) | After component structure | Needs paths to exist |
| INFRA-004 (component dirs) | Before /s2s:plan | Need structure for planning |
| DOCS-001 (docs structure) | Before Phase 1 complete | Part of MVP deliverable |
| DOCS-002, DOCS-003 | After tech stack | Depends on language/framework |
| DOCS-004 (traceability tables) | After first Phase 1 milestone | Accuracy requires real code |
| DOCS-005 (roundtable QA+BA) | After first sprint | Need tests to compare against |
| DOCS-006 (n8n workflow) | Anytime during Phase 1 | Independent of code, low effort |
| DOCS-007 (Phase 2 OQs) | Before Phase 2 design | OQ-018 + OQ-019 unresolved |
| DOCS-008 (no_relevant_context REQ) | Before Phase 1 SRS close | Small scope, low risk |
| TECH-001 (uv workspace) | Before coding | Dev environment setup |
| TECH-002 (good-first-issue) | Before announcement | Community readiness |
| TECH-003 (Phase 2 design roundtable) | After Phase 1 stable + DOCS-007 | Full /s2s:design for Phase 2 |
