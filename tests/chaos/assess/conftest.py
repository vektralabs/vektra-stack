"""One migrated pgvector/pg16 container per module with the real worker role and password."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

# importlib import mode does not put this directory on sys.path; the helpers are siblings
sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import ROLE_PASSWORD

ROOT = Path(__file__).resolve().parents[3]
ROLES = {"worker": "vektra_assess_worker", "api": "vektra_assess_api"}


@dataclass(frozen=True)
class Pg:
    super_url: str
    container_id: str = ""

    def url(self, who: str) -> str:
        if who == "super":
            return self.super_url
        return (
            make_url(self.super_url)
            .set(username=ROLES[who], password=ROLE_PASSWORD)
            .render_as_string(hide_password=False)
        )

    async def run(self, who: str, sql: str, params: dict | None = None) -> list:  # type: ignore[type-arg]
        engine = create_async_engine(self.url(who), pool_pre_ping=True)
        try:
            async with engine.begin() as conn:
                r = await conn.execute(text(sql), params or {})
                return list(r.fetchall()) if r.returns_rows else []
        finally:
            await engine.dispose()


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "VEKTRA_DATABASE_URL": url}
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
    )


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _migrated_pg(fixed_port: int | None) -> Iterator[Pg]:
    docker = pytest.importorskip("docker", reason="docker SDK not installed")
    try:
        docker.from_env().ping()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Docker not available: {exc}")
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer("pgvector/pgvector:pg16")
    if fixed_port is not None:
        # a restarted container must come back on the same host port: the worker's DSN is fixed
        container = container.with_bind_ports(5432, fixed_port)
    with container as c:
        url = c.get_connection_url().replace("psycopg2", "asyncpg")
        res = _alembic(url, "upgrade", "heads")
        assert res.returncode == 0, res.stderr
        handle = Pg(url, c.get_wrapped_container().id)

        async def prepare() -> None:
            for role in ROLES.values():
                await handle.run(
                    "super", f"ALTER ROLE {role} LOGIN PASSWORD '{ROLE_PASSWORD}'"
                )

        asyncio.run(prepare())
        yield handle


@pytest.fixture(scope="module")
def pg() -> Iterator[Pg]:
    yield from _migrated_pg(None)


@pytest.fixture(scope="module")
def pg_fixed_port() -> Iterator[Pg]:
    """Postgres on a fixed host port, for the scenarios that restart the container."""
    yield from _migrated_pg(_free_port())
