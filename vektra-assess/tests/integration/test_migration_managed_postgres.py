"""Migrating as a managed-Postgres user: no superuser, no CREATEROLE (the assess base revision).

The CHANGELOG upgrade note and the assess base revision's error message tell an operator on
managed Postgres what the user Vektra migrates as needs. This proves that guidance on a real
server instead of trusting it: a migration user that is neither superuser nor CREATEROLE, owning
its database, with the extensions and the three assess roles created beforehand by an
administrator, migrates both branches to head once it is granted membership of
``vektra_assess_definer`` (so it can ``SET ROLE`` to it, which ``ALTER FUNCTION ... OWNER TO``
needs). The two controls show each requirement is real: without the roles the assess branch stops
with the message naming all three, and without the membership it fails.

With assess disabled none of it applies (ADR-0033 D7): the same user migrates the platform branch
with no role and no membership, and the database gets no assess object.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
ROLES = ("vektra_assess_api", "vektra_assess_worker", "vektra_assess_definer")
ATTRS = "NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS NOINHERIT"
PASSWORD = "managed-migrator-password-0123"


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
def server() -> Iterator[str]:
    """A bare server; returns the superuser's sync URL (the administrator's connection)."""
    docker = pytest.importorskip("docker", reason="docker SDK not installed")
    try:
        docker.from_env().ping()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Docker not available: {exc}")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16") as c:
        yield c.get_connection_url().replace("psycopg2", "asyncpg")


async def _admin(url: str, *statements: str, database: str | None = None) -> list[Any]:
    """Run statements as the administrator (autocommit); return the last one's rows."""
    target = make_url(url)
    if database is not None:
        target = target.set(database=database)
    engine = create_async_engine(target, isolation_level="AUTOCOMMIT")
    rows: list[Any] = []
    try:
        async with engine.connect() as conn:
            for sql in statements:
                result = await conn.execute(text(sql))
                rows = list(result.fetchall()) if result.returns_rows else []
    finally:
        await engine.dispose()
    return rows


async def _managed_database(
    server: str, *, roles: bool, membership: bool, createrole: bool = False
) -> tuple[str, str]:
    """A database owned by a fresh non-superuser migration user (CREATEROLE only if asked).

    Returns (the migration user's async URL, the database name). The roles are cluster-level,
    so they are created once and reused; ``roles=False`` is only meaningful on a fresh server,
    which is why that control runs first.
    """
    suffix = uuid.uuid4().hex[:8]
    user, db = f"migrator_{suffix}", f"managed_{suffix}"
    await _admin(
        server,
        f"CREATE ROLE {user} LOGIN PASSWORD '{PASSWORD}' "
        f"NOSUPERUSER {'CREATEROLE' if createrole else 'NOCREATEROLE'} NOCREATEDB NOBYPASSRLS",
        f"CREATE DATABASE {db} OWNER {user}",
    )
    # what a managed service's administrator does for the application: the extensions
    await _admin(
        server,
        "CREATE EXTENSION IF NOT EXISTS vector",
        "CREATE EXTENSION IF NOT EXISTS pgcrypto",
        database=db,
    )
    if roles:
        for role in ROLES:
            await _admin(
                server,
                f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') "
                f"THEN CREATE ROLE {role} {ATTRS}; END IF; END $$",
            )
    if membership:
        await _admin(server, f"GRANT vektra_assess_definer TO {user}")
    url = (
        make_url(server)
        .set(
            drivername="postgresql+asyncpg",
            username=user,
            password=PASSWORD,
            database=db,
        )
        .render_as_string(hide_password=False)
    )
    return url, db


async def _flags(server: str, user: str) -> tuple[bool, bool]:
    rows = await _admin(
        server, f"SELECT rolsuper, rolcreaterole FROM pg_roles WHERE rolname = '{user}'"
    )
    return bool(rows[0][0]), bool(rows[0][1])


async def test_a_0_with_assess_off_the_platform_migrates_without_createrole(
    server: str,
) -> None:
    """The review's managed-Postgres case: the platform branch alone needs no CREATEROLE and no
    assess role, and creates none of the assess objects (counted)."""
    url, db = await _managed_database(server, roles=False, membership=False)
    user = make_url(url).username
    assert user is not None
    assert await _flags(server, user) == (False, False)
    res = _alembic(url, "upgrade", "platform@head")
    assert res.returncode == 0, res.stderr
    counts = await _admin(
        server,
        "SELECT (SELECT count(*) FROM pg_tables WHERE schemaname = 'public' "
        "        AND tablename LIKE 'assess\\_%'), "
        "(SELECT count(*) FROM pg_roles WHERE rolname LIKE 'vektra\\_assess\\_%'), "
        "(SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace "
        "        AND (proname LIKE 'assess\\_%' OR proname = 'get_session_material'))",
        database=db,
    )
    assert tuple(counts[0]) == (0, 0, 0)


async def test_a_1_without_the_roles_the_upgrade_stops_naming_all_three(
    server: str,
) -> None:
    url, _ = await _managed_database(server, roles=False, membership=False)
    res = _alembic(url, "upgrade", "heads")
    assert res.returncode != 0
    assert "assess base revision a0001: cannot create role" in res.stderr
    for role in ROLES:
        assert role in res.stderr, f"the message does not name {role}"
    assert "SET ROLE vektra_assess_definer" in res.stderr


async def test_a_2_a_createrole_user_without_pre_created_roles_reaches_head(
    server: str,
) -> None:
    """PG16: a CREATEROLE non-superuser that creates ``vektra_assess_definer`` gets only ADMIN
    on it, not SET, so ``ALTER FUNCTION ... OWNER TO`` would fail. The revision grants the
    role it just created to the migration user, and the upgrade reaches both heads. Runs while
    the roles do not exist yet (after the two tests that need a server without them)."""
    rows = await _admin(
        server, "SELECT count(*) FROM pg_roles WHERE rolname LIKE 'vektra\\_assess\\_%'"
    )
    assert rows[0][0] == 0, "the precondition: no assess role on the server yet"
    url, db = await _managed_database(
        server, roles=False, membership=False, createrole=True
    )
    user = make_url(url).username
    assert user is not None
    assert await _flags(server, user) == (False, True)
    res = _alembic(url, "upgrade", "heads")
    assert res.returncode == 0, res.stderr[-2000:]
    owners = await _admin(
        server,
        "SELECT DISTINCT pg_get_userbyid(proowner) FROM pg_proc "
        "WHERE prosecdef AND pronamespace = 'public'::regnamespace",
        database=db,
    )
    assert [r[0] for r in owners] == ["vektra_assess_definer"]


async def test_b_without_the_definer_membership_the_upgrade_stops_with_the_recipe(
    server: str,
) -> None:
    """Pre-created roles but no membership: the revision checks up front and stops with the
    recipe, not with the raw ``ALTER FUNCTION`` error."""
    url, _ = await _managed_database(server, roles=True, membership=False)
    res = _alembic(url, "upgrade", "heads")
    assert res.returncode != 0
    assert "cannot SET ROLE vektra_assess_definer" in res.stderr, res.stderr[-1500:]
    assert "GRANT vektra_assess_definer TO" in res.stderr
    assert "must be able to SET ROLE" not in res.stderr  # the raw error never surfaces


async def test_c_pre_created_roles_and_the_membership_are_enough_for_head(
    server: str,
) -> None:
    url, db = await _managed_database(server, roles=True, membership=True)
    user = make_url(url).username
    assert user is not None
    # the precondition is real: neither superuser nor CREATEROLE
    assert await _flags(server, user) == (False, False)
    res = _alembic(url, "upgrade", "heads")
    assert res.returncode == 0, res.stderr
    heads = _alembic(url, "heads")
    current = _alembic(url, "current")
    assert heads.returncode == 0 and current.returncode == 0, current.stderr
    want = sorted(line.split()[0] for line in heads.stdout.splitlines() if line.strip())
    assert want == ["0009", "a0002"], heads.stdout
    # the assess head is recorded; the base and the 0009 it depends on are implied
    assert "a0002" in current.stdout, current.stdout
    # the definer functions really are owned by the definer
    owners = await _admin(
        server,
        "SELECT DISTINCT pg_get_userbyid(proowner) FROM pg_proc "
        "WHERE prosecdef AND pronamespace = 'public'::regnamespace",
        database=db,
    )
    assert [r[0] for r in owners] == ["vektra_assess_definer"]
    # RLS for the API role (a0002) is enabled and NOT forced: the migration user, a plain
    # owner here (no superuser, no BYPASSRLS), still reads every site's rows unbound, so a
    # later data migration sees what is there. FORCE would leave it 0 rows (no owner policy).
    await _admin(
        url,
        "INSERT INTO assess_sites (id, lms_site_uuid, base_url) VALUES "
        "('00000000-0000-4000-8000-00000000000a', 'site-a', 'https://a.example'), "
        "('00000000-0000-4000-8000-00000000000b', 'site-b', 'https://b.example')",
        "INSERT INTO assess_start_attempts (site_id, cmid, student_userid, outcome) "
        "SELECT id, 1, 1, 'requested' FROM assess_sites",
    )
    seen = await _admin(
        url, "SELECT count(DISTINCT site_id) FROM assess_start_attempts"
    )
    assert seen[0][0] == 2
    forced = await _admin(
        url,
        "SELECT count(*) FILTER (WHERE relrowsecurity), "
        "count(*) FILTER (WHERE relforcerowsecurity) FROM pg_class "
        "WHERE relnamespace = 'public'::regnamespace AND relname LIKE 'assess\\_%'",
    )
    assert tuple(forced[0]) == (20, 0)
