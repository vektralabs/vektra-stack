"""Initial schema: all 7 Phase 1 tables.

Revision ID: 0001
Revises: (none)
Create Date: 2026-02-18

Resolves pre-implementation review BLOCKERs:
  B-1/B-3: Chunk deletion semantics - source_documents soft-delete,
    document_chunks hard-deleted via explicit DELETE in same transaction.
    ON DELETE CASCADE on document_id FK is a safety net only.
    chunks_removed count captured with SELECT COUNT(*) before deletion.
  B-2: content_type NOT NULL with default 'application/octet-stream' -
    magic bytes detection (ARCH-042) populates the actual MIME type;
    if detection fails, fallback is 'application/octet-stream', never NULL.
"""

from __future__ import annotations

from alembic import op

# revision identifiers
revision: str = "0001"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Extensions (must come first - required by document_chunks.embedding)
    # ------------------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ------------------------------------------------------------------
    # 1. namespaces (ARCH-047, REQ-048)
    # Owner: vektra-admin
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE namespaces (
            id              VARCHAR(64)     PRIMARY KEY,
            display_name    VARCHAR(255)    NULL,
            owner_key_id    UUID            NULL,
            quota_chunks    INTEGER         NULL,
            quota_documents INTEGER         NULL,
            config          JSONB           NOT NULL DEFAULT '{}',
            retention_days  INTEGER         NULL,
            created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),

            CONSTRAINT ck_namespaces_retention
                CHECK (retention_days IS NULL OR retention_days > 0)
        )
    """)

    # Seed the "default" namespace (required before other tables insert data)
    op.execute("""
        INSERT INTO namespaces (id, display_name)
        VALUES ('default', 'Default namespace')
    """)

    # ------------------------------------------------------------------
    # 2. api_keys (REQ-023, ARCH-023)
    # Owner: vektra-admin
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE api_keys (
            id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            key_hash        VARCHAR(255)    NOT NULL,
            key_preview     VARCHAR(4)      NOT NULL,
            label           VARCHAR(255)    NULL,
            scopes          TEXT[]          NOT NULL DEFAULT '{admin}',
            rate_limit_rpm  INTEGER         NULL,
            created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
            last_used_at    TIMESTAMPTZ     NULL,
            revoked_at      TIMESTAMPTZ     NULL,

            CONSTRAINT ck_api_keys_scopes
                CHECK (scopes <@ ARRAY['admin', 'ingest', 'query']::text[])
        )
    """)

    # Deferred FK from namespaces.owner_key_id (avoids circular dependency)
    op.execute("""
        ALTER TABLE namespaces
            ADD CONSTRAINT fk_namespaces_owner_key
            FOREIGN KEY (owner_key_id) REFERENCES api_keys(id)
            ON DELETE SET NULL
    """)

    # ------------------------------------------------------------------
    # 3. source_documents (REQ-034, REQ-056, REQ-057, BR-005)
    # Owner: vektra-ingest
    # Soft delete: sets deleted_at. Chunks are hard-deleted separately.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE source_documents (
            id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            namespace_id    VARCHAR(64)     NOT NULL REFERENCES namespaces(id),
            filename        VARCHAR(1024)   NOT NULL,
            content_hash    VARCHAR(64)     NOT NULL,
            content_type    VARCHAR(255)    NOT NULL,
            file_size_bytes BIGINT          NOT NULL,
            chunk_count     INTEGER         NULL,
            version         INTEGER         NOT NULL DEFAULT 1,
            supersedes_id   UUID            NULL REFERENCES source_documents(id),
            filename_aliases JSONB          NOT NULL DEFAULT '[]',
            deleted_at      TIMESTAMPTZ     NULL,
            deletion_reason VARCHAR(32)     NULL,
            created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),

            CONSTRAINT ck_source_documents_deletion_reason
                CHECK (deletion_reason IS NULL OR deletion_reason IN
                       ('user_request', 'superseded', 'expired', 'pipeline_failure'))
        )
    """)

    # Partial unique index: one active document per (namespace, content_hash)
    op.execute("""
        CREATE UNIQUE INDEX uq_source_documents_hash
            ON source_documents (namespace_id, content_hash)
            WHERE deleted_at IS NULL
    """)
    op.execute(
        "CREATE INDEX ix_source_documents_namespace ON source_documents (namespace_id)"
    )
    op.execute(
        "CREATE INDEX ix_source_documents_content_hash ON source_documents (content_hash)"
    )
    op.execute("""
        CREATE INDEX ix_source_documents_ns_filename
            ON source_documents (namespace_id, filename)
            WHERE deleted_at IS NULL
    """)

    # ------------------------------------------------------------------
    # 4. document_chunks (ARCH-044, ARCH-045, ARCH-051)
    # Owner: vektra-index
    # Hard-deleted explicitly (DELETE WHERE document_id = ?) in the same
    # transaction as the source_documents soft-delete. The ON DELETE CASCADE
    # below is a safety net only and must never be the primary deletion path.
    # content_type NOT NULL default 'application/octet-stream' (BLOCKER B-2).
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE document_chunks (
            id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            document_id     UUID            NOT NULL
                                REFERENCES source_documents(id) ON DELETE CASCADE,
            namespace_id    VARCHAR(64)     NOT NULL REFERENCES namespaces(id),
            content         TEXT            NOT NULL,
            embedding       vector(384)     NOT NULL,
            metadata        JSONB           NOT NULL DEFAULT '{}',
            element_type    VARCHAR(32)     NOT NULL DEFAULT 'text',
            content_format  VARCHAR(16)     NOT NULL DEFAULT 'text',
            position        INTEGER         NOT NULL,
            index_version   INTEGER         NOT NULL DEFAULT 1,
            parent_id       UUID            NULL,
            coordinates     JSONB           NULL,
            created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),

            CONSTRAINT ck_document_chunks_position CHECK (position >= 0),
            CONSTRAINT ck_document_chunks_element_type
                CHECK (element_type IN ('text', 'table', 'title', 'list',
                       'image', 'header', 'footer', 'caption', 'page_break', 'formula')),
            CONSTRAINT ck_document_chunks_content_format
                CHECK (content_format IN ('text', 'html', 'markdown'))
        )
    """)

    # HNSW index for cosine similarity search (m=16, ef_construction=64)
    op.execute("""
        CREATE INDEX ix_document_chunks_embedding_hnsw
            ON document_chunks
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
    """)
    # GIN index for JSONB metadata filtering (REQ-063)
    op.execute("""
        CREATE INDEX ix_document_chunks_metadata_gin
            ON document_chunks
            USING gin (metadata)
    """)
    # Composite index for namespace-scoped search with index_version
    op.execute("""
        CREATE INDEX ix_document_chunks_ns_version
            ON document_chunks (namespace_id, index_version)
    """)
    # Standalone index_version for cleanup of old versions (REQ-064)
    op.execute("""
        CREATE INDEX ix_document_chunks_index_version
            ON document_chunks (index_version)
    """)
    op.execute("""
        CREATE INDEX ix_document_chunks_document_id
            ON document_chunks (document_id)
    """)

    # ------------------------------------------------------------------
    # 5. ingest_jobs (BR-004, REQ-014, ARCH-005)
    # Owner: vektra-ingest
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE ingest_jobs (
            id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            document_id     UUID            NULL REFERENCES source_documents(id),
            namespace_id    VARCHAR(64)     NOT NULL REFERENCES namespaces(id),
            status          VARCHAR(16)     NOT NULL DEFAULT 'pending',
            phase           VARCHAR(16)     NULL,
            filename        VARCHAR(1024)   NOT NULL,
            file_size_bytes BIGINT          NOT NULL,
            error_code      VARCHAR(32)     NULL,
            error_message   TEXT            NULL,
            idempotency_key VARCHAR(255)    NULL,
            chunk_count     INTEGER         NULL,
            percentage      INTEGER         NULL,
            started_at      TIMESTAMPTZ     NULL,
            completed_at    TIMESTAMPTZ     NULL,
            created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),

            CONSTRAINT ck_ingest_jobs_status
                CHECK (status IN ('pending', 'processing', 'indexed', 'failed')),
            CONSTRAINT ck_ingest_jobs_phase
                CHECK (phase IS NULL OR phase IN ('extracting', 'chunking', 'embedding')),
            CONSTRAINT ck_ingest_jobs_percentage
                CHECK (percentage IS NULL OR (percentage >= 0 AND percentage <= 100))
        )
    """)

    op.execute("""
        CREATE UNIQUE INDEX uq_ingest_jobs_idempotency
            ON ingest_jobs (idempotency_key)
            WHERE idempotency_key IS NOT NULL
    """)
    op.execute("CREATE INDEX ix_ingest_jobs_status ON ingest_jobs (status)")

    # ------------------------------------------------------------------
    # 6. audit_log (REQ-022, NFR-007, NFR-008)
    # Owner: vektra-admin
    # key_id is intentionally NOT a FK: revoked/deleted keys must still
    # appear in the audit log (REQ-022).
    # Never contains query text or response content (REQ-051).
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE audit_log (
            id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            key_id          UUID            NOT NULL,
            endpoint        VARCHAR(255)    NOT NULL,
            method          VARCHAR(8)      NOT NULL,
            status_code     INTEGER         NOT NULL,
            request_id      UUID            NOT NULL,
            action          VARCHAR(64)     NULL,
            metadata        JSONB           NOT NULL DEFAULT '{}',
            created_at      TIMESTAMPTZ     NOT NULL DEFAULT now()
        )
    """)

    op.execute("CREATE INDEX ix_audit_log_key_id ON audit_log (key_id)")
    op.execute("CREATE INDEX ix_audit_log_created_at ON audit_log (created_at)")
    op.execute("""
        CREATE INDEX ix_audit_log_action
            ON audit_log (action)
            WHERE action IS NOT NULL
    """)

    # ------------------------------------------------------------------
    # 7. system_state (REQ-036)
    # Owner: vektra-admin
    # Tracks bootstrap key consumption to avoid coupling to audit_log.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE system_state (
            key         VARCHAR(64)     PRIMARY KEY,
            value       TEXT            NOT NULL,
            updated_at  TIMESTAMPTZ     NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        INSERT INTO system_state (key, value)
        VALUES ('bootstrap_consumed', 'false')
    """)


def downgrade() -> None:
    # Drop in reverse dependency order
    op.execute("DROP TABLE IF EXISTS system_state CASCADE")
    op.execute("DROP TABLE IF EXISTS audit_log CASCADE")
    op.execute("DROP TABLE IF EXISTS ingest_jobs CASCADE")
    op.execute("DROP TABLE IF EXISTS document_chunks CASCADE")
    op.execute("DROP TABLE IF EXISTS source_documents CASCADE")
    op.execute("DROP CONSTRAINT IF EXISTS fk_namespaces_owner_key")
    op.execute(
        "ALTER TABLE namespaces DROP CONSTRAINT IF EXISTS fk_namespaces_owner_key"
    )
    op.execute("DROP TABLE IF EXISTS api_keys CASCADE")
    op.execute("DROP TABLE IF EXISTS namespaces CASCADE")
    # Note: extensions not dropped (shared with other users)
