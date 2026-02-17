# Implementation Plan: Database schema and Alembic migrations

**ID**: 20260217-infra-database
**Status**: active
**Branch**: N/A
**Created**: 2026-02-17T22:42:39Z
**Updated**: 2026-02-17T22:42:39Z

## Traceability

**Source**: architecture
**Source Type**: architecture

## References

### Requirements
- REQ-033: Duplicate document handling @.s2s/requirements.md
- REQ-034: Content hash algorithm specification @.s2s/requirements.md
- REQ-036: Bootstrap key single-use enforcement @.s2s/requirements.md
- REQ-048: Namespace support @.s2s/requirements.md
- REQ-057: Soft delete for documents @.s2s/requirements.md
- REQ-063: Chunk metadata filtering @.s2s/requirements.md
- REQ-064: Zero-downtime reindex via index_version @.s2s/requirements.md

### Architecture
- ARCH-058: Database schema (7 tables) @.s2s/architecture.md
- ARCH-034: PostgreSQL + pgvector setup @.s2s/architecture.md
- ARCH-040: Forward-compatible data model @.s2s/architecture.md
- ARCH-044: document_chunks schema with forward-compat fields @.s2s/architecture.md
- ARCH-045: source_documents schema @.s2s/architecture.md
- ARCH-047: Namespace as first-class entity @.s2s/architecture.md

### Decisions
- ADR-0009: Namespace isolation via PostgreSQL RLS @.s2s/decisions/ADR-0009-namespace-isolation-rls.md
- ADR-0022: SQLAlchemy 2.0 async with asyncpg @.s2s/decisions/ADR-0022-orm-sqlalchemy-async.md

### Dependencies
- 20260217-component-shared

## Overview

Defines all 7 Phase 1 database tables as SQLAlchemy 2.0 async ORM models and the initial Alembic migration that creates the full schema. This is the foundation that all component implementations build on. The migration also resolves pre-implementation review BLOCKERs B-1/B-3 (chunk deletion semantics) and B-2 (content_type fallback) by codifying the canonical decisions in the schema and comments.

## Design Notes

**BLOCKER B-1/B-3 resolution (chunk deletion)**: `source_documents` uses soft delete (sets `deleted_at`, `deletion_reason`). `document_chunks` are hard-deleted in the same database transaction via `DELETE FROM document_chunks WHERE document_id = :id`. The chunk count before deletion is captured with `SELECT COUNT(*) WHERE document_id = :id` in the same transaction and returned as `chunks_removed`. This resolves the CASCADE ambiguity and provides the data source for the response field.

**BLOCKER B-2 resolution (content_type)**: `document_chunks.content_type` is NOT NULL with default `'application/octet-stream'`. Magic bytes detection (REQ-058) populates the actual MIME type; if detection fails, the fallback is `'application/octet-stream'` (never NULL). This decision is documented in the migration comment.

ORM models live inside each component's module directory (not in vektra_shared), following ADR-0022. Alembic is configured with separate version branches per component-owned table group (admin branch: api_keys, audit_log, namespaces; ingest branch: source_documents, document_chunks, ingest_jobs; shared: system_state). All branches merge into the initial migration.

## Tasks

- [ ] Configure SQLAlchemy 2.0 async engine in `vektra_shared/db.py`: `create_async_engine()` with `asyncpg` driver, connection pool settings, and `AsyncSession` factory; expose `get_session()` as FastAPI dependency
- [ ] Create Alembic configuration at repo root: `alembic.ini` pointing to migrations directory, `env.py` using `AsyncConnection.run_sync()` pattern for async migrations, multi-branch setup
- [ ] Create initial migration `migrations/versions/0001_initial_schema.py` that: enables pgvector extension (`CREATE EXTENSION IF NOT EXISTS vector`), enables pgcrypto (`CREATE EXTENSION IF NOT EXISTS pgcrypto`), creates all 7 tables (see tasks below)
- [ ] Define `namespaces` table: `id` UUID PK, `display_name` TEXT NOT NULL, `owner_key_id` UUID nullable FK api_keys.id, `quota_chunks` INT nullable, `quota_documents` INT nullable, `config` JSONB nullable, `retention_days` INT nullable, `created_at` TIMESTAMPTZ NOT NULL DEFAULT now(), `updated_at` TIMESTAMPTZ NOT NULL DEFAULT now()`; seed "default" namespace in migration
- [ ] Define `api_keys` table: `id` UUID PK, `key_hash` TEXT NOT NULL (argon2id), `key_preview` CHAR(4) NOT NULL, `label` TEXT nullable, `scopes` TEXT[] NOT NULL DEFAULT '{admin}', `created_at` TIMESTAMPTZ NOT NULL DEFAULT now(), `last_used_at` TIMESTAMPTZ nullable, `revoked_at` TIMESTAMPTZ nullable; CHECK constraint on scopes array values (admin/ingest/query)
- [ ] Define `source_documents` table: `id` UUID PK, `namespace_id` UUID NOT NULL FK namespaces.id, `filename` TEXT NOT NULL, `content_hash` TEXT NOT NULL (SHA-256 hex), `file_size_bytes` BIGINT NOT NULL, `content_type` TEXT NOT NULL, `chunk_count` INT NOT NULL DEFAULT 0, `version` INT NOT NULL DEFAULT 1, `supersedes_id` UUID nullable FK source_documents.id, `deleted_at` TIMESTAMPTZ nullable, `deletion_reason` TEXT nullable CHECK (deletion_reason IN ('user_request','superseded','expired')), `filename_aliases` TEXT[] NOT NULL DEFAULT '{}', `created_at` TIMESTAMPTZ NOT NULL DEFAULT now(), `updated_at` TIMESTAMPTZ NOT NULL DEFAULT now()`; UNIQUE INDEX on (content_hash, namespace_id) WHERE deleted_at IS NULL
- [ ] Define `document_chunks` table: `id` UUID PK, `document_id` UUID NOT NULL FK source_documents.id ON DELETE CASCADE (hard delete via explicit query, CASCADE as safety net), `namespace_id` UUID NOT NULL FK namespaces.id, `content` TEXT NOT NULL, `content_format` TEXT NOT NULL DEFAULT 'text', `content_type` TEXT NOT NULL DEFAULT 'application/octet-stream', `embedding` vector(384) NOT NULL (dimensionality matches all-MiniLM-L6-v2), `chunk_index` INT NOT NULL, `token_count` INT NOT NULL, `metadata` JSONB NOT NULL DEFAULT '{}', `index_version` INT NOT NULL DEFAULT 1, `page_number` INT nullable, `created_at` TIMESTAMPTZ NOT NULL DEFAULT now()`; HNSW index on embedding column (`vector_cosine_ops`); GIN index on metadata; B-tree index on index_version; composite index on (namespace_id, index_version)
- [ ] Define `ingest_jobs` table: `id` UUID PK, `document_id` UUID nullable FK source_documents.id, `namespace_id` UUID NOT NULL FK namespaces.id, `status` TEXT NOT NULL DEFAULT 'pending' CHECK IN ('pending','processing','indexed','failed'), `phase` TEXT nullable CHECK IN ('extracting','chunking','embedding'), `error_code` TEXT nullable, `error_message` TEXT nullable, `created_at` TIMESTAMPTZ NOT NULL DEFAULT now(), `updated_at` TIMESTAMPTZ NOT NULL DEFAULT now()`
- [ ] Define `audit_log` table: `id` UUID PK, `key_id` UUID nullable FK api_keys.id, `endpoint` TEXT NOT NULL, `method` TEXT NOT NULL, `status_code` INT NOT NULL, `request_id` UUID NOT NULL, `action` TEXT nullable (for named events: bootstrap_key_consumed, document_deduplicated, document_aliased, apikey_created, apikey_revoked), `timestamp` TIMESTAMPTZ NOT NULL DEFAULT now()`; index on timestamp for retention queries
- [ ] Define `system_state` table: `key` TEXT PK, `value` TEXT NOT NULL, `updated_at` TIMESTAMPTZ NOT NULL DEFAULT now()`; seed row `('bootstrap_key_consumed', 'false')` in migration
- [ ] Write `startup_validation.py` step for database (ARCH-057 step 2+3): verify connectivity with a test query, verify Alembic current head matches deployed schema, raise `StartupValidationError` with remediation hint on mismatch
- [ ] Write a migration test: apply migration to a test PostgreSQL instance, verify all tables exist with correct columns and indexes (use pytest + testcontainers or docker-compose test profile)

## Acceptance Criteria

- [ ] `alembic upgrade head` runs on a fresh PostgreSQL instance without error
- [ ] All 7 tables created with correct columns, types, NOT NULL constraints, and CHECK constraints
- [ ] HNSW index on `document_chunks.embedding`, GIN on `document_chunks.metadata`, B-tree on `document_chunks.index_version`
- [ ] "default" namespace row exists after migration
- [ ] `system_state` row `('bootstrap_key_consumed', 'false')` exists after migration
- [ ] Chunk deletion semantics documented in migration comment: source_documents soft-delete, document_chunks hard-delete via explicit DELETE (cascade is safety net)
- [ ] `document_chunks.content_type` NOT NULL with default `'application/octet-stream'` confirmed

## Testing Approach

Integration test using a real PostgreSQL instance (testcontainers or docker-compose `test` profile). Apply migration, then assert table structure via `information_schema` queries. Test idempotency: applying migration twice should be a no-op. Teardown drops all tables.

## Integration Notes

All component ORM models import the `AsyncSession` factory from `vektra_shared/db.py`. Table definitions via SQLAlchemy ORM `DeclarativeBase` live in each component's models file. The migration must be applied before any component starts accepting requests (enforced by startup validation step 3 in ARCH-057).
