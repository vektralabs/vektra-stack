"""Alembic migration environment (async asyncpg mode).

Uses AsyncConnection.run_sync() for async migrations per ADR-0022.
Database URL is read from VEKTRA_DATABASE_URL env var, falling back
to the value in alembic.ini.

ORM models are NOT imported here. The initial migration uses raw SQL
to avoid coupling migration history to internal module boundaries (ADR-0005).
"""
from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

# Alembic Config object
config = context.config

# Logging setup from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Resolve database URL: prefer VEKTRA_DATABASE_URL env var
database_url = os.environ.get(
    "VEKTRA_DATABASE_URL",
    config.get_main_option("sqlalchemy.url"),
)

# Ensure asyncpg driver is specified
if database_url and "postgresql://" in database_url and "+asyncpg" not in database_url:
    database_url = database_url.replace("postgresql://", "postgresql+asyncpg://")

# Target metadata is None: migrations use raw SQL (op.execute)
target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (generate SQL without a live DB connection)."""
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:  # noqa: ANN001
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations using an async engine (asyncpg)."""
    connectable = create_async_engine(database_url)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode with a live DB connection."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
