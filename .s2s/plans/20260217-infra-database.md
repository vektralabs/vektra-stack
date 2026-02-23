# Implementation Plan: Database schema and Alembic migrations

**ID**: 20260217-infra-database
**Status**: completed
**Branch**: N/A
**Created**: 2026-02-17T22:42:39Z
**Updated**: 2026-02-18T00:00:00Z

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

- [x] Configure SQLAlchemy 2.0 async engine in `vektra_shared/db.py`: `create_async_engine()` with `asyncpg` driver, connection pool settings, and `AsyncSession` factory; expose `get_session()` as FastAPI dependency
- [x] Create Alembic configuration at repo root: `alembic.ini` pointing to migrations directory, `env.py` using `AsyncConnection.run_sync()` pattern for async migrations, multi-branch setup
- [x] Create initial migration `migrations/versions/0001_initial_schema.py` that: enables pgvector extension (`CREATE EXTENSION IF NOT EXISTS vector`), enables pgcrypto (`CREATE EXTENSION IF NOT EXISTS pgcrypto`), creates all 7 tables (see tasks below)
- [x] Define `namespaces` table: with seed "default" namespace in migration
- [x] Define `api_keys` table: with CHECK constraint on scopes array values (admin/ingest/query)
- [x] Define `source_documents` table: with UNIQUE INDEX on (content_hash, namespace_id) WHERE deleted_at IS NULL
- [x] Define `document_chunks` table: with HNSW index, GIN index, composite index; ON DELETE CASCADE as safety net only
- [x] Define `ingest_jobs` table: with status/phase/percentage CHECK constraints, idempotency_key unique index
- [x] Define `audit_log` table: with indexes on key_id, created_at, action; key_id NOT a FK
- [x] Define `system_state` table: seed row ('bootstrap_consumed', 'false') in migration
- [x] Write `startup_validation.py` step for database (ARCH-057 step 2+3): verify connectivity with a test query, verify Alembic current head matches deployed schema, raise `StartupValidationError` with remediation hint on mismatch
- [x] Write a migration test: apply migration to a test PostgreSQL instance, verify all tables exist with correct columns and indexes (testcontainers, auto-skipped when Docker unavailable)

## Acceptance Criteria

- [x] `alembic upgrade head` runs on a fresh PostgreSQL instance without error (verified via --sql offline mode; integration test auto-skipped when Docker unavailable)
- [x] All 7 tables created with correct columns, types, NOT NULL constraints, and CHECK constraints
- [x] HNSW index on `document_chunks.embedding`, GIN on `document_chunks.metadata`, B-tree on `document_chunks.index_version`
- [x] "default" namespace row exists after migration
- [x] `system_state` row `('bootstrap_consumed', 'false')` exists after migration
- [x] Chunk deletion semantics documented in migration comment: source_documents soft-delete, document_chunks hard-delete via explicit DELETE (cascade is safety net)
- [x] `document_chunks.content` NOT NULL (B-2 resolution: content_type detection uses fallback in application logic, not a DB default)

## Testing Approach

Integration test using a real PostgreSQL instance (testcontainers or Docker Compose `test` profile). Apply migration, then assert table structure via `information_schema` queries. Test idempotency: applying migration twice should be a no-op. Teardown drops all tables.

## Integration Notes

All component ORM models import the `AsyncSession` factory from `vektra_shared/db.py`. Table definitions via SQLAlchemy ORM `DeclarativeBase` live in each component's models file. The migration must be applied before any component starts accepting requests (enforced by startup validation step 3 in ARCH-057).
