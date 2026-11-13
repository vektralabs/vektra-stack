"""The assess schema as its own Alembic branch (ADR-0033 D7, plan 2a-3.4).

Four claims, each counted on a real cluster rather than read off a green migration:

1. the squash equals the old chain: the assess base revision's schema (``pg_dump
   --schema-only``, normalised) and the effective privileges of the three roles are identical
   to the goldens taken from revisions 0001 to 0044, with ADR-0033 D5's renames applied;
2. with assess off, ``docker/entrypoint.sh`` migrates the platform branch only: 0 ``assess_*``
   tables, 0 ``vektra_assess_*`` roles, 0 assess functions;
3. with assess on, it migrates both branches: every table, role and function of the head;
4. the downgrade of the assess branch gives back the platform schema exactly.

The managed-Postgres cases (assess off needs no CREATEROLE; without CREATEROLE the assess branch
stops with the message naming all three roles) are ``test_migration_managed_postgres.py``.

Regenerate the goldens after a NEW assess revision (never to make this test pass otherwise):
``VEKTRA_REGEN_SCHEMA_GOLDEN=1 uv run pytest vektra-assess/tests/integration/test_schema_branch.py
-m integration``, then review the diff: it must show that revision's change and nothing else.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from support.schema_dump import privilege_text, schema_text

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
GOLDEN = ROOT / "vektra-assess" / "tests" / "golden"
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"
REGEN = os.environ.get("VEKTRA_REGEN_SCHEMA_GOLDEN") == "1"

#: what the assess head has (the goldens say exactly which; these are the sizes)
TABLES, ROLES, FUNCTIONS = 37, 3, 17

COUNTS = (
    "SELECT (SELECT count(*) FROM pg_tables WHERE schemaname = 'public' "
    "        AND tablename LIKE 'assess\\_%'), "
    "(SELECT count(*) FROM pg_roles WHERE rolname LIKE 'vektra\\_assess\\_%'), "
    "(SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace "
    "        AND (proname LIKE 'assess\\_%' OR proname = 'get_session_material'))"
)
VERSIONS = "SELECT version_num FROM alembic_version ORDER BY 1"


@pytest.fixture(scope="module")
def server() -> Iterator[Any]:
    """One bare server: its databases are made per test (roles are cluster-level, so the
    assess-off count runs before anything creates them)."""
    docker = pytest.importorskip("docker", reason="docker SDK not installed")
    try:
        docker.from_env().ping()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Docker not available: {exc}")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16") as c:
        yield c


def _url(container: Any, database: str | None = None) -> str:
    url = make_url(container.get_connection_url().replace("psycopg2", "asyncpg"))
    if database is not None:
        url = url.set(database=database)
    return url.render_as_string(hide_password=False)


async def _sql(url: str, sql: str, *, autocommit: bool = False) -> list[Any]:
    engine = create_async_engine(
        url, isolation_level="AUTOCOMMIT" if autocommit else None
    )
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(sql))
            rows = list(result.fetchall()) if result.returns_rows else []
            await conn.commit()
            return rows
    finally:
        await engine.dispose()


async def _database(container: Any, name: str) -> str:
    await _sql(_url(container), f"CREATE DATABASE {name}", autocommit=True)
    return _url(container, name)


def _run(url: str, *argv: str, assess: str | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "VEKTRA_ASSESS_ENABLED"}
    env["VEKTRA_DATABASE_URL"] = url
    if assess is not None:
        env["VEKTRA_ASSESS_ENABLED"] = assess
    res = subprocess.run(
        ["uv", "run", *argv], capture_output=True, text=True, cwd=ROOT, env=env
    )
    assert res.returncode == 0, res.stdout + res.stderr
    return res.stdout


def _entrypoint(url: str, assess: str | None) -> str:
    """The real entrypoint's ``migrate`` target, with the real alembic (``uv run`` puts it on
    PATH)."""
    return _run(url, "sh", str(ENTRYPOINT), "migrate", assess=assess)


async def _counts(url: str) -> tuple[int, int, int]:
    return tuple((await _sql(url, COUNTS))[0])  # type: ignore[return-value]


async def _dumps(container: Any, url: str) -> tuple[str, str]:
    database = make_url(url).database
    assert database is not None
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            schema = await schema_text(container, conn, container.username, database)
            privileges = await privilege_text(conn)
    finally:
        await engine.dispose()
    return schema, privileges


async def test_a_with_assess_off_the_entrypoint_creates_no_assess_object(
    server: Any,
) -> None:
    url = await _database(server, "assess_off")
    out = _entrypoint(url, assess=None)
    assert "Assess is disabled" in out
    assert [r[0] for r in await _sql(url, VERSIONS)] == ["0009"]
    assert await _counts(url) == (0, 0, 0)
    # "false" is as off as unset
    _entrypoint(url, assess="false")
    assert await _counts(url) == (0, 0, 0)

    # the startup schema step: the platform alone is enough with assess off, and is not
    # with assess on (the remediation names the branch to migrate)
    from vektra_shared.startup import StartupValidationError, check_database_schema

    ini = str(ROOT / "alembic.ini")
    await check_database_schema(url, ini, branches=("platform",))
    with pytest.raises(StartupValidationError) as exc:
        await check_database_schema(url, ini, branches=("platform", "assess"))
    assert "a0004" in exc.value.detail and "assess@head" in exc.value.remediation
    # a database recorded at a revision of the replaced chain is refused, by name
    await _sql(url, "UPDATE alembic_version SET version_num = '0044'")
    try:
        with pytest.raises(StartupValidationError) as exc:
            await check_database_schema(url, ini, branches=("platform",))
        assert "unknown to this release: ['0044']" in exc.value.detail
    finally:
        await _sql(url, "UPDATE alembic_version SET version_num = '0009'")


async def test_b_with_assess_on_the_entrypoint_migrates_both_branches(
    server: Any,
) -> None:
    url = await _database(server, "assess_on")
    _entrypoint(url, assess="true")
    # Alembic records the assess head only: the base depends on 0009, which is implied
    assert [r[0] for r in await _sql(url, VERSIONS)] == ["a0004"]
    assert await _counts(url) == (TABLES, ROLES, FUNCTIONS)
    # the startup schema step agrees: both branches at head, and a disabled deployment that
    # keeps the assess schema is still accepted
    from vektra_shared.startup import check_database_schema

    ini = str(ROOT / "alembic.ini")
    await check_database_schema(url, ini, branches=("platform", "assess"))
    await check_database_schema(url, ini, branches=("platform",))


async def test_c_the_squash_equals_the_old_chain(server: Any) -> None:
    """Schema and effective privileges, byte for byte, against the goldens of 0044."""
    url = await _database(server, "squash")
    _run(url, "alembic", "upgrade", "0009")
    _run(url, "alembic", "upgrade", "assess@head")
    schema, privileges = await _dumps(server, url)
    assert "CREATE TABLE public.assess_sessions" in schema, "the migrations did not run"
    assert privileges.count("\n") > 500, (
        "no privilege listed: the migrations did not run"
    )
    if REGEN:
        (GOLDEN / "assess_schema_head.sql").write_text(schema)
        (GOLDEN / "assess_privileges_head.txt").write_text(privileges)
    assert schema == (GOLDEN / "assess_schema_head.sql").read_text()
    assert privileges == (GOLDEN / "assess_privileges_head.txt").read_text()


async def test_d_the_downgrade_gives_back_the_platform_schema(server: Any) -> None:
    """``alembic downgrade assess@base`` leaves exactly the platform schema at 0009: the api_keys
    constraint restored, the reserved namespace gone (the trace column is the platform's)."""
    platform = await _database(server, "platform_only")
    _run(platform, "alembic", "upgrade", "platform@head")
    want, _ = await _dumps(server, platform)

    url = await _database(server, "round_trip")
    _run(url, "alembic", "upgrade", "heads")
    _run(url, "alembic", "downgrade", "assess@base")
    assert [r[0] for r in await _sql(url, VERSIONS)] == ["0009"]
    got, _ = await _dumps(server, url)
    assert got == want
    tables, _, functions = await _counts(url)
    assert (tables, functions) == (0, 0)  # the roles are cluster-level and stay
    _run(url, "alembic", "upgrade", "heads")
    assert await _counts(url) == (TABLES, ROLES, FUNCTIONS)


def test_e_the_platform_chain_names_no_assess_object() -> None:
    """Nothing of the assess schema is left on the platform branch."""
    versions = sorted((ROOT / "migrations" / "versions").glob("*.py"))
    assert len(versions) >= 8, "the platform chain was not found"
    for path in versions:
        assert "assess_" not in path.read_text(), path.name
