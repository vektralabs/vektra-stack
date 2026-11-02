"""Migration 0009: assess scopes, scope exclusivity, NOLOGIN roles (plan assess-foundations 4.3).

Runs against a real Postgres (testcontainers). Marked ``integration`` so the
package-integration job (``pytest vektra-*/tests -m integration``) runs it; without
Docker it is reported as SKIPPED, never as passed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
ASSESS = [
    "assess:mint",
    "assess:teacher",
    "assess:privacy-export",
    "assess:privacy-erase",
]


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
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Docker not available: {exc}")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16") as c:
        sync_url = c.get_connection_url()
        async_url = sync_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
        res = _alembic(async_url, "upgrade", "head")
        assert res.returncode == 0, res.stderr
        yield async_url


async def _fetch(url: str, sql: str, params: dict | None = None):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params or {})
            return result.fetchall() if result.returns_rows else []
    finally:
        await engine.dispose()


async def _insert_key(url: str, scopes: list[str], tag: str) -> None:
    await _fetch(
        url,
        "INSERT INTO api_keys (key_hash, key_preview, scopes) VALUES (:h, 'prev', :s)",
        {"h": f"hash-{tag}", "s": scopes},
    )


@pytest.mark.parametrize("scope", ASSESS)
async def test_each_assess_scope_accepted_alone(pg, scope):
    await _insert_key(pg, [scope], scope)


async def test_all_assess_scopes_together_accepted(pg):
    await _insert_key(pg, ASSESS, "all-assess")


@pytest.mark.parametrize("legacy", ["admin", "ingest", "query"])
async def test_mixed_assess_and_legacy_rejected_by_name(pg, legacy):
    with pytest.raises(Exception, match="ck_api_keys_assess_scope_exclusive"):
        await _insert_key(pg, [legacy, "assess:mint"], f"mixed-{legacy}")


async def test_unknown_scope_still_rejected(pg):
    with pytest.raises(Exception, match="ck_api_keys_scopes"):
        await _insert_key(pg, ["assess:anything"], "unknown")


async def test_legacy_scopes_unchanged(pg):
    await _insert_key(pg, ["admin", "ingest", "query"], "legacy")


async def test_roles_exist_with_attributes(pg):
    rows = await _fetch(
        pg,
        "SELECT rolname, rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, "
        "rolbypassrls, rolinherit FROM pg_roles "
        "WHERE rolname IN ('vektra_assess_api', 'vektra_assess_worker') ORDER BY rolname",
    )
    assert [r[0] for r in rows] == ["vektra_assess_api", "vektra_assess_worker"]
    for r in rows:
        assert tuple(r[1:]) == (False, False, False, False, False, False), r


async def test_upgrade_is_idempotent_over_existing_roles(pg):
    # Roles pre-created (init.sql path): a re-run from 0008 must not fail on them.
    await _fetch(pg, "DELETE FROM api_keys WHERE scopes && :a", {"a": ASSESS})
    down = _alembic(pg, "downgrade", "0008")
    assert down.returncode == 0, down.stderr
    up = _alembic(pg, "upgrade", "head")
    assert up.returncode == 0, up.stderr


async def test_downgrade_refused_while_assess_keys_exist(pg):
    await _insert_key(pg, ["assess:teacher"], "blocker")
    before = (await _fetch(pg, "SELECT version_num FROM alembic_version"))[0][0]
    res = _alembic(pg, "downgrade", "0008")
    assert res.returncode != 0
    assert "downgrade refused" in res.stderr and "1 API key" in res.stderr
    # the refusal rolls the whole (transactional) downgrade back, later revisions included
    rows = await _fetch(pg, "SELECT version_num FROM alembic_version")
    assert rows[0][0] == before
    rows = await _fetch(
        pg, "SELECT count(*) FROM api_keys WHERE key_hash = 'hash-blocker'"
    )
    assert rows[0][0] == 1
