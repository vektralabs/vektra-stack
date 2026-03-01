# Implementation Plan: vektra-core - Persistent conversations, feedback, disconnect handling

**ID**: 20260301-core-conversations
**Status**: pending
**Branch**: N/A
**Created**: 2026-03-01T14:30:09Z
**Updated**: 2026-03-01T14:30:09Z

## Traceability

**Source**: core-conversations
**Source Type**: architecture

## Provides / Requires

**Provides**:
- Persistent conversation CRUD with pgcrypto encryption (consumers: core-pipeline-v2)
- Feedback API for response and citation ratings (consumers: core-pipeline-v2, component-analytics)

**Requires**:
- 20260301-database-phase2.md: conversations, conversation_turns, and feedback tables must exist with pgcrypto encryption columns

## References

### Requirements
- REQ-049: Multi-turn conversation context @.s2s/requirements.md
- REQ-051: Operator privacy - no conversation content access @.s2s/requirements.md
- REQ-055: Response and citation traceability @.s2s/requirements.md
- REQ-042: Streaming query responses @.s2s/requirements.md

### Architecture
- ARCH-031: Conversation storage encrypted via pgcrypto @.s2s/architecture.md
- ARCH-040: Forward-compatible data model @.s2s/architecture.md
- ARCH-054: Composable Jinja2 prompt templates (conversation template) @.s2s/architecture.md
- ARCH-055: Token budget allocation @.s2s/architecture.md
- ARCH-059: API contract specification (Phase 2 additions) @.s2s/architecture.md

### Decisions
- ADR-0011: Conversation storage with encrypted content column @.s2s/decisions/ADR-0011-conversation-encryption.md
- ADR-0005: Module boundary enforcement @.s2s/decisions/ADR-0005-module-boundary-enforcement.md
- ADR-0022: SQLAlchemy 2.0 async with asyncpg for ORM @.s2s/decisions/ADR-0022-orm-sqlalchemy-async.md

### Dependencies
- 20260301-database-phase2.md

## Overview

This plan migrates the in-memory `ConversationStore` in vektra-core to persistent PostgreSQL storage with pgcrypto column-level encryption. Phase 1 uses a dict-keyed-by-UUID store that loses all history on restart (documented as TD-01). Phase 2 replaces this with `ConversationOrm` and `ConversationTurnOrm` models backed by the `conversations` and `conversation_turns` tables created by the database-phase2 plan.

Encryption uses `pgp_sym_encrypt`/`pgp_sym_decrypt` with the existing `VEKTRA_CONVERSATION_KEY` environment variable (already defined in VektraSettings as `conversation_key: str | None = None`), separate from admin API keys. The PostgreSQL session variable is `vektra.conversation_key` (set via `SET LOCAL`). Content is encrypted at write time and decrypted only in the query processing path. The GET endpoint returns metadata only (no content), enforcing REQ-051.

This plan also adds feedback endpoints (POST /api/v1/feedback/{response_id} and POST /api/v1/feedback/citation/{citation_id}) per REQ-055, and implements client disconnect cancellation for streaming queries (DEBT-005).

## Design Notes

**ORM models** (internal to vektra-core, not in vektra_shared per ADR-0005):

> **IMPORTANT**: The sketches below are illustrative. The actual ORM models MUST match the DDL in database-phase2 (migration 0002). Key differences: `conversation_turns` has separate `question` (BYTEA) and `answer` (BYTEA) columns (not a single `content_encrypted`), uses `turn_number` (not `sequence`), has no `role` column, and includes `response_id`, `model`, `prompt_tokens`, `completion_tokens` fields. `conversations` has `key_id`, `title`, `turn_count`, `deleted_at` columns. Always defer to the DDL as the source of truth.

```python
# vektra_core/models.py (new file)
class ConversationOrm(Base):
    __tablename__ = "conversations"
    id: Mapped[UUID]                    # PK, gen_random_uuid()
    namespace_id: Mapped[str]           # FK namespaces(id)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    metadata_json: Mapped[dict]         # JSONB, nullable

class ConversationTurnOrm(Base):
    __tablename__ = "conversation_turns"
    id: Mapped[UUID]                    # PK, gen_random_uuid()
    conversation_id: Mapped[UUID]       # FK conversations(id) ON DELETE CASCADE
    sequence: Mapped[int]
    role: Mapped[str]                   # 'user' | 'assistant'
    content_encrypted: Mapped[bytes]    # BYTEA, pgp_sym_encrypt(content, key)
    created_at: Mapped[datetime]

class FeedbackOrm(Base):
    __tablename__ = "feedback"
    id: Mapped[UUID]                    # PK, gen_random_uuid()
    response_id: Mapped[UUID]           # NOT a FK (response may not be persisted)
    citation_id: Mapped[UUID | None]    # NULL for response-level feedback
    rating: Mapped[int]                 # 1-5, CHECK constraint
    comment: Mapped[str | None]         # optional text
    namespace_id: Mapped[str]
    key_id: Mapped[UUID]                # who submitted it (NOT a FK)
    created_at: Mapped[datetime]
```

**Encryption flow**:
- Write: `func.pgp_sym_encrypt(text, key)` as a SQL expression in the INSERT
- Read: `func.pgp_sym_decrypt(col, key)` as a SQL expression in the SELECT
- Key source: `VEKTRA_CONVERSATION_KEY` env var, validated at startup (ARCH-057)
- If key is not set, conversation persistence is disabled (fallback to in-memory, log warning)

**ConversationStore replacement**:
- New class: `PersistentConversationStore` with the same interface as `ConversationStore`
- `get_history()` returns decrypted turns ordered by sequence
- `add_turn()` encrypts and inserts; prunes oldest turns if count exceeds max_turns
- `clear()` deletes the conversation (CASCADE deletes turns)
- Pipeline receives `PersistentConversationStore` instead of `ConversationStore`
- The `ConversationStore` (in-memory) class is kept as fallback when VEKTRA_CONVERSATION_KEY is not set

**Feedback endpoints** (per ARCH-059 Phase 2 additions):
- POST /api/v1/feedback/{response_id}: body `{ rating: int, comment: str | None }`
- POST /api/v1/feedback/citation/{citation_id}: body `{ rating: int, comment: str | None }`
- Auth: `query` or `admin` scope (same as /query endpoint)
- Rating: integer 1-5 (CHECK constraint in DB, Pydantic validation in endpoint)

**Disconnect cancellation** (DEBT-005):
- In `_sse_generator()` (api.py), pass `request` to the generator
- Poll `request.is_disconnected()` between token yields
- On disconnect: call `aclose()` on the LLM stream iterator
- Log disconnect event via structlog

**Conversation CRUD endpoints** (per ARCH-059 Phase 2 additions):
- GET /api/v1/conversations/{id}: returns conversation metadata (id, namespace_id, created_at, turn_count). No content (REQ-051).
- DELETE /api/v1/conversations/{id}: deletes conversation and all turns (CASCADE)
- Auth: `query` or `admin` scope

## Tasks

- [ ] Create `vektra_core/models.py` with `ConversationOrm`, `ConversationTurnOrm`, and `FeedbackOrm` ORM models (follow vektra-admin `models.py` patterns: DeclarativeBase, Mapped[], server_default)
- [ ] Verify `VEKTRA_CONVERSATION_KEY` exists in VektraSettings (`conversation_key: str | None = None` already defined in `vektra_shared/config.py`). Add startup validation in the conversation store initialization: if set, verify non-empty; if not set, log warning about in-memory fallback. No config schema changes needed.
- [ ] Implement `PersistentConversationStore` in `vektra_core/conversation.py` (keep existing `ConversationStore` class as `InMemoryConversationStore`). Methods: `get_history()` with pgp_sym_decrypt, `add_turn()` with pgp_sym_encrypt and max_turns pruning, `clear()` with CASCADE delete. Constructor takes `AsyncSession` factory and encryption key
- [ ] Add `create_conversation()` method to `PersistentConversationStore`: creates a new conversation row, returns UUID. Called from pipeline when `conversation_id` is None and a new multi-turn session starts
- [ ] Implement GET /api/v1/conversations/{id} endpoint in `vektra_core/api.py`: returns metadata only (id, namespace_id, created_at, updated_at, turn_count). Auth: query or admin scope. 404 if not found
- [ ] Implement DELETE /api/v1/conversations/{id} endpoint in `vektra_core/api.py`: soft deletes or hard deletes the conversation. Auth: query or admin scope. 404 if not found. 204 on success
- [ ] Implement POST /api/v1/feedback/{response_id} endpoint: accepts `{ rating: int, comment: str | None }`, validates rating 1-5, writes FeedbackOrm row with citation_id=None. Auth: query or admin scope. Returns 201
- [ ] Implement POST /api/v1/feedback/citation/{citation_id} endpoint: same as above but sets citation_id. Returns 201
- [ ] Update `SimpleQueryPipeline.__init__()` to accept either `ConversationStore` (in-memory) or `PersistentConversationStore` (persistent). Use a common base class or Protocol to type the parameter
- [ ] Implement client disconnect cancellation in `api.py`: pass `request` to `_sse_generator()`, poll `request.is_disconnected()` between token yields, call `aclose()` on the LLM stream iterator on disconnect, log the event (DEBT-005)
- [ ] Write unit tests for `PersistentConversationStore`: encryption round-trip, max_turns pruning, get_history ordering, clear cascade
- [ ] Write unit tests for feedback endpoints: valid rating, invalid rating (0, 6), response-level vs citation-level, auth scope enforcement

## Acceptance Criteria

- [ ] Conversations persist across application restarts when VEKTRA_CONVERSATION_KEY is set
- [ ] Conversation content is encrypted at rest (pgcrypto column-level encryption, verifiable by querying raw bytes)
- [ ] GET /conversations/{id} returns metadata only, never content (REQ-051)
- [ ] DELETE /conversations/{id} removes conversation and all turns
- [ ] POST /feedback/{response_id} stores rating (1-5) and optional comment
- [ ] POST /feedback/citation/{citation_id} stores citation-level feedback
- [ ] When VEKTRA_CONVERSATION_KEY is not set, application falls back to in-memory conversation store with a logged warning
- [ ] Client disconnect during SSE streaming explicitly closes the LLM iterator (DEBT-005)
- [ ] No orphan async tasks after simulated client disconnect
- [ ] Existing pipeline tests continue to pass (backward compatible)

## Testing Approach

Unit tests use SQLite or a test PostgreSQL instance with pgcrypto extension. The encryption round-trip test verifies that `pgp_sym_encrypt` and `pgp_sym_decrypt` produce the original content. A mock `AsyncSession` factory validates the store's SQL generation without a live database for fast CI. Integration tests (if PostgreSQL is available) verify the full path: pipeline.execute() with a persistent conversation store, followed by GET /conversations/{id} returning correct turn_count.

For disconnect cancellation, the test creates a mock `Request` object with `is_disconnected()` returning True after N yields, and verifies that `aclose()` was called on the mock LLM stream.

Feedback endpoint tests use the standard FastAPI `TestClient` pattern with a test database session.

## Integration Notes

The `PersistentConversationStore` is constructed during the app lifespan (infra-app-entrypoint), using the same session factory as other ORM operations. It is registered in `ProviderRegistry` or passed directly to `SimpleQueryPipeline` at construction time.

The feedback table references `response_id` and `citation_id` from `QueryResponse.response_id` and `SourceRef.citation_id` (both UUIDs generated per-query in pipeline.py). These are NOT foreign keys because query responses are not persisted in Phase 1. The feedback table simply records the UUID for correlation. Phase 2 core-pipeline-v2 will persist QueryTrace (which includes response_id), enabling joins.

The infra-phase2 plan will update the app lifespan to construct `PersistentConversationStore` when `VEKTRA_CONVERSATION_KEY` is set, or fall back to `InMemoryConversationStore` otherwise.

## Notes

<!-- Progress notes during implementation -->
