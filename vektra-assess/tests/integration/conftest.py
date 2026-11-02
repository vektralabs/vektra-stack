"""Real-Postgres fixture for the assess data tests (plan assess-data-crypto 6.1).

One migrated pgvector/pg16 container per module, three login roles (superuser, the real
``vektra_assess_api`` and ``vektra_assess_worker``). The fixture asserts ``current_user``
for each engine before yielding, so a fixture that silently connects as superuser fails
instead of making every grant test pass. Without Docker the tests SKIP with a reason (the
CI integration job treats a fully skipped assess integration suite as a failure).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

ROOT = Path(__file__).resolve().parents[3]
ROLE_PASSWORD = "test-role-password-0123456789"
ROLES = {"api": "vektra_assess_api", "worker": "vektra_assess_worker"}


@dataclass(frozen=True)
class Pg:
    super_url: str

    def url(self, who: str) -> str:
        if who == "super":
            return self.super_url
        return (
            make_url(self.super_url)
            .set(username=ROLES[who], password=ROLE_PASSWORD)
            .render_as_string(hide_password=False)
        )

    @asynccontextmanager
    async def conn(self, who: str = "super") -> AsyncIterator[AsyncConnection]:
        engine = create_async_engine(self.url(who))
        try:
            async with engine.connect() as c:
                yield c
        finally:
            await engine.dispose()

    async def run(
        self, who: str, sql: str, params: dict[str, Any] | None = None
    ) -> list[Any]:
        """Run one statement in its own transaction as ``who``; return rows."""
        async with self.conn(who) as c:
            try:
                r = await c.execute(text(sql), params or {})
                rows = list(r.fetchall()) if r.returns_rows else []
                await c.commit()
                return rows
            except BaseException:
                await c.rollback()
                raise


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "VEKTRA_DATABASE_URL": url}
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
    )


async def _prepare(pg: Pg) -> None:
    for role in ROLES.values():
        await pg.run("super", f"ALTER ROLE {role} LOGIN PASSWORD '{ROLE_PASSWORD}'")
    for who, role in (("super", None), *ROLES.items()):
        row = (
            await pg.run(
                who,
                "SELECT current_user, rolsuper FROM pg_roles "
                "WHERE rolname = current_user",
            )
        )[0]
        if role is None:
            assert row[1] is True
        else:
            assert tuple(row) == (role, False), f"fixture connected as {tuple(row)}"


@pytest.fixture(scope="module")
def pg() -> Iterator[Pg]:
    docker = pytest.importorskip("docker", reason="docker SDK not installed")
    try:
        docker.from_env().ping()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Docker not available: {exc}")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16") as c:
        url = c.get_connection_url().replace("psycopg2", "asyncpg")
        res = _alembic(url, "upgrade", "head")
        assert res.returncode == 0, res.stderr
        handle = Pg(url)
        asyncio.run(_prepare(handle))
        yield handle


GENESIS = "genesis-hash"


async def make_session(pg: Pg, state: str = "ACTIVE", *, epoch: int = 1) -> uuid.UUID:
    """A site-bound session with snapshot, opening turn and (live) hand-off row."""
    site, sid = uuid.uuid4(), uuid.uuid4()
    async with pg.conn("super") as c:
        await c.execute(
            text(
                "INSERT INTO assess_sites (id, moodle_site_uuid, base_url) "
                "VALUES (:i, :u, 'https://m.example')"
            ),
            {"i": site, "u": str(site)},
        )
        closed = "'finished'" if state == "CLOSED" else "NULL"
        await c.execute(
            text(
                "INSERT INTO assess_sessions (id, site_id, cmid, student_userid, state, "
                f"close_reason, epoch) VALUES (:s, :site, 1, 1, :st, {closed}, :e)"
            ),
            {"s": sid, "site": site, "st": state, "e": epoch},
        )
        if state != "STARTING":
            await c.execute(
                text(
                    "INSERT INTO assess_snapshots (session_id, ciphertext, content_hash) "
                    "VALUES (:s, '\\x01', :h)"
                ),
                {"s": sid, "h": GENESIS},
            )
            await c.execute(
                text(
                    "INSERT INTO assess_turns (session_id, turn_no, role, kind, ciphertext, "
                    "turn_hash, prev_hash) VALUES (:s, 1, 'bot', 'opening', '\\x02', 'h1', :g)"
                ),
                {"s": sid, "g": GENESIS},
            )
        if state in ("ACTIVE", "OUTAGE"):
            await c.execute(
                text(
                    "INSERT INTO assess_session_handoff (session_id, session_state, epoch, "
                    "sealed_dek, handoff_kid) VALUES (:s, :st, :e, '\\x03', 'k1')"
                ),
                {"s": sid, "st": state, "e": epoch},
            )
        await c.commit()
    return sid
