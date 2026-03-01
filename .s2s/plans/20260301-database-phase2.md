# Implementation Plan: Phase 2 database schema and Alembic migrations

**ID**: 20260301-database-phase2
**Status**: pending
**Branch**: N/A
**Created**: 2026-03-01T14:30:09Z
**Updated**: 2026-03-01T14:30:09Z

## Traceability

**Source**: database-phase2
**Source Type**: architecture

## Provides / Requires

**Provides**:
- conversations table with pgcrypto column-level encryption (consumers: core-conversations)
- conversation_turns table with pgcrypto column-level encryption (consumers: core-conversations)
- query_traces table with JSONB step storage (consumers: component-analytics, core-pipeline-v2)
- feedback table with response_id and citation_id references (consumers: core-conversations)
- Unique partial index on source_documents (namespace_id, filename) for TOCTOU fix (consumers: ingest-phase2)
- Namespace quota CHECK constraints and indexes (consumers: admin-enforcement)
- `api_keys.expires_at` column for token expiration (consumers: admin-enforcement)
- Migration chain: 0001 -> 0002 (consumers: all Phase 2 plans)

**Requires**: none

## References

### Requirements
- REQ-033: Duplicate document handling (filename uniqueness) @.s2s/requirements.md
- REQ-041: Unified authentication error codes (token expiration) @.s2s/requirements.md
- REQ-049: Multi-turn conversation context (persistent storage Phase 2) @.s2s/requirements.md
- REQ-055: Response and citation traceability (feedback endpoints Phase 2) @.s2s/requirements.md
- REQ-060: QueryTrace for RAG observability (dedicated storage Phase 2) @.s2s/requirements.md

### Architecture
- ARCH-031: Conversation storage encrypted (pgcrypto column-level encryption) @.s2s/architecture.md
- ARCH-040: Forward-compatible data model (namespace quota columns already nullable) @.s2s/architecture.md
- ARCH-041: Audit/analytics separation (QueryTrace dedicated storage Phase 2) @.s2s/architecture.md
- ARCH-047: Namespace as first-class entity (quota enforcement Phase 2) @.s2s/architecture.md
- ARCH-058: Database schema specification (Phase 2 additions table) @.s2s/architecture.md

### Decisions
- ADR-0011: Conversation encryption @.s2s/decisions/ADR-0011-conversation-encryption.md
- ADR-0022: SQLAlchemy 2.0 async with asyncpg @.s2s/decisions/ADR-0022-orm-sqlalchemy-async.md

### Dependencies
none

## Overview

This plan creates the Phase 2 database tables and indexes via a new Alembic migration (0002). Phase 1 shipped 7 tables in migration 0001. Phase 2 adds 4 new tables (conversations, conversation_turns, query_traces, feedback) and modifies the existing source_documents table with a TOCTOU-mitigating unique partial index.

The conversations and conversation_turns tables use pgcrypto for column-level encryption of user-generated content (question and answer text). The pgcrypto extension was already enabled in migration 0001. Encryption uses `pgp_sym_encrypt()` / `pgp_sym_decrypt()` with a symmetric key provided at query time via a PostgreSQL session variable (`SET LOCAL vektra.conversation_key = ...`). The encryption key is separate from the database credentials and loaded from `VEKTRA_CONVERSATION_KEY` env var.

The query_traces table stores QueryTrace data (StepTrace list, ChunkRef list) as JSONB columns, since the nested structure maps naturally to JSON and avoids the overhead of normalized child tables for write-heavy, read-occasionally data.

The feedback table stores both response-level and citation-level feedback, using response_id as a non-FK reference to QueryResponse (which is ephemeral, not stored in a table) and citation_id as a reference to SourceRef.citation_id.

## Design Notes

**pgcrypto encryption pattern**: The `question` and `answer` columns in conversation_turns are stored as `BYTEA` (encrypted). Reads and writes use PostgreSQL functions:

```sql
-- Write (application layer wraps this in ORM):
INSERT INTO conversation_turns (conversation_id, turn_number, question, answer)
VALUES (:conv_id, :turn, pgp_sym_encrypt(:question, current_setting('vektra.conversation_key')),
        pgp_sym_encrypt(:answer, current_setting('vektra.conversation_key')));

-- Read:
SELECT pgp_sym_decrypt(question, current_setting('vektra.conversation_key')) AS question,
       pgp_sym_decrypt(answer, current_setting('vektra.conversation_key')) AS answer
FROM conversation_turns WHERE conversation_id = :conv_id;
```

The application sets the session variable before each transaction:
```sql
SET LOCAL vektra.conversation_key = :key;
```
`SET LOCAL` scopes the variable to the current transaction only, so the key is never visible to other connections or after the transaction ends.

**TOCTOU fix (TECH-004)**: Add `CREATE UNIQUE INDEX uq_source_documents_filename ON source_documents (namespace_id, filename) WHERE deleted_at IS NULL`. This complements the existing `uq_source_documents_hash` index (which covers content dedup) with filename uniqueness. The `run_ingest()` code must then catch `IntegrityError` from this index and raise `IngestConflictError` (that change belongs to ingest-phase2 plan, not this one).

**Namespace quota columns**: The `quota_chunks` and `quota_documents` columns already exist as nullable integers in the namespaces table (ARCH-040 forward-compatible). This plan adds CHECK constraints (`quota_chunks IS NULL OR quota_chunks > 0`, same for `quota_documents`) and a `quota_bytes` column to match the extended Namespace type from shared-protocols-phase2. No enforcement logic here, that belongs in admin-enforcement.

**Migration file naming**: Following the established pattern, the new migration is `0002_phase2_tables.py` with `revision = "0002"` and `down_revision = "0001"`.

**No ORM models in migration**: Consistent with migration 0001, this migration uses raw SQL via `op.execute()`. ORM models for the new tables will be defined in their owning component modules (core-conversations for conversations/conversation_turns/feedback, component-analytics for query_traces).

## Tasks

- [ ] Create migration file `migrations/versions/0002_phase2_tables.py` with `revision = "0002"`, `down_revision = "0001"`. Add module docstring explaining: Phase 2 schema additions (4 new tables, 1 index, quota constraints). Follow the same structure as 0001 (op.execute with raw SQL, numbered sections with ARCH references).

- [ ] Add `conversations` table via `op.execute()`:
  ```sql
  CREATE TABLE conversations (
      id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
      namespace_id    VARCHAR(64)     NOT NULL REFERENCES namespaces(id),
      key_id          UUID            NOT NULL,           -- API key that created it (not FK, key may be revoked)
      title           VARCHAR(512)    NULL,               -- Optional conversation title
      turn_count      INTEGER         NOT NULL DEFAULT 0,
      created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
      updated_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
      deleted_at      TIMESTAMPTZ     NULL,               -- Soft delete for GDPR
      CONSTRAINT ck_conversations_turn_count CHECK (turn_count >= 0)
  );
  ```
  Add indexes: `ix_conversations_namespace` on `(namespace_id)`, `ix_conversations_key_id` on `(key_id)`, partial index `ix_conversations_active` on `(namespace_id, updated_at) WHERE deleted_at IS NULL`.

- [ ] Add `conversation_turns` table via `op.execute()`:
  ```sql
  CREATE TABLE conversation_turns (
      id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
      conversation_id UUID            NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
      turn_number     INTEGER         NOT NULL,
      question        BYTEA           NOT NULL,           -- pgp_sym_encrypt(plaintext, key)
      answer          BYTEA           NULL,               -- pgp_sym_encrypt(plaintext, key), NULL if no answer
      response_id     UUID            NULL,               -- Correlates to QueryResponse.response_id
      model           VARCHAR(255)    NULL,               -- LLM model used for this turn
      prompt_tokens   INTEGER         NULL,
      completion_tokens INTEGER       NULL,
      created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
      CONSTRAINT ck_conversation_turns_number CHECK (turn_number >= 1),
      CONSTRAINT uq_conversation_turns_order UNIQUE (conversation_id, turn_number)
  );
  ```
  Add indexes: `ix_conversation_turns_conversation` on `(conversation_id)`, `ix_conversation_turns_response` on `(response_id) WHERE response_id IS NOT NULL`.

- [ ] Add `query_traces` table via `op.execute()`:
  ```sql
  CREATE TABLE query_traces (
      id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
      response_id     UUID            NOT NULL UNIQUE,    -- 1:1 with QueryResponse
      namespace_id    VARCHAR(64)     NOT NULL REFERENCES namespaces(id),
      steps           JSONB           NOT NULL DEFAULT '[]',  -- list of StepTrace dicts
      total_duration_ms INTEGER       NOT NULL,
      chunks_retrieved JSONB          NOT NULL DEFAULT '[]',  -- list of ChunkRef dicts
      llm_model       VARCHAR(255)    NOT NULL,
      prompt_version  VARCHAR(8)      NOT NULL,           -- SHA-256[:8] of template sources
      created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
      CONSTRAINT ck_query_traces_duration CHECK (total_duration_ms >= 0)
  );
  ```
  Add indexes: `ix_query_traces_namespace` on `(namespace_id)`, `ix_query_traces_created` on `(created_at)`, GIN index `ix_query_traces_steps_gin` on `(steps)` for step-level queries.

- [ ] Add `feedback` table via `op.execute()`:
  ```sql
  CREATE TABLE feedback (
      id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
      response_id     UUID            NOT NULL,           -- Correlates to QueryResponse.response_id
      citation_id     UUID            NULL,               -- NULL = response-level, non-NULL = citation-level
      namespace_id    VARCHAR(64)     NOT NULL REFERENCES namespaces(id),
      key_id          UUID            NOT NULL,           -- API key that submitted feedback (not FK)
      rating          SMALLINT        NOT NULL,           -- 1 (negative) to 5 (positive)
      comment         TEXT            NULL,               -- Optional text feedback
      created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
      CONSTRAINT ck_feedback_rating CHECK (rating >= 1 AND rating <= 5)
  );
  ```
  Add indexes: `ix_feedback_response` on `(response_id)`, `ix_feedback_namespace` on `(namespace_id)`, `ix_feedback_created` on `(created_at)`.

- [ ] Add TOCTOU-mitigating unique partial index on `source_documents`:
  ```sql
  CREATE UNIQUE INDEX uq_source_documents_filename
      ON source_documents (namespace_id, filename)
      WHERE deleted_at IS NULL;
  ```
  This resolves TECH-004. The existing `ix_source_documents_ns_filename` non-unique index (created in 0001) is retained since PostgreSQL can use either index depending on the query plan. The unique index adds write-time constraint enforcement; the non-unique index may still be preferred for range scans.

- [ ] Add `expires_at` column to `api_keys` table for token expiration (REQ-041):
  ```sql
  ALTER TABLE api_keys
      ADD COLUMN expires_at TIMESTAMPTZ NULL;

  CREATE INDEX ix_api_keys_expires
      ON api_keys (expires_at)
      WHERE expires_at IS NOT NULL AND revoked_at IS NULL;
  ```
  NULL means the key never expires (backward compatible with all Phase 1 keys). The partial index supports efficient lookup of keys approaching expiration. Enforcement logic belongs to admin-enforcement plan.

- [ ] Add namespace quota constraints and `quota_bytes` column:
  ```sql
  ALTER TABLE namespaces
      ADD COLUMN quota_bytes BIGINT NULL;

  ALTER TABLE namespaces
      ADD CONSTRAINT ck_namespaces_quota_chunks
      CHECK (quota_chunks IS NULL OR quota_chunks > 0);

  ALTER TABLE namespaces
      ADD CONSTRAINT ck_namespaces_quota_documents
      CHECK (quota_documents IS NULL OR quota_documents > 0);

  ALTER TABLE namespaces
      ADD CONSTRAINT ck_namespaces_quota_bytes
      CHECK (quota_bytes IS NULL OR quota_bytes > 0);
  ```
  The `quota_chunks` and `quota_documents` columns already exist (created in 0001 as nullable). This adds CHECK constraints to prevent invalid values (zero or negative) and adds the `quota_bytes` column for storage-based quotas.

- [ ] Implement the `downgrade()` function in 0002: drop tables in reverse dependency order (feedback, query_traces, conversation_turns, conversations), drop the unique index `uq_source_documents_filename` on source_documents, drop `ix_api_keys_expires` index and `expires_at` column from api_keys, drop the quota constraints (`ck_namespaces_quota_chunks`, `ck_namespaces_quota_documents`, `ck_namespaces_quota_bytes`) and `quota_bytes` column from namespaces.

- [ ] Write a migration test in `vektra-shared/tests/test_migration.py` (extend the existing test file): apply migration 0002 to a test PostgreSQL instance (after 0001), verify:
  - All 4 new tables exist with correct columns and types
  - `conversation_turns.question` and `conversation_turns.answer` are BYTEA type
  - `uq_source_documents_filename` unique index exists
  - `ck_namespaces_quota_chunks` CHECK constraint exists
  - `query_traces.response_id` has UNIQUE constraint
  - Downgrade removes all Phase 2 additions without affecting Phase 1 tables
  - Use testcontainers (auto-skip when Docker unavailable)

- [ ] Verify pgcrypto round-trip in test: INSERT a row into `conversation_turns` using `pgp_sym_encrypt()`, SELECT it back with `pgp_sym_decrypt()`, verify the plaintext matches. This validates that the BYTEA columns work correctly with pgcrypto functions and that the `SET LOCAL vektra.conversation_key` pattern works.

## Acceptance Criteria

- [ ] `alembic upgrade head` applies both 0001 and 0002 on a fresh PostgreSQL instance without error
- [ ] `conversations` table created with soft delete support and namespace FK
- [ ] `conversation_turns` table created with BYTEA columns for encrypted content, unique constraint on (conversation_id, turn_number)
- [ ] `query_traces` table created with JSONB columns for steps and chunks_retrieved, UNIQUE on response_id
- [ ] `feedback` table created with rating CHECK constraint (1-5), response_id and optional citation_id
- [ ] `uq_source_documents_filename` unique partial index prevents concurrent duplicate filename inserts (TECH-004)
- [ ] `api_keys.expires_at` column exists as nullable TIMESTAMPTZ with partial index
- [ ] Namespace quota CHECK constraints reject zero and negative values
- [ ] `quota_bytes` column added to namespaces table
- [ ] pgcrypto encryption round-trip verified in integration test
- [ ] `alembic downgrade 0001` removes all Phase 2 additions cleanly
- [ ] All Phase 1 tables and data remain intact after 0002 upgrade

## Testing Approach

Integration test using testcontainers (PostgreSQL with pgvector extension). The test applies the full migration chain (0001 + 0002), then verifies table structure via `information_schema` queries. A separate test validates pgcrypto encryption round-trip. Downgrade test applies 0002 then downgrades to 0001, verifying Phase 2 tables are removed while Phase 1 tables remain. All integration tests are auto-skipped when Docker is unavailable (consistent with Phase 1 test pattern).

## Integration Notes

The ORM models for the new tables will be defined in their respective owning components:

| Table | Owner component | Plan |
|-------|----------------|------|
| conversations | vektra-core | core-conversations |
| conversation_turns | vektra-core | core-conversations |
| query_traces | vektra-analytics | component-analytics |
| feedback | vektra-core | core-conversations |

The pgcrypto encryption key (`VEKTRA_CONVERSATION_KEY`) will be added to `VektraSettings` in the config plan (shared-protocols-phase2 or core-conversations, depending on implementation order). The `SET LOCAL` session variable pattern will be encapsulated in a SQLAlchemy session event hook in the core-conversations plan.

The `uq_source_documents_filename` index enables the TOCTOU fix, but the application-level `IntegrityError` handling belongs to ingest-phase2. This plan only creates the index.

## Notes

**Migration ordering**: This plan creates migration 0002. Later plans that need schema changes (index-hybrid: sparse_vector column, component-learn: enrollment tables) should create migrations 0003 and 0004 respectively, with `down_revision` chaining from 0002. This plan is the foundation of the migration chain.

<!-- Progress notes during implementation -->
