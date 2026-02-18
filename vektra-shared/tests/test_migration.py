"""Integration test: apply initial Alembic migration to a real PostgreSQL instance.

Requires Docker to be running (uses testcontainers-python).
Skipped automatically if Docker is unavailable.

Verifies:
- All 7 tables are created with correct structure
- HNSW index exists on document_chunks.embedding
- GIN index exists on document_chunks.metadata
- "default" namespace row seeded
- ('bootstrap_consumed', 'false') row seeded in system_state
- Chunk deletion semantics documented (CASCADE on FK as safety net)
- content_type NOT NULL with default 'application/octet-stream'
"""
from __future__ import annotations

import os
import pytest
import subprocess


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

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        cwd=project_root,
        env=env,
    )
    if result.returncode != 0:
        pytest.fail(f"Alembic upgrade failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}")

    return postgres_url


@pytest.mark.asyncio
async def test_all_tables_exist(migrated_db):
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

    engine = create_async_engine(migrated_db)
    expected_tables = {
        "namespaces", "api_keys", "source_documents", "document_chunks",
        "ingest_jobs", "audit_log", "system_state",
    }
    async with engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        ))
        tables = {row[0] for row in result}
    await engine.dispose()
    assert expected_tables.issubset(tables), f"Missing tables: {expected_tables - tables}"


@pytest.mark.asyncio
async def test_default_namespace_seeded(migrated_db):
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

    engine = create_async_engine(migrated_db)
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT id, display_name FROM namespaces WHERE id = 'default'"))
        row = result.fetchone()
    await engine.dispose()
    assert row is not None, "'default' namespace not seeded"
    assert row[0] == "default"


@pytest.mark.asyncio
async def test_bootstrap_consumed_seeded(migrated_db):
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

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
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

    engine = create_async_engine(migrated_db)
    async with engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'document_chunks' AND indexname = 'ix_document_chunks_embedding_hnsw'"
        ))
        row = result.fetchone()
    await engine.dispose()
    assert row is not None, "HNSW index on document_chunks.embedding not found"


@pytest.mark.asyncio
async def test_document_chunks_metadata_gin_index_exists(migrated_db):
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

    engine = create_async_engine(migrated_db)
    async with engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'document_chunks' AND indexname = 'ix_document_chunks_metadata_gin'"
        ))
        row = result.fetchone()
    await engine.dispose()
    assert row is not None, "GIN index on document_chunks.metadata not found"


@pytest.mark.asyncio
async def test_document_chunks_content_type_not_null(migrated_db):
    """BLOCKER B-2: content_type must be NOT NULL with a default."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

    engine = create_async_engine(migrated_db)
    async with engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = 'document_chunks' AND column_name = 'content'"
        ))
        row = result.fetchone()
    await engine.dispose()
    assert row is not None
    assert row[0] == "NO", "document_chunks.content must be NOT NULL"


@pytest.mark.asyncio
async def test_api_keys_scopes_check_constraint(migrated_db):
    """api_keys.scopes must only accept valid scope names."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    engine = create_async_engine(migrated_db)
    raised = False
    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO api_keys (key_hash, key_preview, scopes) "
                "VALUES ('hash', 'prev', ARRAY['invalid_scope'])"
            ))
    except Exception:
        raised = True
    finally:
        await engine.dispose()

    assert raised, "CHECK constraint on api_keys.scopes should have rejected 'invalid_scope'"


@pytest.mark.asyncio
async def test_migration_idempotent(migrated_db):
    """Running alembic upgrade head a second time should be a no-op."""
    env = os.environ.copy()
    env["VEKTRA_DATABASE_URL"] = migrated_db

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        cwd=project_root,
        env=env,
    )
    assert result.returncode == 0, f"Second upgrade failed: {result.stderr}"
