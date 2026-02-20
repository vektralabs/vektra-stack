"""SQLAlchemy 2.0 async engine and session factory (ADR-0022).

Creates the async engine from VEKTRA_DATABASE_URL and exposes
`get_session()` as a FastAPI dependency.

ORM models (DeclarativeBase subclasses) live in each component's
models file, not here. This module provides only the connection layer.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

# Module-level engine and session factory; initialized by `init_db()`.
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_db(database_url: str, **engine_kwargs: object) -> None:
    """Initialize the async engine and session factory.

    Call this once at application startup (ARCH-057 step 2), before
    registering routes. Thread-safe because it is called once on startup.

    Args:
        database_url: AsyncPG connection string
            (e.g., 'postgresql+asyncpg://user:pass@host/db').
        **engine_kwargs: Forwarded to `create_async_engine()`.
    """
    global _engine, _session_factory
    _engine = create_async_engine(
        database_url,
        pool_size=engine_kwargs.pop("pool_size", 10),
        max_overflow=engine_kwargs.pop("max_overflow", 20),
        pool_pre_ping=engine_kwargs.pop("pool_pre_ping", True),
        echo=engine_kwargs.pop("echo", False),
        **engine_kwargs,
    )
    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


def get_engine() -> AsyncEngine:
    """Return the initialized async engine.

    Raises RuntimeError if `init_db()` has not been called.
    """
    if _engine is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _engine


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an AsyncSession per request.

    Usage:
        @router.get("/")
        async def handler(session: AsyncSession = Depends(get_session)):
            ...
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    async with _session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
