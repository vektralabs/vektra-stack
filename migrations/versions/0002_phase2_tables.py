"""Phase 2 schema additions: 4 new tables, TOCTOU index, quota constraints.

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-01

New tables:
  - conversations: persistent multi-turn conversations (ARCH-031, REQ-049)
  - conversation_turns: encrypted question/answer pairs (pgcrypto)
  - query_traces: RAG observability traces with JSONB steps (ARCH-041, REQ-060)
  - feedback: response-level and citation-level feedback (REQ-055)

Schema modifications:
  - source_documents: unique partial index on (namespace_id, filename) for TOCTOU fix (TECH-004)
  - api_keys: expires_at column for token expiration (REQ-041)
  - namespaces: quota_bytes column + CHECK constraints on quota columns (ARCH-047)
"""

from __future__ import annotations

from alembic import op

# revision identifiers
revision: str = "0002"
down_revision: str = "0001"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. conversations (ARCH-031, REQ-049)
    # Owner: vektra-core
    # Persistent multi-turn conversations with soft delete for GDPR.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE conversations (
            id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            namespace_id    VARCHAR(64)     NOT NULL REFERENCES namespaces(id),
            key_id          UUID            NOT NULL,
            title           VARCHAR(512)    NULL,
            turn_count      INTEGER         NOT NULL DEFAULT 0,
            created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
            deleted_at      TIMESTAMPTZ     NULL,
            CONSTRAINT ck_conversations_turn_count CHECK (turn_count >= 0)
        )
    """)

    op.execute(
        "CREATE INDEX ix_conversations_namespace ON conversations (namespace_id)"
    )
    op.execute("CREATE INDEX ix_conversations_key_id ON conversations (key_id)")
    op.execute("""
        CREATE INDEX ix_conversations_active
            ON conversations (namespace_id, updated_at)
            WHERE deleted_at IS NULL
    """)

    # ------------------------------------------------------------------
    # 2. conversation_turns (ARCH-031, ADR-0011)
    # Owner: vektra-core
    # question and answer stored as BYTEA (pgp_sym_encrypt).
    # Encryption key passed via SET LOCAL vektra.conversation_key.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE conversation_turns (
            id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            conversation_id     UUID            NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            turn_number         INTEGER         NOT NULL,
            question            BYTEA           NOT NULL,
            answer              BYTEA           NULL,
            response_id         UUID            NULL,
            model               VARCHAR(255)    NULL,
            prompt_tokens       INTEGER         NULL,
            completion_tokens   INTEGER         NULL,
            created_at          TIMESTAMPTZ     NOT NULL DEFAULT now(),
            CONSTRAINT ck_conversation_turns_number CHECK (turn_number >= 1),
            CONSTRAINT uq_conversation_turns_order UNIQUE (conversation_id, turn_number)
        )
    """)

    op.execute("""
        CREATE INDEX ix_conversation_turns_conversation
            ON conversation_turns (conversation_id)
    """)
    op.execute("""
        CREATE INDEX ix_conversation_turns_response
            ON conversation_turns (response_id)
            WHERE response_id IS NOT NULL
    """)

    # ------------------------------------------------------------------
    # 3. query_traces (ARCH-041, REQ-060)
    # Owner: vektra-analytics
    # Steps and chunks stored as JSONB (write-heavy, read-occasionally).
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE query_traces (
            id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            response_id         UUID            NOT NULL UNIQUE,
            namespace_id        VARCHAR(64)     NOT NULL REFERENCES namespaces(id),
            steps               JSONB           NOT NULL DEFAULT '[]',
            total_duration_ms   INTEGER         NOT NULL,
            chunks_retrieved    JSONB           NOT NULL DEFAULT '[]',
            llm_model           VARCHAR(255)    NOT NULL,
            prompt_version      VARCHAR(8)      NOT NULL,
            created_at          TIMESTAMPTZ     NOT NULL DEFAULT now(),
            CONSTRAINT ck_query_traces_duration CHECK (total_duration_ms >= 0)
        )
    """)

    op.execute(
        "CREATE INDEX ix_query_traces_namespace ON query_traces (namespace_id)"
    )
    op.execute(
        "CREATE INDEX ix_query_traces_created ON query_traces (created_at)"
    )
    op.execute("""
        CREATE INDEX ix_query_traces_steps_gin
            ON query_traces
            USING gin (steps)
    """)

    # ------------------------------------------------------------------
    # 4. feedback (REQ-055)
    # Owner: vektra-core
    # response_id is NOT a FK (QueryResponse is ephemeral).
    # citation_id NULL = response-level, non-NULL = citation-level.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE feedback (
            id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            response_id     UUID            NOT NULL,
            citation_id     UUID            NULL,
            namespace_id    VARCHAR(64)     NOT NULL REFERENCES namespaces(id),
            key_id          UUID            NOT NULL,
            rating          SMALLINT        NOT NULL,
            comment         TEXT            NULL,
            created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
            CONSTRAINT ck_feedback_rating CHECK (rating >= 1 AND rating <= 5)
        )
    """)

    op.execute("CREATE INDEX ix_feedback_response ON feedback (response_id)")
    op.execute("CREATE INDEX ix_feedback_namespace ON feedback (namespace_id)")
    op.execute("CREATE INDEX ix_feedback_created ON feedback (created_at)")

    # ------------------------------------------------------------------
    # 5. TOCTOU fix: unique partial index on source_documents (TECH-004)
    # Prevents concurrent duplicate filename inserts within a namespace.
    # The existing non-unique ix_source_documents_ns_filename is retained.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE UNIQUE INDEX uq_source_documents_filename
            ON source_documents (namespace_id, filename)
            WHERE deleted_at IS NULL
    """)

    # ------------------------------------------------------------------
    # 6. api_keys.expires_at for token expiration (REQ-041)
    # NULL = never expires (backward compatible with Phase 1 keys).
    # ------------------------------------------------------------------
    op.execute("""
        ALTER TABLE api_keys
            ADD COLUMN expires_at TIMESTAMPTZ NULL
    """)

    op.execute("""
        CREATE INDEX ix_api_keys_expires
            ON api_keys (expires_at)
            WHERE expires_at IS NOT NULL AND revoked_at IS NULL
    """)

    # ------------------------------------------------------------------
    # 7. Namespace quota constraints + quota_bytes column (ARCH-047)
    # quota_chunks and quota_documents already exist as nullable (0001).
    # ------------------------------------------------------------------
    op.execute("""
        ALTER TABLE namespaces
            ADD COLUMN quota_bytes BIGINT NULL
    """)

    op.execute("""
        ALTER TABLE namespaces
            ADD CONSTRAINT ck_namespaces_quota_chunks
            CHECK (quota_chunks IS NULL OR quota_chunks > 0)
    """)

    op.execute("""
        ALTER TABLE namespaces
            ADD CONSTRAINT ck_namespaces_quota_documents
            CHECK (quota_documents IS NULL OR quota_documents > 0)
    """)

    op.execute("""
        ALTER TABLE namespaces
            ADD CONSTRAINT ck_namespaces_quota_bytes
            CHECK (quota_bytes IS NULL OR quota_bytes > 0)
    """)


def downgrade() -> None:
    # Namespace quota: drop constraints then column
    op.execute(
        "ALTER TABLE namespaces DROP CONSTRAINT IF EXISTS ck_namespaces_quota_bytes"
    )
    op.execute(
        "ALTER TABLE namespaces DROP CONSTRAINT IF EXISTS ck_namespaces_quota_documents"
    )
    op.execute(
        "ALTER TABLE namespaces DROP CONSTRAINT IF EXISTS ck_namespaces_quota_chunks"
    )
    op.execute("ALTER TABLE namespaces DROP COLUMN IF EXISTS quota_bytes")

    # api_keys.expires_at
    op.execute("DROP INDEX IF EXISTS ix_api_keys_expires")
    op.execute("ALTER TABLE api_keys DROP COLUMN IF EXISTS expires_at")

    # TOCTOU unique index on source_documents
    op.execute("DROP INDEX IF EXISTS uq_source_documents_filename")

    # Drop Phase 2 tables in reverse dependency order
    op.execute("DROP TABLE IF EXISTS feedback CASCADE")
    op.execute("DROP TABLE IF EXISTS query_traces CASCADE")
    op.execute("DROP TABLE IF EXISTS conversation_turns CASCADE")
    op.execute("DROP TABLE IF EXISTS conversations CASCADE")
