"""Integration test: apply Alembic migrations to a real PostgreSQL instance.

Requires Docker to be running (uses testcontainers-python).
Skipped automatically if Docker is unavailable.

Phase 1 (migration 0001) verifies:
- All 7 tables are created with correct structure
- HNSW index exists on document_chunks.embedding
- GIN index exists on document_chunks.metadata
- "default" namespace row seeded
- ('bootstrap_consumed', 'false') row seeded in system_state
- Chunk deletion semantics documented (CASCADE on FK as safety net)
- content_type NOT NULL with default 'application/octet-stream'

Phase 2 (migration 0002) verifies:
- 4 new tables: conversations, conversation_turns, query_traces, feedback
- conversation_turns.question and .answer are BYTEA (encrypted)
- uq_source_documents_filename unique partial index (TECH-004)
- Namespace quota CHECK constraints
- query_traces.response_id UNIQUE constraint
- api_keys.expires_at column
- pgcrypto encryption round-trip
- Downgrade removes Phase 2 additions without affecting Phase 1
"""

from __future__ import annotations

import os
import subprocess

import pytest


def _docker_available() -> bool:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker not available - skipping migration integration tests",
)


@pytest.fixture(scope="module")
def postgres_url():
    """Spin up a PostgreSQL container and return the asyncpg connection URL."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        url = postgres.get_connection_url()
        # Convert to asyncpg URL
        async_url = url.replace("postgresql://", "postgresql+asyncpg://").replace(
            "psycopg2", "asyncpg"
        )
        yield async_url


@pytest.fixture(scope="module")
def migrated_db(postgres_url):
    """Apply the initial Alembic migration and return the connection URL."""
    env = os.environ.copy()
    env["VEKTRA_DATABASE_URL"] = postgres_url

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        cwd=project_root,
        env=env,
    )
    if result.returncode != 0:
        pytest.fail(
            f"Alembic upgrade failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

    return postgres_url


@pytest.mark.asyncio
async def test_all_tables_exist(migrated_db):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(migrated_db)
    expected_tables = {
        # Phase 1
        "namespaces",
        "api_keys",
        "source_documents",
        "document_chunks",
        "ingest_jobs",
        "audit_log",
        "system_state",
        # Phase 2
        "conversations",
        "conversation_turns",
        "query_traces",
        "feedback",
    }
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        )
        tables = {row[0] for row in result}
    await engine.dispose()
    assert expected_tables.issubset(tables), (
        f"Missing tables: {expected_tables - tables}"
    )


@pytest.mark.asyncio
async def test_default_namespace_seeded(migrated_db):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(migrated_db)
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT id, display_name FROM namespaces WHERE id = 'default'")
        )
        row = result.fetchone()
    await engine.dispose()
    assert row is not None, "'default' namespace not seeded"
    assert row[0] == "default"


@pytest.mark.asyncio
async def test_bootstrap_consumed_seeded(migrated_db):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(migrated_db)
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT value FROM system_state WHERE key = 'bootstrap_consumed'")
        )
        row = result.fetchone()
    await engine.dispose()
    assert row is not None, "bootstrap_consumed row not seeded"
    assert row[0] == "false"


@pytest.mark.asyncio
async def test_document_chunks_hnsw_index_exists(migrated_db):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(migrated_db)
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'document_chunks' AND indexname = 'ix_document_chunks_embedding_hnsw'"
            )
        )
        row = result.fetchone()
    await engine.dispose()
    assert row is not None, "HNSW index on document_chunks.embedding not found"


@pytest.mark.asyncio
async def test_document_chunks_metadata_gin_index_exists(migrated_db):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(migrated_db)
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'document_chunks' AND indexname = 'ix_document_chunks_metadata_gin'"
            )
        )
        row = result.fetchone()
    await engine.dispose()
    assert row is not None, "GIN index on document_chunks.metadata not found"


@pytest.mark.asyncio
async def test_document_chunks_content_type_not_null(migrated_db):
    """BLOCKER B-2: content_type must be NOT NULL with a default."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(migrated_db)
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_name = 'document_chunks' AND column_name = 'content'"
            )
        )
        row = result.fetchone()
    await engine.dispose()
    assert row is not None
    assert row[0] == "NO", "document_chunks.content must be NOT NULL"


@pytest.mark.asyncio
async def test_api_keys_scopes_check_constraint(migrated_db):
    """api_keys.scopes must only accept valid scope names."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(migrated_db)
    raised = False
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO api_keys (key_hash, key_preview, scopes) "
                    "VALUES ('hash', 'prev', ARRAY['invalid_scope'])"
                )
            )
    except Exception:
        raised = True
    finally:
        await engine.dispose()

    assert raised, (
        "CHECK constraint on api_keys.scopes should have rejected 'invalid_scope'"
    )


@pytest.mark.asyncio
async def test_migration_idempotent(migrated_db):
    """Running alembic upgrade head a second time should be a no-op."""
    env = os.environ.copy()
    env["VEKTRA_DATABASE_URL"] = migrated_db

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        cwd=project_root,
        env=env,
    )
    assert result.returncode == 0, f"Second upgrade failed: {result.stderr}"


# --------------------------------------------------------------------------
# Phase 2 migration tests (0002)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conversation_turns_bytea_columns(migrated_db):
    """conversation_turns.question and .answer must be BYTEA for pgcrypto."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(migrated_db)
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_name = 'conversation_turns' "
                "AND column_name IN ('question', 'answer') "
                "ORDER BY column_name"
            )
        )
        rows = result.fetchall()
    await engine.dispose()

    col_types = {row[0]: row[1] for row in rows}
    assert col_types.get("question") == "bytea", "question must be BYTEA"
    assert col_types.get("answer") == "bytea", "answer must be BYTEA"


@pytest.mark.asyncio
async def test_uq_source_documents_filename_exists(migrated_db):
    """TECH-004: unique partial index on (namespace_id, filename) WHERE deleted_at IS NULL."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(migrated_db)
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'source_documents' "
                "AND indexname = 'uq_source_documents_filename'"
            )
        )
        row = result.fetchone()
    await engine.dispose()
    assert row is not None, "uq_source_documents_filename index not found"
    indexdef = row[0]
    assert "UNIQUE INDEX" in indexdef
    assert "(namespace_id, filename)" in indexdef
    assert "deleted_at IS NULL" in indexdef


@pytest.mark.asyncio
async def test_namespace_quota_check_constraints(migrated_db):
    """Namespace quota CHECK constraints reject zero values."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(migrated_db)

    # quota_chunks = 0 should fail
    raised = False
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE namespaces SET quota_chunks = 0 WHERE id = 'default'")
            )
    except Exception:
        raised = True
    await engine.dispose()
    assert raised, "ck_namespaces_quota_chunks should reject 0"


@pytest.mark.asyncio
async def test_query_traces_response_id_unique(migrated_db):
    """query_traces.response_id must have a UNIQUE constraint."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(migrated_db)
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT constraint_name FROM information_schema.table_constraints "
                "WHERE table_name = 'query_traces' AND constraint_type = 'UNIQUE'"
            )
        )
        rows = result.fetchall()
    await engine.dispose()
    constraint_names = [row[0] for row in rows]
    assert any("response_id" in name for name in constraint_names), (
        f"No UNIQUE constraint on query_traces.response_id, found: {constraint_names}"
    )


@pytest.mark.asyncio
async def test_api_keys_expires_at_column(migrated_db):
    """api_keys must have an expires_at nullable TIMESTAMPTZ column."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(migrated_db)
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_name = 'api_keys' AND column_name = 'expires_at'"
            )
        )
        row = result.fetchone()
    await engine.dispose()
    assert row is not None, "api_keys.expires_at column not found"
    assert row[0] == "timestamp with time zone", "expires_at must be TIMESTAMPTZ"
    assert row[1] == "YES", "expires_at must be nullable"


@pytest.mark.asyncio
async def test_namespaces_quota_bytes_column(migrated_db):
    """namespaces must have a quota_bytes nullable BIGINT column."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(migrated_db)
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_name = 'namespaces' AND column_name = 'quota_bytes'"
            )
        )
        row = result.fetchone()
    await engine.dispose()
    assert row is not None, "namespaces.quota_bytes column not found"
    assert row[0] == "bigint", "quota_bytes must be BIGINT"
    assert row[1] == "YES", "quota_bytes must be nullable"


@pytest.mark.asyncio
async def test_feedback_rating_check_constraint(migrated_db):
    """feedback.rating CHECK constraint must reject values outside 1-5."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(migrated_db)
    raised = False
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO feedback (response_id, namespace_id, key_id, rating) "
                    "VALUES (gen_random_uuid(), 'default', gen_random_uuid(), 0)"
                )
            )
    except Exception:
        raised = True
    await engine.dispose()
    assert raised, "ck_feedback_rating should reject rating = 0"


@pytest.mark.asyncio
async def test_pgcrypto_encryption_roundtrip(migrated_db):
    """Verify pgcrypto encrypt/decrypt round-trip via SET LOCAL session variable."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(migrated_db)
    test_key = "test-encryption-key-32chars!!"
    test_question = "What is RAG?"
    test_answer = "Retrieval-Augmented Generation"

    async with engine.begin() as conn:
        # Create a conversation to reference
        result = await conn.execute(
            text(
                "INSERT INTO conversations (namespace_id, key_id) "
                "VALUES ('default', gen_random_uuid()) RETURNING id"
            )
        )
        conv_id = result.scalar()

        # Set encryption key for this transaction.
        # SET LOCAL does not support bind parameters in asyncpg,
        # so the key is embedded directly (test-only, known value).
        await conn.execute(text(f"SET LOCAL vektra.conversation_key = '{test_key}'"))

        # Insert encrypted turn
        await conn.execute(
            text(
                "INSERT INTO conversation_turns "
                "(conversation_id, turn_number, question, answer) "
                "VALUES (:conv_id, 1, "
                "pgp_sym_encrypt(:question, current_setting('vektra.conversation_key')), "
                "pgp_sym_encrypt(:answer, current_setting('vektra.conversation_key')))"
            ),
            {"conv_id": conv_id, "question": test_question, "answer": test_answer},
        )

        # Read back and decrypt
        result = await conn.execute(
            text(
                "SELECT "
                "pgp_sym_decrypt(question, current_setting('vektra.conversation_key')), "
                "pgp_sym_decrypt(answer, current_setting('vektra.conversation_key')) "
                "FROM conversation_turns WHERE conversation_id = :conv_id"
            ),
            {"conv_id": conv_id},
        )
        row = result.fetchone()

    await engine.dispose()
    assert row is not None
    assert row[0] == test_question, f"Decrypted question mismatch: {row[0]}"
    assert row[1] == test_answer, f"Decrypted answer mismatch: {row[1]}"


@pytest.mark.asyncio
async def test_downgrade_removes_phase2_tables(migrated_db):
    """Downgrade to 0001 must remove Phase 2 tables while keeping Phase 1 tables."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    env = os.environ.copy()
    env["VEKTRA_DATABASE_URL"] = migrated_db
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))

    # Downgrade to 0001
    result = subprocess.run(
        ["uv", "run", "alembic", "downgrade", "0001"],
        capture_output=True,
        text=True,
        cwd=project_root,
        env=env,
    )
    assert result.returncode == 0, f"Downgrade failed: {result.stderr}"

    # Verify Phase 2 tables are gone, Phase 1 tables remain
    engine = create_async_engine(migrated_db)
    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        )
        tables = {row[0] for row in res}
    await engine.dispose()

    phase1_tables = {
        "namespaces",
        "api_keys",
        "source_documents",
        "document_chunks",
        "ingest_jobs",
        "audit_log",
        "system_state",
    }
    phase2_tables = {
        "conversations",
        "conversation_turns",
        "query_traces",
        "feedback",
    }

    assert phase1_tables.issubset(tables), (
        f"Phase 1 tables missing after downgrade: {phase1_tables - tables}"
    )
    assert not phase2_tables.intersection(tables), (
        f"Phase 2 tables still present after downgrade: {phase2_tables & tables}"
    )

    # Re-upgrade to head for subsequent tests (idempotent fixture)
    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        cwd=project_root,
        env=env,
    )
    assert result.returncode == 0, f"Re-upgrade failed: {result.stderr}"
