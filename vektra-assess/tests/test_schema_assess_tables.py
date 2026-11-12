"""The assess tables of the assess base revision (plan assess-data-crypto 1.6, plan 2a-3).

upgrade heads -> downgrade assess@base -> upgrade heads on an empty schema; the table list is
exactly the expected one; every ORM mirror matches the migrated columns; the downgrade refuses
while an ``assess_sessions`` row exists. Real Postgres (testcontainers), ``integration``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "VEKTRA_DATABASE_URL": url}
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
    )


@pytest.fixture(scope="module")
def pg():
    docker = pytest.importorskip("docker", reason="docker SDK not installed")
    try:
        docker.from_env().ping()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Docker not available: {exc}")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16") as c:
        url = c.get_connection_url().replace("psycopg2", "asyncpg")
        res = _alembic(url, "upgrade", "heads")
        assert res.returncode == 0, res.stderr
        yield url


async def _fetch(url, sql, params=None):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            r = await conn.execute(text(sql), params or {})
            return r.fetchall() if r.returns_rows else []
    finally:
        await engine.dispose()


async def _tables(url):
    rows = await _fetch(
        url,
        "SELECT tablename FROM pg_tables WHERE schemaname='public' "
        "AND tablename LIKE 'assess\\_%'",
    )
    return {r[0] for r in rows}


async def test_tables_are_exactly_the_orm_mirrors(pg):
    from vektra_assess.db.models import Base

    expected = set(Base.metadata.tables)
    got = await _tables(pg)
    assert got, "no assess_% table: the migration did not run"
    assert got == expected, (sorted(got - expected), sorted(expected - got))


async def test_orm_columns_match_the_database(pg):
    from vektra_assess.db.models import Base

    rows = await _fetch(
        pg,
        "SELECT table_name, column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name LIKE 'assess\\_%'",
    )
    db: dict[str, dict[str, bool]] = {}
    for t, c, n in rows:
        db.setdefault(t, {})[c] = n == "YES"
    for name, table in Base.metadata.tables.items():
        assert set(db[name]) == {c.name for c in table.columns}, name
        for col in table.columns:
            if not col.primary_key:
                assert db[name][col.name] == col.nullable, (name, col.name)


async def test_downgrade_roundtrip_on_empty_schema_and_refusal(pg):
    res = _alembic(pg, "downgrade", "assess@base")
    assert res.returncode == 0, res.stderr
    assert await _tables(pg) == set()
    res = _alembic(pg, "upgrade", "heads")
    assert res.returncode == 0, res.stderr
    assert await _tables(pg)

    await _fetch(
        pg,
        "INSERT INTO assess_sites (id, lms_site_uuid, base_url) "
        "VALUES ('00000000-0000-0000-0000-000000000001', 's', 'https://m')",
    )
    # a STARTING session needs no opening turn
    await _fetch(
        pg,
        "INSERT INTO assess_sessions (site_id, cmid, student_userid, state) "
        "VALUES ('00000000-0000-0000-0000-000000000001', 1, 1, 'STARTING')",
    )
    res = _alembic(pg, "downgrade", "assess@base")
    assert res.returncode != 0
    assert "downgrade refused" in res.stderr
    assert (await _fetch(pg, "SELECT count(*) FROM assess_sessions"))[0][0] == 1
    await _fetch(pg, "DELETE FROM assess_sessions")
    await _fetch(pg, "DELETE FROM assess_sites")
    assert _alembic(pg, "downgrade", "assess@base").returncode == 0
    assert _alembic(pg, "upgrade", "heads").returncode == 0
