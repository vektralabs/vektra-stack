# Plan: v0.5.0 - Widget production-ready + instructor configuration

**Status**: active
**Plan ID**: 20260418-v050-widget-and-prof-config
**Branch**: feat/v050-widget-and-prof-config
**Created**: 2026-04-18T12:40:00Z
**Updated**: 2026-04-18T12:40:00Z
**Source**: v0.5.0 milestone scoping discussion (2026-04-18)
**Source type**: milestone

## Traceability

- **Requirements**: REQ-049 (multi-turn conversations), REQ-053 (QueryPipeline Protocol), REQ-066 (no relevant context)
- **Architecture**: ARCH-047 (namespace metadata as JSONB), ARCH-063 (widget architecture), ARCH-054 (composable prompt templates)
- **Decisions**: ADR-0025 (learn chatbot widget as backend-served JS bundle), ADR-0020 (prompt template architecture), ADR-0010 (authentication gateway)
- **Backlog items**: FEAT-016, FEAT-012, FEAT-004
- **Related (deferred)**: FEAT-008 (per-namespace prompt templates), FEAT-014 (source visibility), FEAT-022 (suggested questions), FEAT-023 (socratic mode)
- **Builds on**: FEAT-020 (grounding mode already reads from `namespaces.config`)
- **Dependencies**: none (all infrastructure in place)

## Overview

v0.5.0 closes the gap between "widget works" (v0.4.0) and "widget is production-ready for real courses". Three user-visible gaps remain:

1. **White-label** (FEAT-016): every course looks identical. Universities need their own title, color, welcome message.
2. **Citation clarity** (FEAT-012): sources show `chunk_id` UUIDs — meaningless to students. Document filename must be shown.
3. **Conversation persistence** (FEAT-004): refresh or accidental tab close loses the conversation. Unacceptable for courses where students ask follow-up questions.

Plus one foundational backend addition: **instructor configuration API** — professors need to toggle grounding mode (strict/hybrid) per course from inside Moodle without admin intervention. The `namespaces.config` JSONB column and pipeline resolution already exist (FEAT-020); only the write API and the Moodle integration are missing.

**Repositories touched**: vektra-stack (this plan), vektra-moodle (separate plan in the plugin repo).

**Not in scope**: FEAT-022 (suggested questions), FEAT-023 (socratic mode), FEAT-014 (source visibility toggle), FEAT-008 (per-namespace prompt templates), full admin UI for namespace config. These are deliberately deferred.

## Architecture decisions for this plan

### Two-category configuration model

All course customization splits cleanly into two categories, persisted in different places:

| Category | Examples | Storage | Read by | Write path |
|---|---|---|---|---|
| **(A) Visual** | title, color, welcome message, icon | Moodle block config | Widget via `data-*` attrs | Moodle block edit form → stays in Moodle |
| **(B) Behavioral** | `grounding_mode`, future `show_sources`, `top_k` override | `namespaces.config` JSONB | Vektra pipeline server-side | Moodle block edit form → AJAX → `PATCH /api/v1/namespaces/{id}/config` |

Rationale: behavioral config must be server-enforced to prevent clients from bypassing (e.g., a browser can't be trusted to enforce strict mode). Visual config has no security implications and can stay in the host plugin.

### Authentication model for namespace PATCH

Same trust model already used for token generation (FEAT-003):

1. Moodle plugin holds `VEKTRA_API_KEY` (admin scope) in its server-side PHP config.
2. Moodle's native auth verifies "user X is a teacher of course Y" before showing/accepting the block edit form.
3. When a teacher saves the form, Moodle PHP calls `PATCH /api/v1/namespaces/{course_id}/config` authenticated with the admin API key.
4. Vektra does not know individual professors. It trusts the upstream auth (same philosophy as `VEKTRA_LEARN_REQUIRE_ENROLLMENT=false`).

No new Vektra-side auth primitive needed. No "teacher scope" API key type to invent.

### Conversation persistence: tab-scoped, no cross-device

- Storage: `sessionStorage` keyed by `course_id` (tab-scoped per browser tab, wiped on tab close).
- Not `localStorage`: shared computers in university labs must not leak conversations across students.
- Not server-side "last conversation for this student": avoids privacy debate and keeps the widget stateless from Vektra's perspective (conversation turns are already persisted encrypted server-side per ADR-0011, this is just a UI convenience).
- On load: if `sessionStorage[course_id]` has a `conversation_id`, widget fetches turns from `GET /api/v1/conversations/{id}/turns` and renders them before the user types.
- "New chat" button: explicit reset (clears sessionStorage + DOM).

## Work items

### WI-1 - Backend: GET /api/v1/conversations/{id}/turns (JWT-scoped)

FEAT-004 prerequisite. Admin equivalent already exists at `vektra-admin/src/vektra_admin/api.py:463`; this is the student-scoped mirror in `vektra-learn`.

**Endpoint**:
- Method: `GET`
- Path: `/api/v1/conversations/{conversation_id}/turns`
- Auth: JWT (learn dashboard token)
- Path param: `conversation_id: UUID`
- Response: `200 OK` with `{"turns": [{"turn_id": UUID, "question": str, "answer": str, "created_at": ISO8601, "sources": [...] }], "conversation_id": UUID, "namespace": str}`
- Errors: `404` if conversation does not exist, `403` if JWT namespace does not match conversation namespace, `401` if JWT invalid/expired

**Authorization rule**: the JWT contains `namespace` (or `course_id` fallback per FEAT-003). The endpoint MUST verify `conversation.namespace_id == jwt.namespace` before returning turns. This is the only authorization gate — we do not also check `student_id` because a student may legitimately reload a conversation started in a previous session with the same course.

**Files**:
- `vektra-learn/src/vektra_learn/api.py` - add endpoint
- `vektra-learn/src/vektra_learn/models.py` - add `ConversationTurnsResponse` Pydantic model if not present
- `vektra-core/src/vektra_core/conversations.py` (or wherever `conversation_turns` table is queried) - add `get_turns_by_conversation_id(conversation_id, namespace) -> list[Turn]` helper that decrypts `question` and `answer` via `pgp_sym_decrypt` (see CLAUDE.md DB investigation notes)
- Tests: `vektra-learn/tests/test_conversations.py`

**Decryption note**: `conversation_turns.question` and `conversation_turns.answer` are encrypted with `pgp_sym_encrypt()` using `VEKTRA_CONVERSATION_KEY`. The helper must decrypt before serializing. See `.claude/CLAUDE.md` → "Conversation turns are encrypted".

**Test gates** (acceptance):
- [ ] Valid JWT + matching namespace returns decrypted turns ordered by `created_at` ASC
- [ ] Valid JWT + different namespace returns `403` (no data leak across courses)
- [ ] Expired JWT returns `401`
- [ ] Non-existent `conversation_id` returns `404`
- [ ] Response includes `sources` field (even if empty array) so widget does not need a conditional render
- [ ] Integration test uses a real Postgres (per `feedback_no_mock_database` memory)

### WI-2 - Backend: document name in source citations (FEAT-012)

Currently `SourceRef` returns `chunk_id` (UUID). Students see `[1] 4a2f3b... (0.82)`. They need to see the document name.

**Data source**: `document_chunks.document_id` → `documents.source_file` (or `documents.title` if we want a human-friendlier field). The Qdrant payload also carries `source_file` per audit earlier.

**Changes**:
- Extend `SourceRef` schema to include `document_name: str` (derived from `documents.source_file`, fallback to `documents.id[:8]` if null)
- Update the pipeline source assembly to fetch `source_file` from the document referenced by each chunk. Prefer a JOIN in the existing query over a second round-trip per chunk.
- Widget change: render `[1] document_name (score)` instead of `[1] chunk_id (score)`. Tooltip on hover can keep the chunk UUID for debugging.

**Files**:
- `vektra-core/src/vektra_core/api.py` or `pipeline.py` - `SourceRef` schema + assembly
- `vektra-learn/src/vektra_learn/query.py` - ensure the field passes through to the learn response
- `vektra-learn/widget/src/chat-ui.js` (or wherever sources render) - use `document_name`
- Tests: both backend (pipeline returns document_name) and widget (renders correctly)

**Edge cases**:
- Document soft-deleted (REQ-057): still render the name, append " (archived)" marker. Do NOT hide the citation — it must match what was retrieved.
- Document name with special chars (e.g., emoji, Italian accents): must survive JSON round-trip. Widget uses `textContent`, not `innerHTML`, for the citation label.

**Test gates**:
- [ ] `/api/v1/query` response includes `document_name` for each source
- [ ] `/api/v1/learn/query` response propagates the field
- [ ] Widget displays `[1] <name> (<score>)` — no UUID visible in normal UI
- [ ] Falls back gracefully if `source_file` is null

### WI-3 - Backend: PATCH /api/v1/namespaces/{id}/config (instructor config)

Foundation for category (B) config. Writes into the existing `namespaces.config` JSONB column. Read path already exists (`resolve_grounding_mode` at `vektra-shared/src/vektra_shared/namespace.py:19`).

**Endpoint**:
- Method: `PATCH`
- Path: `/api/v1/namespaces/{namespace_id}/config`
- Auth: admin API key (via `require_admin` dependency pattern already used elsewhere)
- Body: `{"grounding_mode": "strict" | "hybrid" | null}` (null = unset, falls back to env var). Explicitly allow partial updates — only keys present in the body are updated, others are preserved. Unknown keys rejected with `400`.
- Response: `200 OK` with the current full `config` object after merge
- Errors: `404` if namespace does not exist, `400` if value invalid (not in enum), `401`/`403` for auth

**Design choice — allowed keys**:
Keep the whitelist tight. For v0.5.0, only `grounding_mode` is a valid key. Adding more keys is a later patch-release change. The endpoint must reject unknown keys (not silently ignore) so that typos from the Moodle plugin surface immediately during development.

```python
ALLOWED_CONFIG_KEYS = {"grounding_mode"}  # extend cautiously; each key is a public contract
ALLOWED_GROUNDING_MODES = {"strict", "hybrid"}
```

**Files**:
- `vektra-admin/src/vektra_admin/api.py` (namespace routes already live here) - add PATCH
- `vektra-admin/src/vektra_admin/service.py` (or similar) - `update_namespace_config(namespace_id, partial_config) -> dict`
- Tests: `vektra-admin/tests/test_namespaces.py`

**Why admin API (not a new "teacher" scope)**: Moodle acts as the trusted upstream (see "Authentication model" above). Inventing a new scope here adds complexity without benefit — Moodle's admin key is already stored server-side and never reaches the browser.

**Test gates**:
- [ ] PATCH with `{"grounding_mode": "hybrid"}` updates the value in `namespaces.config`
- [ ] Subsequent query to that namespace uses hybrid mode (end-to-end: write → resolve → pipeline)
- [ ] PATCH with unknown key returns `400` with the rejected key name
- [ ] PATCH with invalid value (e.g., `"grounding_mode": "banana"`) returns `400`
- [ ] PATCH with `null` value for a key removes it from config (fallback to env var)
- [ ] PATCH is partial: existing keys not in the body are preserved
- [ ] Non-admin API key returns `403`

### WI-4 - Widget: white-label data-attrs (FEAT-016)

Client-side only. Read additional `data-*` attributes from the script tag, expose them as CSS custom properties + template values.

**New data-* attributes**:

| Attribute | Default | Applied to |
|---|---|---|
| `data-title` | "Course Assistant" / i18n | Chat panel header |
| `data-primary-color` | `#2563eb` | Button, links, accents (as `--vektra-primary` CSS var) |
| `data-icon` | speech bubble emoji | Floating button icon (URL or emoji) |
| `data-welcome-message` | (none) | First assistant message on open |
| `data-powered-by` | "true" | If "false", hide "Powered by Vektra" footer |

**Files**:
- `vektra-learn/widget/src/index.js` - parse new attributes in the constructor
- `vektra-learn/widget/src/styles.js` - replace hardcoded `#2563eb` with `var(--vektra-primary, #2563eb)`
- `vektra-learn/widget/src/chat-ui.js` - render title from config, inject welcome message on first open
- Tests: `vektra-learn/widget/tests/` - verify each attribute changes the rendered output, missing attributes fall back to defaults

**Fallback chain** (consistent with FEAT-016 backlog spec): `data-* attribute > hardcoded default`. Namespace metadata override is deferred to a later release (would require category-B endpoint extension, not worth it for visual fields).

**Test gates**:
- [ ] Each `data-*` attribute individually changes the widget output
- [ ] Missing attributes fall back to current v0.4.0 defaults (no breaking change for existing Moodle installs)
- [ ] Light/dark theme still works when `data-primary-color` is set
- [ ] `data-icon` accepts both emoji and URL; URL shows image, emoji shows text
- [ ] XSS: `data-title` with `<script>` injected is rendered as text (verify via `textContent`, not `innerHTML`)

### WI-5 - Widget: conversation persistence (FEAT-004)

Consumes WI-1 endpoint. Client-side logic for `sessionStorage` + history reload + "New chat" button.

**Storage schema**:
```js
sessionStorage[`vektra-conv-${courseId}`] = JSON.stringify({
  conversation_id: "...",
  created_at: "2026-04-18T..."  // for optional stale-after-N-hours cleanup
});
```

**Lifecycle**:
- On widget init: read sessionStorage for this `course_id`. If present and not stale (configurable TTL, default 24h), call `GET /conversations/{id}/turns` and render them before enabling input.
- On each successful query response: persist `conversation_id` (comes back in the response / SSE `done` event).
- On "New chat" button: clear sessionStorage entry, clear DOM, reset `_conversationId`.
- On `404` from turns endpoint (conversation deleted server-side): clear sessionStorage, start fresh, do not show error to user.
- On `403`: log and start fresh (likely namespace mismatch after course change).

**Token expiry and conversation continuity**: the existing `onTokenExpired` callback (FEAT-009) already handles transparent token refresh. Conversation ID is independent of JWT — it survives a refresh. This is the correct behavior.

**Idle timeout**: deferred. Do not auto-reset conversations after N minutes. Sessions in university contexts may include long reading breaks; forcing a new conversation frustrates follow-up questions.

**Files**:
- `vektra-learn/widget/src/index.js` - init-time history restore, persistence hook
- `vektra-learn/widget/src/chat-ui.js` - "New chat" button in header, message replay renderer
- `vektra-learn/widget/src/api-client.js` - `getConversationTurns(id)` method

**Test gates**:
- [ ] After sending a message and refreshing the page, the message history reappears
- [ ] Closing the tab and reopening loses the history (sessionStorage is tab-scoped — expected)
- [ ] "New chat" button resets conversation_id and DOM
- [ ] Different `course_id` on the same tab does NOT share history (keyed by course)
- [ ] `403` response triggers silent reset, not error UI
- [ ] History replay renders assistant messages with Markdown (FEAT-007 already in place) and sources (FEAT-012)

## Implementation order

Strict ordering to minimize rework:

1. **WI-3** first (PATCH /namespaces/config). Foundation. Also the easiest to test standalone.
2. **WI-2** (document name). Independent of WI-3. Small backend change, small widget change.
3. **WI-1** (GET turns endpoint). Prerequisite for WI-5.
4. **WI-4** (data-attrs). Independent of backend work. Can be done in parallel with WI-1/2/3 if convenient but not required.
5. **WI-5** (conversation persistence widget). Last, depends on WI-1. Also benefits from WI-2 being done (so replayed history shows document names, not UUIDs).

Each WI gets its own commit (Conventional Commits) on the `feat/v050-widget-and-prof-config` branch. Do not squash during development; squash-or-merge is a PR-time decision.

## Handoff to vektra-moodle

A sibling plan lives in `vektra-moodle` covering:

- Block edit form with two sections (visual / behavioral) that separate category (A) data-attrs from category (B) namespace config
- AJAX endpoint `/blocks/vektra/save_config.php` that receives the form, validates teacher auth, and fans out:
  - Visual fields → persisted as block instance config (Moodle native)
  - Behavioral fields → POST to `PATCH /api/v1/namespaces/{course_id}/config` with stored admin API key
- AJAX endpoint `/blocks/vektra/refresh_token.php` for FEAT-009 (if not yet implemented)
- Page render: read block config, emit `<script data-title="..." data-primary-color="..." ...>` on each course page

Vektra-stack plan (this file) is independent and can be completed and merged before the Moodle work starts. The widget's data-attr contract is stable (ADR-0025); Moodle consumes it.

## Test strategy

Three layers:

1. **Unit tests**: each WI has its own. Backend uses pytest + real Postgres (per the `feedback_no_mock_database` memory — do NOT mock the DB). Widget uses the existing Jest-like setup.
2. **Integration tests**: extend `tests/integration/` with a flow that:
   - Creates a namespace
   - PATCHes `grounding_mode=hybrid`
   - Runs a query against a namespace with no ingested content
   - Asserts the response is non-null (hybrid fallback engaged) — proves end-to-end persistence → read path works
3. **Manual smoke test before PR** (documented in PR description):
   - Kalypso Docker stack up, Moodle + Vektra reachable
   - Open a course page with the widget
   - Set a custom title + color via data-attrs (hardcoded in test Moodle block for now)
   - Send a message, refresh, verify history returns
   - Click "New chat", verify reset
   - Manually PATCH namespace to hybrid, ask an off-topic question, verify hybrid behavior kicks in

Lint/test gate per `feedback_lint_before_push`: `make lint && make test` must pass before any push.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| GET /conversations/turns decryption key unavailable in vektra-learn container | Keys loaded from same env var (`VEKTRA_CONVERSATION_KEY`) already used by core — single source of truth. Verify in startup check. |
| `namespaces.config` overwrite: concurrent PATCH from two professor edits races | Out of scope for v0.5.0 — last-write-wins is acceptable for instructor config. Add row-level locking if seen in practice. |
| Widget `sessionStorage` quota exhaustion on public computers | Quota is ~5MB per origin; storing only conversation_id + timestamp is a few hundred bytes. Not a realistic concern. |
| XSS via `data-title` or `data-welcome-message` | All data-attrs rendered via `textContent`, never `innerHTML`. Add explicit test case. |
| Decrypted conversation history exposed to wrong student via bad JWT scoping | Enforce `conversation.namespace_id == jwt.namespace` check + integration test that asserts 403 on mismatch. |

## Acceptance criteria (aggregate)

Plan is complete when:
- [ ] All 5 WIs merged to `feat/v050-widget-and-prof-config` (one commit per WI)
- [ ] `make lint && make test` pass
- [ ] Integration test for PATCH → hybrid query flow passes
- [ ] Manual smoke test checklist executed and documented
- [ ] Backlog statuses updated: FEAT-016, FEAT-012, FEAT-004 → completed; add new FEAT for "instructor namespace config API" if we want explicit tracking separate from FEAT-008
- [ ] vektra-moodle sibling plan referenced
- [ ] CHANGELOG.md updated with v0.5.0 section
- [ ] Version bumped to `0.5.0` in `pyproject.toml` and wherever else

## Integration and deployment notes

- No DB migration needed: `namespaces.config` column already exists.
- No new environment variables: all config flows through the existing admin API key.
- No new dependencies in `pyproject.toml`.
- Widget bundle size may grow ~1-2KB for data-attr parsing + history replay; acceptable (ADR-0025 sets no hard limit).
- Moodle plugin release is independent — v0.5.0 of vektra-stack works unchanged for a Moodle plugin still on v0.4.0 (all new data-attrs fall back gracefully, all new endpoints return 404 cleanly if the plugin does not call them).

## Next actions

1. Confirm this plan covers the intent (especially: WI-1/2/3 backend endpoints as specified, no hidden surprises).
2. Start WI-3 (PATCH endpoint) as the first implementation task.
3. Create corresponding plan in vektra-moodle repo before touching that codebase.
