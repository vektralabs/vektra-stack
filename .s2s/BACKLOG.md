# Vektra Backlog

**Updated**: 2026-01-29
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
| TECH-001 (uv workspace) | Before coding | Dev environment setup |
| TECH-002 (good-first-issue) | Before announcement | Community readiness |
