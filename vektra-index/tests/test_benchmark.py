"""NFR-002 benchmark: p95 search latency < 500ms with 10k chunks indexed.

Requires Docker (testcontainers PostgreSQL with pgvector).
Skipped automatically when Docker is unavailable.

Uses random 384-dim embeddings (not real model) - we're benchmarking
DB/index performance, not embedding quality.

Run with -s to see intermediate timing output.
"""

from __future__ import annotations

import os
import random
import statistics
import subprocess
import time
from uuid import uuid4

import pytest


def _docker_available() -> bool:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _docker_available(),
        reason="Docker not available - skipping benchmark",
    ),
]

CHUNK_COUNT = 10_000
QUERY_COUNT = 100
EMBEDDING_DIM = 384
P95_THRESHOLD_MS = 500


def _random_vector() -> list[float]:
    """Generate a random unit vector (384-dim, cosine distance makes sense)."""
    v = [random.gauss(0, 1) for _ in range(EMBEDDING_DIM)]
    norm = sum(x * x for x in v) ** 0.5
    return [x / norm for x in v]


@pytest.fixture(scope="module")
def bench_db_url():
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        async_url = (
            postgres.get_connection_url()
            .replace("postgresql://", "postgresql+asyncpg://")
            .replace("psycopg2", "asyncpg")
        )

        env = os.environ.copy()
        env["VEKTRA_DATABASE_URL"] = async_url
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        result = subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
            cwd=project_root,
            env=env,
        )
        if result.returncode != 0:
            pytest.fail(f"Migration failed: {result.stderr}")

        yield async_url


@pytest.fixture(scope="module")
def bench_provider():
    from vektra_index.providers.pgvector import PgvectorProvider

    return PgvectorProvider(active_index_version=1)


@pytest.fixture
async def bench_session(bench_db_url):
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    engine = create_async_engine(bench_db_url, pool_size=4, max_overflow=0)
    try:
        factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as s:
            yield s
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_nfr002_p95_search_under_500ms(
    bench_db_url, bench_provider, bench_session
):
    """NFR-002: p95 search latency < 500ms with 10k chunks in the index."""
    from sqlalchemy import text

    from vektra_shared.types import ChunkEmbedding, QueryEmbedding

    namespace = f"bench-{uuid4().hex[:8]}"
    doc_id = uuid4()

    # Set up namespace + source document
    async with bench_session.begin():
        await bench_session.execute(
            text("INSERT INTO namespaces (id, display_name) VALUES (:ns, :dn)"),
            {"ns": namespace, "dn": "benchmark namespace"},
        )
        await bench_session.execute(
            text(
                "INSERT INTO source_documents "
                "(id, namespace_id, filename, content_hash, content_type, file_size_bytes) "
                "VALUES (:id, :ns, 'bench.pdf', 'benchhash', 'application/pdf', 0)"
            ),
            {"id": str(doc_id), "ns": namespace},
        )

    # Insert chunks in batches of 500 to avoid memory pressure
    print(f"\nInserting {CHUNK_COUNT} chunks in batches of 500...")
    batch_size = 500
    inserted = 0
    while inserted < CHUNK_COUNT:
        count = min(batch_size, CHUNK_COUNT - inserted)
        chunks = [
            ChunkEmbedding(
                chunk_id="",
                text=f"Benchmark chunk {inserted + i}. Some representative text content.",
                dense=_random_vector(),
                metadata={"position": inserted + i},
            )
            for i in range(count)
        ]

        # Create a fresh engine/session for each batch (avoids memory buildup)
        from sqlalchemy.ext.asyncio import (
            AsyncSession,
            async_sessionmaker,
            create_async_engine,
        )

        batch_engine = create_async_engine(bench_db_url, pool_size=1)
        batch_factory = async_sessionmaker(
            batch_engine, class_=AsyncSession, expire_on_commit=False
        )
        async with batch_factory() as batch_sess:
            async with batch_sess.begin():
                await bench_provider.store(batch_sess, namespace, doc_id, chunks)
        await batch_engine.dispose()

        inserted += count

    print(f"Inserted {CHUNK_COUNT} chunks. Running {QUERY_COUNT} search queries...")

    # Warm up: one query before timing (JIT, planner caching)
    query_vec = QueryEmbedding(dense=_random_vector())
    async with bench_session.begin():
        await bench_provider.search(bench_session, namespace, query_vec, top_k=10)

    # Timed queries: measure individual latencies
    latencies_ms: list[float] = []
    for i in range(QUERY_COUNT):
        q = QueryEmbedding(dense=_random_vector())
        t0 = time.monotonic()
        async with bench_session.begin():
            results = await bench_provider.search(bench_session, namespace, q, top_k=10)
        elapsed_ms = (time.monotonic() - t0) * 1000
        latencies_ms.append(elapsed_ms)
        assert len(results) > 0, f"Query {i} returned no results"

    # Compute p95
    latencies_ms.sort()
    p50 = statistics.median(latencies_ms)
    p95 = latencies_ms[int(0.95 * QUERY_COUNT) - 1]
    p99 = latencies_ms[int(0.99 * QUERY_COUNT) - 1]

    print(
        f"\nSearch latency over {QUERY_COUNT} queries with {CHUNK_COUNT} chunks:\n"
        f"  p50 = {p50:.1f}ms\n"
        f"  p95 = {p95:.1f}ms  (threshold: {P95_THRESHOLD_MS}ms)\n"
        f"  p99 = {p99:.1f}ms\n"
        f"  max = {max(latencies_ms):.1f}ms"
    )

    assert p95 < P95_THRESHOLD_MS, (
        f"NFR-002 VIOLATED: p95 search latency = {p95:.1f}ms, "
        f"threshold = {P95_THRESHOLD_MS}ms. "
        f"(p50={p50:.1f}ms, p99={p99:.1f}ms)"
    )
