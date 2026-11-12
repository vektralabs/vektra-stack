"""The job-loop runtime shape of ``assess_jobs`` (plan assess-job-loop A1-A2; formerly 0018).

Real Postgres (testcontainers), ``integration``. The schema is checked by what the catalog
says, not by what the migration claims to have run.
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


async def test_jobs_contract_columns_states_and_dedupe(pg):
    cols = {
        r[0]: (r[1], r[2])
        for r in await _fetch(
            pg,
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name='assess_jobs'",
        )
    }
    for name in (
        "id", "kind", "args", "dedupe_key", "state", "not_before", "lease_until",
        "leased_by", "attempts", "max_attempts", "created_at", "done_at",
        "last_error_code",
    ):  # fmt: skip
        assert name in cols, name
    assert cols["last_error_code"] == ("text", "YES")
    check = (
        await _fetch(
            pg,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname='ck_assess_jobs_state'",
        )
    )[0][0]
    for state in ("ready", "leased", "done", "failed", "dead"):
        assert f"'{state}'" in check


async def test_claim_index_serves_ready_and_expired_leases(pg):
    idx = {
        r[0]: r[1]
        for r in await _fetch(
            pg,
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename='assess_jobs'",
        )
    }
    assert "ix_assess_jobs_claim" not in idx, "superseded by ix_assess_jobs_kind_claim"
    d = idx["ix_assess_jobs_kind_claim"]
    assert "(kind, not_before)" in d
    assert "'ready'" in d and "'leased'" in d


async def test_storage_options_are_applied(pg):
    opts = (
        await _fetch(pg, "SELECT reloptions FROM pg_class WHERE relname='assess_jobs'")
    )[0][0]
    got = dict(o.split("=") for o in opts)
    assert got["fillfactor"] == "70"
    assert got["autovacuum_vacuum_scale_factor"] == "0.01"
    assert got["autovacuum_vacuum_threshold"] == "200"
    assert got["autovacuum_analyze_scale_factor"] == "0.02"
