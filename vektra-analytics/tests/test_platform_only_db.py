"""Analytics on a database migrated with the platform branch only (ADR-0033 D7, review #206).

With ``VEKTRA_ASSESS_ENABLED=false`` the entrypoint runs ``alembic upgrade platform@head`` and
nothing of the assess branch. Every column analytics maps and filters on (``query_traces.pipeline``
included) must therefore come from the platform branch: a trace is stored, read back, listed and
counted in the metrics on such a database. Real Postgres (testcontainers), ``integration``.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from vektra_analytics.service import AnalyticsService
from vektra_shared.types import ChunkRef, QueryTrace, StepTrace

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def platform_db() -> Iterator[str]:
    docker = pytest.importorskip("docker", reason="docker SDK not installed")
    try:
        docker.from_env().ping()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Docker not available: {exc}")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16") as c:
        url = c.get_connection_url().replace("psycopg2", "asyncpg")
        res = subprocess.run(
            ["uv", "run", "alembic", "upgrade", "platform@head"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env={**os.environ, "VEKTRA_DATABASE_URL": url},
        )
        assert res.returncode == 0, res.stderr
        yield url


async def test_store_read_list_and_metrics_without_the_assess_branch(
    platform_db: str,
) -> None:
    engine = create_async_engine(platform_db)
    try:
        async with engine.connect() as conn:
            tables = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' "
                        "AND tablename LIKE 'assess\\_%'"
                    )
                )
            ).scalar()
        assert tables == 0, "the fixture is not a platform-only database"

        svc = AnalyticsService()
        rid = uuid.uuid4()
        trace = QueryTrace(
            response_id=rid,
            steps=[StepTrace(name="retrieve", duration_ms=12, metadata={})],
            total_duration_ms=40,
            chunks_retrieved=[ChunkRef(chunk_id="c-1", score=0.8)],
            llm_model="m",
            prompt_version="abcd1234",
            created_at=datetime.now(UTC),
        )
        async with AsyncSession(engine) as session:
            await svc.store_trace(session, trace, "default")
            await session.commit()
        async with AsyncSession(engine) as session:
            got = await svc.get_trace(session, rid)
            assert got is not None and got.response_id == rid and got.pipeline is None
            listed = await svc.list_traces(session, namespace="default")
            assert [t.response_id for t in listed] == [rid]
            metrics = await svc.get_metrics(session, namespace="default")
            assert metrics.total_queries == 1
    finally:
        await engine.dispose()
