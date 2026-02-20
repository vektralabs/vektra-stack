# Vektra Backlog

**Updated**: 2026-02-20
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

### DEBT-001: ~~`_stream()` skips token budget allocation~~

**Status**: completed | **Priority**: low | **Created**: 2026-02-19 | **Completed**: 2026-02-20
**Resolved in**: PR #2 review, commit e527ce1

**Context**: `SimpleQueryPipeline._stream()` (`vektra_core/pipeline.py`) built the prompt with all filtered chunks without applying `allocate_token_budget`. Fixed: `_stream()` now applies the same budget allocation logic as `execute()`.

**Traceability**: ARCH-055 (token budget allocation), vektra_core/pipeline.py `_stream()`

**Acceptance Criteria**:
- [x] `_stream()` applies the same `allocate_token_budget` logic as `execute()` before building the prompt
- [ ] Streaming test covers budget-constrained scenario (many chunks, tight context window)

---

### DEBT-002: `_stream()` emits no QueryTrace

**Status**: planned | **Priority**: low | **Created**: 2026-02-19
**Blocked by**: Phase 2
**PR #2 review**: Confirmed as deferred. Fixing requires collecting step timings across the async generator lifecycle, which is a structural change. Comments 2833984647, nitpick pipeline.py:414-437.

**Context**: `SimpleQueryPipeline._stream()` does not collect `StepTrace` entries and does not emit a `QueryTrace` via structlog. Streaming requests are therefore invisible to ARCH-041 (per-step timing observability). The non-streaming `execute()` path emits a full `QueryTrace`.

**Traceability**: ARCH-041 (QueryTrace structure), REQ-060, vektra_core/pipeline.py `_stream()`

**Acceptance Criteria**:
- [ ] `_stream()` collects step timing (embed, search, filter, build_prompt, llm_call) after stream completes
- [ ] QueryTrace emitted via structlog after full stream is consumed
- [ ] QueryTrace for streaming queries appears in structured log output

---

### DEBT-003: `post_retrieval` safeguard trust boundary not called

**Status**: planned | **Priority**: low | **Created**: 2026-02-19
**Blocked by**: Phase 2 (PassthroughSafeguard covers Phase 1)
**PR #2 review**: Confirmed as deferred. Adding the boundary requires calling `post_retrieval` in both `execute()` and `_stream()` after retrieval filter, plus implementing chunk filtering via `SafeguardResult.filtered_ids`. Acceptable for Phase 1 with PassthroughSafeguard. Comment 2833984647.

**Context**: ARCH-049 defines 3 SafeguardHook trust boundary points: `pre_query` (called in `api.py`), `post_retrieval` (not called anywhere), `pre_response` (called in `pipeline.execute()` and `pipeline._stream()`). The middle boundary - triggered after chunks are retrieved and before the prompt is built - is entirely absent. This means chunk-level PII filtering or namespace isolation checks are not enforced.

**Traceability**: ARCH-049 (safeguard content modification), REQ-044, vektra_shared/protocols.py SafeguardHook

**Acceptance Criteria**:
- [ ] `pipeline.execute()` calls `safeguard.post_retrieval(chunk_ids, sg_ctx)` after retrieval filter, before build_prompt
- [ ] `pipeline._stream()` calls the same boundary
- [ ] SafeguardHook Protocol documents the expected signature for `post_retrieval`
- [ ] PassthroughSafeguard implements `post_retrieval` as a no-op

---

### DEBT-004: Budget allocator input ordering unenforced

**Status**: planned | **Priority**: low | **Created**: 2026-02-19
**Blocked by**: Phase 2 (pgvector returns score-descending results, so convention holds in Phase 1)

**Context**: `allocate_token_budget` docstring states that `chunks` must be passed in score-descending order ("sorted by score descending"). In `pipeline.execute()`, `chunk_inputs` is built from `filtered`, which is in original retrieval position order (not score order). This works in Phase 1 because PgvectorProvider returns results score-descending, but the VectorStoreProvider Protocol does not guarantee ordering. If a Phase 2 provider (e.g., Qdrant) returns results in a different order, the budget allocator may skip high-scoring chunks and include low-scoring ones.

**Traceability**: ARCH-055, vektra_core/budget.py, vektra_core/pipeline.py:287, VectorStoreProvider Protocol

**Acceptance Criteria**:
- [ ] Either: `pipeline.execute()` sorts `filtered` by score descending before constructing `chunk_inputs` and remaps indices correctly
- [ ] Or: VectorStoreProvider Protocol documents that `search()` results must be score-descending, and all implementations enforce it
- [ ] `test_budget.py` covers unsorted-input scenario to verify behavior

---

### DEBT-005: Client disconnect doesn't explicitly cancel LLM coroutine

**Status**: planned | **Priority**: low | **Created**: 2026-02-19
**Blocked by**: Phase 2

**Context**: The plan acceptance criterion states "Client disconnect cancels LLM request (no orphan async task)". The current `_sse_generator()` in `api.py` relies entirely on Starlette/uvicorn to propagate client disconnects as generator cancellation. There is no explicit `asyncio.CancelledError` handling or `request.is_disconnected()` polling inside the streaming path. In practice uvicorn does cancel the generator on disconnect, but this is not guaranteed across all ASGI servers or under all conditions (e.g., slow clients, buffered responses).

**Traceability**: REQ-042 (streaming), vektra_core/api.py `_sse_generator()`

**Acceptance Criteria**:
- [ ] `_sse_generator()` or `_stream()` polls `request.is_disconnected()` periodically during token streaming
- [ ] On disconnect detected, the LLM stream iterator is explicitly closed (`aclose()`)
- [ ] Test verifies no orphan task after simulated client disconnect

---

### DEBT-006: Ingest job phase never advances beyond 'extracting'

**Status**: planned | **Priority**: low | **Created**: 2026-02-19
**Blocked by**: Phase 2 (monitoring gap acceptable for Phase 1)

**Context**: `ingest_document_task` sets `phase='extracting'` once at the start and never updates it to `'chunking'` or `'embedding'` during execution. The acceptance criterion in the component-ingest plan says "Job status endpoint returns phase field ('extracting', 'chunking', 'embedding') during processing." `run_ingest()` is a monolithic call with no progress callback, so the phase cannot be updated mid-execution without restructuring the pipeline.

**Traceability**: REQ-014 (job status), vektra_ingest/jobs.py `ingest_document_task`, NFR-010

**Acceptance Criteria**:
- [ ] `run_ingest()` accepts an optional progress callback or is split into phases
- [ ] `ingest_document_task` updates phase to `'chunking'` after extraction and `'embedding'` after chunking
- [ ] Test verifies phase sequence: processing/extracting → processing/chunking → processing/embedding → indexed

---

### DEBT-007: Audit log not written for ingest failure responses (409, 422)

**Status**: planned | **Priority**: low | **Created**: 2026-02-19
**Blocked by**: Needs investigation of BackgroundTasks behavior with HTTPException

**Context**: `_write_audit_log` is called only on successful ingest (200) and async enqueue (202) paths. When `IngestConflictError` → 409 or `IngestError` → 422, no audit entry is written. The plan requires audit for all ingest outcomes. Note: `BackgroundTasks` added before an `HTTPException` is raised may not execute (FastAPI creates a new Response for error handlers). Fixing this requires either awaiting `log_event` directly in error paths or restructuring the exception handling.

**Traceability**: REQ-038 (audit log), NFR-007, vektra_ingest/api.py `ingest()`

**Acceptance Criteria**:
- [ ] `log_event` is called for 409 (conflict) and 422 (extraction error) responses
- [ ] Test verifies audit entries are written for all ingest outcomes
- [ ] Solution handles BackgroundTasks + HTTPException correctly (direct await or try/finally pattern)

---

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

### BUG-001: `pre_response` safeguard receives UUID instead of answer text

**Status**: completed | **Completed**: 2026-02-20
**Resolved in**: PR #2 review, commit e527ce1

**Context**: `pipeline.execute()` passed `str(response_id)` (a UUID) to `SafeguardHook.pre_response()`, making PII anonymization impossible (ADR-0018, ARCH-049). The Protocol parameter `response_ref: str` was ambiguous, but `pre_query` already passes actual query text. Fixed: now passes `answer or ""`.

**Traceability**: ADR-0018, ARCH-049, PR #2 comment 2833984639

---

### BUG-002: TOCTOU race in bootstrap key consumption

**Status**: completed | **Completed**: 2026-02-20
**Resolved in**: PR #2 review, commit 1e1cfde

**Context**: `is_bootstrap_consumed()` in `vektra_admin/bootstrap.py` performed a SELECT without `FOR UPDATE`, allowing two concurrent bootstrap requests to both see the key as unconsumed. Fixed: added `.with_for_update()` to the SELECT, matching the docstring's documented behavior.

**Traceability**: REQ-036, PR #2 comment 2834065162

---

### BUG-003: Audit-log gap for bootstrap key usage

**Status**: completed | **Completed**: 2026-02-20
**Resolved in**: PR #2 review, commit 1de0930

**Context**: Bootstrap key creates the first admin API key, but no audit log entry was written for this operation because `key_info` is None during bootstrap (no pre-existing key). Fixed: uses `UUID(int=0)` sentinel as `key_id` and `"apikey_created_bootstrap"` as action. AuditLogOrm.key_id is not a FK, so the sentinel is safe.

**Traceability**: NFR-007, REQ-038, PR #2 comments 2834065155, 2833984674

---

### BUG-004: Error responses not using ErrorResponse envelopes (admin)

**Status**: completed | **Completed**: 2026-02-20
**Resolved in**: PR #2 review, commit 1de0930

**Context**: Three error paths in `vektra_admin/api.py` returned raw dicts instead of `ErrorResponse.to_envelope()` format (REQ-010). Fixed: invalid scopes (ERR-ADMIN-001, 422), key not found (ERR-ADMIN-002, 404), and key already revoked (ERR-ADMIN-003, 409) now use structured ErrorResponse envelopes.

**Traceability**: REQ-010, PR #2 comment 2833984683

---

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
| ~~DEBT-001 (stream budget)~~ | ~~Phase 2~~ | Fixed in PR #2 review (e527ce1) |
| DEBT-002 (stream trace) | Phase 2 | Observability gap, not blocking |
| DEBT-003 (post_retrieval hook) | Phase 2 | PassthroughSafeguard covers Phase 1 |
| DEBT-004 (budget ordering) | Phase 2 | Pgvector returns score-desc in practice |
| DEBT-005 (disconnect cancel) | Phase 2 | uvicorn handles it implicitly |
