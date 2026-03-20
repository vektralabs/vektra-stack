# Vektra Backlog

**Updated**: 2026-02-28
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

### DEBT-004: ~~Budget allocator input ordering unenforced~~

**Status**: completed | **Priority**: low | **Created**: 2026-02-19 | **Completed**: 2026-03-01
**Resolved in**: Phase 2 Wave 2 (core-pipeline-v2) — explicit sort in all 3 pipeline paths + test coverage

**Context**: `allocate_token_budget` docstring states that `chunks` must be passed in score-descending order ("sorted by score descending"). In `pipeline.execute()`, `chunk_inputs` is built from `filtered`, which is in original retrieval position order (not score order). This works in Phase 1 because PgvectorProvider returns results score-descending, but the VectorStoreProvider Protocol does not guarantee ordering. If a Phase 2 provider (e.g., Qdrant) returns results in a different order, the budget allocator may skip high-scoring chunks and include low-scoring ones.

**Traceability**: ARCH-055, vektra_core/budget.py, vektra_core/pipeline.py:287, VectorStoreProvider Protocol

**Acceptance Criteria**:
- [x] `pipeline.execute()` sorts `filtered` by score descending before constructing `chunk_inputs`
- [x] `pipeline._stream()` sorts identically
- [x] `advanced_pipeline._build_prompt()` sorts identically (covers both execute and stream)
- [x] `test_budget.py` covers unsorted-input scenario (`test_unsorted_input_selects_front_items`)

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

### DEBT-008: LRU cache stores plaintext API keys in memory

**Status**: planned | **Priority**: low | **Created**: 2026-02-28
**Blocked by**: Phase 2
**Origin**: PR #2 review, CodeRabbit comment 2867565397

**Context**: `verify_key()` in `vektra_admin/keys.py` uses `functools.lru_cache(maxsize=512)` keyed by `(key_hash, plaintext)`. The plaintext API key remains in the Python heap for the entire process lifetime (or until LRU eviction). `functools.lru_cache` is size-bounded only, not TTL-bounded. While the plaintext is already in memory during each request (Authorization header), the cache extends exposure from request-scoped to process-scoped. Replace with `cachetools.TTLCache` (e.g. TTL=300s, maxsize=512) to limit temporal exposure.

**Traceability**: REQ-023, ARCH-023, PR #2 comment 2867565397

**Acceptance Criteria**:
- [ ] Replace `functools.lru_cache` with `cachetools.TTLCache` in `_cached_verify`
- [ ] TTL configured via constant (default 300s)
- [ ] Cache key uses `(key_hash, plaintext)` as before (or hash-based fingerprint)
- [ ] Unit test verifies cache expiration after TTL
- [ ] `cachetools` added to vektra-admin dependencies

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

**Status**: completed | **Priority**: low | **Created**: 2026-02-17

**Context**: n8n is the external pipeline orchestrator for Vektra (configuration over fork, no scheduling logic inside Vektra). The periodic indexing pattern - e.g., daily sync of course materials from an LMS - is an open question in requirements.md (OQ: "Periodic indexing pattern: n8n orchestrates ingestion, but the scheduling pattern needs documentation as a reference workflow"). SC-A02 describes the ingestion side but not the n8n workflow definition. This is needed for the MVP operator experience (SC-H01 target: 30 minutes to first working query, which implies n8n integration should be documentable).

**Traceability**: requirements.md OQ (periodic indexing), SC-A02, REQ-005 (onboarding)

**Acceptance Criteria**:
- [x] Reference n8n workflow (JSON export) included in repository under docs/workflows/
- [x] Workflow performs: poll source -> compute SHA-256 -> skip if exists -> POST /ingest -> poll job status
- [x] README or guide explains how to import and configure the workflow
- [x] OQ closed in CONTEXT.md with pointer to the reference workflow

---

### DOCS-007: ~~Resolve Phase 2 open questions~~

**Status**: completed | **Priority**: high | **Created**: 2026-02-17 | **Completed**: 2026-03-01
**Resolved in**: ADR-0024, ADR-0025, ARCH-062/063/064

**Context**: Two open questions from requirements.md resolved before Phase 2 planning.

**OQ-018** (learn-ui + admin-ui architecture):
- Admin UI: HTMX + Jinja2 server-side rendering (ADR-0024, ARCH-062). Phase 3: migrate to separate SPA.
- Chatbot widget: self-contained JS bundle served by vektra-learn (ADR-0025, ARCH-063). Phase 3: extract to npm package.
- Design constraints documented for Phase 3 migration (no business logic in templates, REST API only, self-contained widget).

**OQ-019** (Phase 2 hardware minimum):
- 8GB RAM / 4 CPU formalized as Phase 2 minimum (ARCH-064). Requirements.md is closed; formalization in architecture.md instead of NFR-014.

**Traceability**: requirements.md OQ-018/OQ-019, architecture.md v1.10, CONTEXT.md, ADR-0024, ADR-0025

**Acceptance Criteria**:
- [x] OQ-018: architecture decision recorded (ADR-0025, ARCH-063) for widget deployment model
- [x] OQ-018: architecture decision recorded (ADR-0024, ARCH-062) for admin-ui deployment model
- [x] OQ-019: Phase 2 hardware target formalized in architecture.md (ARCH-064)
- [x] Both OQs marked as resolved in CONTEXT.md

---

### DOCS-008: ~~Evaluate adding REQ for no_relevant_context behavior~~

**Status**: completed | **Priority**: low | **Created**: 2026-02-17 | **Completed**: 2026-02-19
**Resolved in**: SRS v1.5.0 (requirements.md update)

**Context**: REQ-066 (no_relevant_context graceful fallback) was added to requirements.md during SRS v1.5.0. Traceability updated in traceability.yaml.

**Traceability**: ARCH-056, ADR-0021, SC-B05, REQ-066

**Acceptance Criteria**:
- [x] Decision made: REQ-066 added to requirements.md
- [x] If REQ added: traceability tables updated (ties to DOCS-004)
- [x] Gaps table row in validation-scenarios.md updated to reflect resolution

---

### TECH-003: Phase 2 implementation plans

**Status**: done | **Priority**: high | **Created**: 2026-02-17 | **Updated**: 2026-03-01
**Blocked by**: none (DOCS-007 resolved)
**Completed**: PR #21 (scoping plan), PR #22 (11 detailed plans, 168 tasks, codebase-validated)

**Context**: Phase 2 requirements and architecture are already formalized in the existing documents: requirements.md contains 44 Phase 2 references (EX-xxx exclusions, Phase 2 deferrals), architecture.md contains 115+ Phase 2 references (ARCH decisions with Phase 2 annotations, ADR-0014 AdvancedQueryPipeline, ADR-0023 query rewriting, ADR-0024/0025 UI decisions, etc.). A full `/s2s:design` roundtable is NOT needed. Only implementation plans via `/s2s:plan` are required.

Plan generation follows a three-phase approach (lesson learned from Phase 1):
1. Scoping plan: map features to work groups, define wave structure with provides/requires
2. Dependency validation: SPV L1 + L3 checks before detailed plans
3. Detailed plans: per-component plans with provides/requires from the start

**Traceability**: architecture.md section 11.3, requirements.md EX-xxx items, ADR-0009, ADR-0011, ADR-0024, ADR-0025, plans/INDEX-PHASE2.md

**Acceptance Criteria**:
- [x] DOCS-007 resolved (OQ-018, OQ-019)
- [x] Scoping plan generated and validated (SPV L1 + L3 pass)
- [x] Detailed plans generated with provides/requires YAML front-matter
- [x] INDEX-PHASE2.md populated with wave structure and dependency graph
- [x] New ADRs created as needed during planning (ADR-0024, ADR-0025)

---

### FEAT-001: Audit log expandable detail rows

**Status**: draft | **Priority**: low | **Created**: 2026-03-08
**Origin**: first Docker smoke test of admin UI (PR #29)

**Context**: The audit log table in `/admin/audit` displays 6 columns (timestamp, key_id, method, endpoint, status_code, action). Two additional fields are stored but not shown: `request_id` (UUID for log correlation) and `metadata` (JSONB with action-specific context like namespace, filename, document_id, error_code, chunk_count). Adding a click-to-expand detail row would surface this information without cluttering the table. Proposal emerged during initial testing and needs further analysis to determine scope, UX approach, and whether it justifies the added complexity.

**Traceability**: REQ-022, ARCH-062, ADR-0024

**Acceptance Criteria** (tentative, pending evaluation):
- [ ] Clicking an audit row expands an inline detail section (HTMX partial or `<details>`)
- [ ] Detail shows: full key_id, request_id, and metadata key-value pairs
- [ ] Metadata rendered as formatted key-value list (not raw JSON)
- [ ] Empty metadata (`{}`) shows "No additional details" or similar
- [ ] Works with cursor pagination (expanded state need not survive page navigation)

---

### FEAT-002: Admin UI edit forms for namespaces and API keys

**Status**: draft | **Priority**: low | **Created**: 2026-03-08
**Origin**: first Docker smoke test of admin UI (PR #29)

**Context**: The admin UI supports create/delete for namespaces and keys, but not editing existing records. Several DB-backed fields are already writable via the REST API but have no UI: namespace quotas (`quota_chunks`, `quota_documents`), retention policy (`retention_days`), namespace config (JSONB), and per-key rate limit (`rate_limit_rpm`). In a production scenario, an operator would need SSH or API calls to adjust these values. Proposal emerged during initial testing and needs further analysis to determine which fields are worth exposing, UX design for the edit forms, and validation requirements.

**Traceability**: REQ-006, REQ-048, ARCH-062, ADR-0024

**Acceptance Criteria** (tentative, pending evaluation):
- [ ] Namespace edit form: retention_days, quota_chunks, quota_documents, display_name
- [ ] API key edit form: rate_limit_rpm, label
- [ ] Inline edit or modal pattern consistent with existing HTMX approach
- [ ] Validation feedback on invalid values (e.g., negative retention)
- [ ] Corresponding REST API endpoints exist (verify or create as needed)

---

### TECH-004: ~~Add unique indexes for idempotent ingest (TOCTOU mitigation)~~

**Status**: completed | **Priority**: medium | **Created**: 2026-02-20 | **Completed**: 2026-03-01
**Resolved in**: Phase 2 Wave 0 (migration 0002_phase2_tables.py) + pipeline IntegrityError handling
**Origin**: PR #2 review, comment 2834065202 (CodeRabbit)

**Context**: `run_ingest()` checks for duplicate filename+namespace via SELECT before INSERT. Without a unique partial index (`WHERE deleted_at IS NULL`), concurrent requests can both pass the check and insert duplicate documents (TOCTOU window). The fix requires a partial unique index on `(namespace_id, filename) WHERE deleted_at IS NULL` plus `IntegrityError` handling as a fallback. Deferred because it requires an Alembic migration (infra-database plan, Wave 5).

**Traceability**: REQ-033, ARCH-052, PR #2 comment 2834065202

**Acceptance Criteria**:
- [x] Alembic migration adds `CREATE UNIQUE INDEX ... ON source_documents (namespace_id, filename) WHERE deleted_at IS NULL`
- [x] `run_ingest()` catches `IntegrityError` from duplicate insert and raises `IngestConflictError`
- [ ] Integration test verifies concurrent duplicate ingest returns 409 (deferred, not blocking)

---

### INFRA-003: ~~Create CODEOWNERS file~~

**Status**: completed | **Priority**: medium | **Created**: 2026-01-29 | **Completed**: 2026-02-19
**Resolved in**: Wave 0, plan 20260217-infra-003

**Context**: Maps components to maintainers for automatic PR review assignment.

**Traceability**:
- **Implements**: REQ-016, REQ-018

**Acceptance Criteria**:
- [x] CODEOWNERS file at repo root
- [x] Each component has owner defined
- [x] Docs ownership follows component ownership

---

### INFRA-004: ~~Create component directories~~

**Status**: completed | **Priority**: high | **Created**: 2026-01-29 | **Completed**: 2026-02-19
**Resolved in**: Wave 0 (commit 4d6d375)

**Context**: All component directories created with full package structure during Wave 0.

**Traceability**:
- **Implements**: ADR-0001

**Acceptance Criteria**:
- [x] vektra-core/ with README.md and pyproject.toml stub
- [x] vektra-ingest/ with README.md and pyproject.toml stub
- [x] vektra-index/ with README.md and pyproject.toml stub
- [x] vektra-admin/ with README.md stub

---

### DOCS-001: ~~Create documentation structure~~

**Status**: completed | **Priority**: medium | **Created**: 2026-01-29 | **Completed**: 2026-02-27
**Resolved in**: Wave 7, PR #10

**Context**: Diataxis-based docs structure per REQ-007, REQ-010.

**Traceability**:
- **Implements**: REQ-007, REQ-008, REQ-009, REQ-010

**Acceptance Criteria**:
- [x] docs/getting-started/ exists with index
- [x] docs/guides/integrators/ exists
- [ ] docs/guides/elearning/ exists (Phase 2)
- [x] docs/guides/contributors/ exists
- [x] docs/reference/ exists
- [x] docs/architecture/ exists with link to .s2s/decisions/

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

### TECH-001: ~~Setup uv workspace for monorepo~~

**Status**: completed | **Priority**: high | **Created**: 2026-01-29 | **Completed**: 2026-02-19
**Resolved in**: Wave 0 (CI/CD setup, commit cdec18b)

**Context**: uv workspace configured in root pyproject.toml. All components are workspace members. Cross-component imports work via shared vektra_shared package.

**Acceptance Criteria**:
- [x] Root pyproject.toml with [tool.uv.workspace]
- [x] Each component is a workspace member
- [x] `uv sync` works from root
- [x] Cross-component imports work

---

### TECH-002: Create good-first-issue items

**Status**: planned | **Priority**: medium | **Created**: 2026-01-29
**Blocked by**: Before public announcement

**Context**: 5-10 good-first-issue items needed before public announcement.

**Traceability**:
- **Implements**: REQ-021

**Acceptance Criteria**:
- [ ] 5+ issues labeled "good first issue"
- [ ] Each issue has clear scope and acceptance criteria
- [ ] Mix of docs, tests, and small features

---

### FEAT-005: LLM fallback for no-context queries (greetings, courtesies, off-topic)

**Status**: draft | **Priority**: medium | **Created**: 2026-03-17
**Origin**: Moodle integration testing — greetings like "ciao", "buongiorno" trigger `no_relevant_context` and produce empty/unhelpful responses

**Context**: Both `SimpleQueryPipeline` and `AdvancedQueryPipeline` short-circuit when no chunks pass the relevance threshold (`min_relevance_score`): they return `answer: null` + `no_relevant_context: true` without ever calling the LLM. This is correct for retrieval quality (ARCH-056, REQ-066) — the system should not hallucinate answers from non-relevant chunks.

However, for the learn chatbot widget (and any conversational interface), this creates a poor UX for:
- **Greetings and courtesies**: "ciao", "buongiorno", "come stai?" — the assistant should acknowledge and redirect to course materials
- **Meta questions**: "chi sei?", "cosa puoi fare?" — the assistant should explain its role
- **Off-topic but harmless**: "che ore sono?" — the assistant should politely decline

The existing **query rewriting** (ADR-0023, `AdvancedQueryPipeline`) only activates when conversation history exists and resolves pronoun references — it does not help with greetings.

The existing **SafeguardHook** (`pre_query`, `post_retrieval`, `pre_response`) could catch prompt injection and abusive content at the `pre_query` stage, but with `VEKTRA_SAFEGUARD_MODE=passthrough` they are no-ops. Even with safeguards active, they filter/block — they don't generate friendly responses.

**Proposed approach**: When `no_relevant_context` is detected, instead of short-circuiting, invoke the LLM with a modified system prompt that instructs it to respond to greetings, explain its role, and redirect to course-related questions — without fabricating information from missing context. This keeps the retrieval quality gate intact while allowing the LLM to handle conversational basics.

**Alternatives considered**:
- **Intent classification pre-retrieval**: separate LLM call to classify intent before retrieval. More precise but adds latency and cost for every query.
- **Client-side pattern matching**: widget detects greetings and responds locally. Fragile, language-dependent, doesn't help other clients.

**Traceability**: ARCH-056, REQ-066, ADR-0021, ADR-0025

**Acceptance Criteria** (tentative):
- [ ] Greetings/courtesies receive a friendly response acknowledging the user and explaining the assistant's role
- [ ] Off-topic queries receive a polite redirect to course-related questions
- [ ] The LLM is NOT given fabricated context — it knows no relevant chunks were found
- [ ] Retrieval quality gate unchanged — `no_relevant_context` flag still set in QueryTrace
- [ ] Safeguard hooks still apply (pre_query can block before LLM call)
- [ ] Works with both SimpleQueryPipeline and AdvancedQueryPipeline
- [ ] System prompt for no-context fallback is configurable via Jinja2 template

---

### FEAT-007: Markdown rendering in widget chat messages

**Status**: draft | **Priority**: medium | **Created**: 2026-03-20
**Origin**: Moodle integration testing (2026-03-20)

**Context**: The learn chatbot widget (`vektra-chat.js`) renders all messages as plain text via `textContent`. LLM responses typically contain Markdown formatting (bold, italic, lists, code blocks, headings) which is displayed as raw syntax. This makes responses harder to read, especially for structured answers with bullet points or code examples.

The widget is deliberately vanilla JS with zero dependencies (ADR-0025). Adding Markdown rendering requires either a lightweight library (e.g., `marked`, ~7KB minified) or a minimal custom parser for the most common patterns.

**Scope**: only assistant messages need rendering. User messages stay as plain text. Sources section is already structured HTML.

**Security**: rendered HTML must be sanitized to prevent XSS. The LLM output is not user-controlled but defense in depth applies. Use a sanitizer or restrict to a safe subset of HTML tags.

**Traceability**: ADR-0025, ARCH-063

**Acceptance Criteria** (tentative):
- [ ] Assistant messages render bold, italic, lists (ordered/unordered), code inline/blocks, and headings
- [ ] User messages remain plain text
- [ ] Streaming tokens render progressively (Markdown applied incrementally or on completion)
- [ ] Output is sanitized against XSS
- [ ] Widget bundle size increase is documented and reasonable (<10KB)

---

### FEAT-006: Widget error feedback when Vektra API is unreachable

**Status**: draft | **Priority**: medium | **Created**: 2026-03-20
**Origin**: Moodle integration testing on remote machine (2026-03-20)

**Context**: When the chatbot widget JS (`vektra-chat.js`) cannot reach the Vektra API (missing SSH tunnel, CORS misconfiguration, Vektra container down), the floating chat button silently fails to appear. No error is shown to the user or the admin. The Moodle block still displays "AI Assistant is active" because the server-side token generation succeeded (PHP runs inside Docker, reaches Vektra on the internal network), but the browser-side widget cannot load or connect.

This makes troubleshooting difficult: the admin sees "active" but students see nothing. The root cause (network/CORS/port) is invisible without opening browser dev tools.

**Proposed approach**: The widget JS should detect load/connection failures and surface them:
- If the script loads but cannot reach the API (fetch error, CORS block): show a subtle error state in the chat button or a dismissible banner
- If the script itself fails to load (network error): the Moodle plugin could add a `<noscript>`-style fallback or an `onerror` handler on the script tag to display an admin-visible message

**Traceability**: ADR-0025, ARCH-063

**Acceptance Criteria** (tentative):
- [ ] Widget shows visible feedback when Vektra API is unreachable from the browser
- [ ] Admin users see a diagnostic message (not just silent failure)
- [ ] Normal users see a non-technical "assistant unavailable" message
- [ ] No false positives during normal page load latency

---

## In Progress

### BUG-011: Ingest pipeline does not generate sparse embeddings for hybrid search

**Status**: in_progress | **Priority**: high | **Created**: 2026-03-17
**Origin**: RAG tuning testing — combo B (hybrid search with BM25)

**Context**: The `SparseEmbeddingProvider` (fastembed-bm25) is correctly registered at startup and the Qdrant collection is created with sparse vector support (`sparse` named vector with IDF modifier). However, the ingest pipeline (`vektra_ingest/pipeline.py` `run_ingest()`) only calls the dense `EmbeddingProvider.embed_documents()` and constructs `ChunkEmbedding` objects without the `sparse` field. The `ChunkEmbedding` dataclass already supports `sparse: SparseVector | None = None` and the Qdrant provider correctly stores sparse vectors when present (`qdrant.py:138-142`). The gap is solely in the ingest pipeline: it doesn't call `SparseEmbeddingProvider.embed_documents()`.

The `AdvancedQueryPipeline` correctly calls `SparseEmbeddingProvider.embed_query()` at query time (step 2: `sparse_embed`), but finds no sparse vectors in the stored points, making hybrid search effectively dense-only.

**Traceability**: ARCH-053, ADR-0021

**Fix**: Add sparse embedding generation in `run_ingest()` between dense embedding and `ChunkEmbedding` construction. Check `registry.has("sparse_embedding", "default")`, call `embed_documents(texts)`, pass results as `sparse=` parameter.

---

### BUG-010: Learn query endpoint does not auto-create conversation on first query

**Status**: in_progress | **Priority**: high | **Created**: 2026-03-16
**Origin**: Moodle integration testing (2026-03-16)

**Context**: The learn query endpoint (`POST /api/v1/learn/query`) passes `conversation_id` through to the pipeline unchanged. When the widget sends the first query without a `conversation_id` (which is the normal flow), the pipeline receives `None`, skips history retrieval and turn saving, and returns `conversation_id: null`. The widget receives `null` and has nothing to save — so the second query also has no `conversation_id`. Result: **every query is a single-turn query with no conversation continuity**.

REQ-049 states: "Response includes conversation_id for client to maintain continuity." The learn query endpoint should auto-generate a `conversation_id` (UUID) on the first query when the client doesn't provide one, save the turn, and return the ID so the widget can reuse it.

The core API (`POST /api/v1/query`) has the same design — it's documented as "If conversation_id omitted, single-turn query" — but for the learn widget UX, multi-turn is the expected default behavior.

**Traceability**: REQ-049, ADR-0025, ARCH-063

**Acceptance Criteria**:
- [ ] `course_query()` generates a `uuid4()` conversation_id when the request omits it
- [ ] The generated ID is passed to the pipeline, which creates the conversation and saves the first turn
- [ ] The response includes the generated `conversation_id`
- [ ] Subsequent queries from the widget include the `conversation_id` and get history context
- [ ] When `conversation_id` IS provided by the client, behavior unchanged
- [ ] Test covers both paths (auto-generated vs client-provided)

---

### FEAT-004: Widget conversation lifecycle improvements

**Status**: draft | **Priority**: medium | **Created**: 2026-03-16
**Origin**: Moodle integration testing (2026-03-16)
**Depends on**: BUG-010

**Context**: After BUG-010 is fixed, the widget will support multi-turn conversations within a single page load. However, the `conversation_id` lives only in JS memory (`ApiClient._conversationId`) and is lost on page refresh, navigation, or tab close. Additionally, there is no explicit way for the user to start a fresh conversation. These are UX improvements to evaluate for the learn chatbot widget.

**Areas to evaluate**:
- **Persist conversation_id across page refresh**: use `sessionStorage` (scoped to tab) so refresh doesn't break the conversation. New tab = new conversation.
- **Persist across same-course navigation**: if the student navigates between pages of the same course, the widget could maintain continuity via `sessionStorage` keyed by course_id.
- **"New chat" button**: add a button in the widget header to explicitly reset the conversation. Clears `_conversationId` and message history in DOM.
- **Token expiry vs conversation continuity**: when the JWT expires (1h default) and Moodle generates a new one, the conversation_id is not in the JWT — so old conversations remain accessible. Decide if this is desired or if token refresh should start a new conversation.
- **Max idle timeout**: consider auto-starting a new conversation after N minutes of inactivity (e.g. 30min), even if the page stays open.

**Traceability**: REQ-049, ADR-0025, ARCH-063

**Acceptance Criteria** (tentative, pending evaluation):
- [ ] conversation_id survives page refresh within same tab
- [ ] "New chat" button available in widget header
- [ ] Decision documented on token expiry behavior
- [ ] Decision documented on idle timeout behavior

---

### FEAT-003: Optional enrollment — trust external identity providers for learn queries

**Status**: in_progress | **Priority**: high | **Created**: 2026-03-16
**Origin**: Moodle integration experience (vektra-moodle plugin)

**Context**: The learn module currently requires a Vektra enrollment record for every student+course pair before allowing queries. This creates friction in LMS integrations where the LMS already manages enrollment and authorization. The JWT is signed server-side with the admin API key, contains `student_id` + `course_id`, and has short TTL (1h default). By the time a query arrives with a valid JWT, the student is already authorized by the upstream system.

**Implementation**: `VEKTRA_LEARN_REQUIRE_ENROLLMENT` flag (default `true`). When `false`, the learn query endpoint skips enrollment lookup and derives namespace from the JWT (`namespace` claim or `course_id` fallback). Token generation accepts an optional `namespace` field to override the convention.

**Traceability**: REQ-031, ADR-0010, ADR-0025, ARCH-063

**Acceptance Criteria**:
- [x] `VEKTRA_LEARN_REQUIRE_ENROLLMENT` flag added to VektraSettings (default `true`)
- [x] Token generation accepts optional `namespace` in TokenRequest
- [x] JWT payload includes `namespace` when provided
- [x] Query endpoint: when flag=false, derive namespace from JWT `namespace` field or fallback to `course_id`
- [x] Query endpoint: when flag=true, current enrollment-based behavior unchanged
- [x] Conversation history and audit log still capture `student_id` from JWT
- [x] Tests cover both paths (enrollment required vs optional)
- [x] `.env.example` updated

---

## Completed

### BUG-009: ~~Ingest should auto-create namespace if it doesn't exist~~

**Status**: completed | **Priority**: medium | **Created**: 2026-03-16 | **Completed**: 2026-03-16
**Origin**: Integration testing (2026-03-13), Moodle integration (2026-03-16)
**Resolved in**: Already implemented in Phase 2 — `run_ingest()` (pipeline.py:169-176) uses `pg_insert(...).on_conflict_do_nothing()`. `LearnService.create_enrollment()` uses the same pattern.

**Traceability**: REQ-033, ARCH-047

**Acceptance Criteria**:
- [x] `POST /api/v1/ingest` auto-creates namespace row if not found
- [x] `POST /api/v1/learn/content/ingest` does the same (calls `run_ingest()`)
- [x] Auto-created namespace has empty config, no quotas
- [x] If namespace already exists, no-op (idempotent)

---

### BUG-007: Ingest error responses not using ErrorResponse envelopes

**Status**: completed | **Completed**: 2026-02-20
**Resolved in**: PR #2 review, commit baa7d18

**Context**: `IngestConflictError` (409) and `IngestError` (422) handlers in `vektra_ingest/api.py` returned raw dicts instead of `ErrorResponse.to_envelope()` format (REQ-010). Same pattern as BUG-004 in vektra-admin. Fixed: both now use structured ErrorResponse envelopes with category, code, message, and remediation. Conflict uses hardcoded 409 since `ERR_INGEST_001` is shared with unsupported type (422).

**Traceability**: REQ-010, PR #2 comment 2834065198

---

### BUG-008: Embedding count not validated against chunk count before storage

**Status**: completed | **Completed**: 2026-02-20
**Resolved in**: PR #2 review, commit 4985eb5

**Context**: `run_ingest()` in `vektra_ingest/pipeline.py` used `zip(all_chunks, embeddings)` to build `ChunkEmbedding` objects. If the embedding provider returned fewer embeddings than chunks, `zip` would silently truncate - a data loss risk. Added explicit length check before the zip.

**Traceability**: ARCH-009, PR #2 comment 2834065217

---

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

### BUG-005: model.encode() blocks event loop in embedding provider

**Status**: completed | **Completed**: 2026-02-20
**Resolved in**: PR #2 review, commit 3213337

**Context**: `SentenceTransformersProvider.embed_documents()` and `embed_query()` called `model.encode()` synchronously inside `async def` methods. This CPU-intensive operation blocked the event loop for all concurrent requests. Fixed: both methods now use `asyncio.to_thread()` to offload inference to the default thread pool.

**Traceability**: ADR-0013, PR #2 comments 2833984653, 2833984670, 2834065193

---

### BUG-006: document_id consistency not validated across chunks in store()

**Status**: completed | **Completed**: 2026-02-20
**Resolved in**: PR #2 review, commit 4e01c52

**Context**: `VectorStoreServiceAdapter.store()` only validated `chunks[0].metadata["document_id"]` but did not check that all chunks carried the same document_id. If mixed document_ids were passed, the mismatch would be silently ignored. Fixed: all chunks are now validated to share the same document_id before write.

**Traceability**: ARCH-052, PR #2 comment 2834065173

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
| ~~INFRA-003 (CODEOWNERS)~~ | ~~After component structure~~ | Done (Wave 0) |
| ~~INFRA-004 (component dirs)~~ | ~~Before /s2s:plan~~ | Done (Wave 0) |
| ~~DOCS-001 (docs structure)~~ | ~~Before Phase 1 complete~~ | Done (Wave 7, PR #10) |
| DOCS-002, DOCS-003 | After tech stack | Depends on language/framework |
| DOCS-004 (traceability tables) | After first Phase 1 milestone | Accuracy requires real code |
| DOCS-005 (roundtable QA+BA) | After first sprint | Need tests to compare against |
| ~~DOCS-006 (n8n workflow)~~ | ~~Anytime during Phase 1~~ | Done (Wave 7, PR #10) |
| ~~DOCS-007 (Phase 2 OQs)~~ | ~~Before Phase 2 design~~ | Done (ADR-0024, ADR-0025, ARCH-064) |
| ~~DOCS-008 (no_relevant_context REQ)~~ | ~~Before Phase 1 SRS close~~ | Done (REQ-066 in SRS v1.5.0) |
| ~~TECH-001 (uv workspace)~~ | ~~Before coding~~ | Done (Wave 0) |
| TECH-002 (good-first-issue) | Before announcement | Community readiness |
| ~~TECH-003 (Phase 2 plans)~~ | ~~After DOCS-007~~ | Done (PR #21 + PR #22, 11 plans, 168 tasks) |
| FEAT-001 (audit detail rows) | Post-Phase 2 or spare time | Draft, needs evaluation |
| FEAT-002 (namespace/key edit) | Post-Phase 2 or spare time | Draft, needs evaluation |
| TECH-004 (unique indexes) | Anytime (infra-database done) | Alembic migration ready |
| ~~DEBT-001 (stream budget)~~ | ~~Phase 2~~ | Fixed in PR #2 review (e527ce1) |
| DEBT-002 (stream trace) | Phase 2 | Observability gap, not blocking |
| DEBT-003 (post_retrieval hook) | Phase 2 | PassthroughSafeguard covers Phase 1 |
| DEBT-004 (budget ordering) | Phase 2 | Pgvector returns score-desc in practice |
| DEBT-005 (disconnect cancel) | Phase 2 | uvicorn handles it implicitly |
| DEBT-008 (LRU plaintext cache) | Phase 2 | Replace lru_cache with TTLCache |
