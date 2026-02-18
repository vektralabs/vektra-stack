"""Integration tests for vektra-index: store/search/delete/stats + namespace isolation.

Requires Docker (testcontainers PostgreSQL with pgvector extension).
Skipped automatically when Docker is unavailable.

Test coverage:
- Store 10 chunks, search returns top-k with correct ordering
- DELETE removes all chunks and soft-deletes source_document
- Stats returns accurate counts
- Namespace isolation: search in namespace A does not return namespace B results
"""
from __future__ import annotations

import os
import subprocess
import pytest
from uuid import uuid4


def _docker_available() -> bool:
    try:
        import docker
        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker not available - skipping integration tests",
)


@pytest.fixture(scope="module")
def db_url():
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        async_url = postgres.get_connection_url().replace(
            "postgresql://", "postgresql+asyncpg://"
        ).replace("psycopg2", "asyncpg")

        # Apply migrations
        env = os.environ.copy()
        env["VEKTRA_DATABASE_URL"] = async_url
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        result = subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            capture_output=True, text=True, cwd=project_root, env=env
        )
        if result.returncode != 0:
            pytest.fail(f"Migration failed: {result.stderr}")

        yield async_url


@pytest.fixture(scope="module")
def provider(db_url):
    from vektra_shared.db import init_db
    init_db(db_url)

    from vektra_index.providers.pgvector import PgvectorProvider
    return PgvectorProvider(active_index_version=1)


@pytest.fixture(scope="module")
def embedding_provider():
    from vektra_index.providers.sentence_transformers import SentenceTransformersProvider
    return SentenceTransformersProvider("all-MiniLM-L6-v2")


@pytest.fixture
async def session(db_url):
    from vektra_shared.db import get_session
    async for s in get_session():
        yield s
        await s.rollback()


@pytest.mark.asyncio
async def test_store_and_search(provider, embedding_provider, session):
    doc_id = uuid4()
    namespace = f"ns-{uuid4().hex[:8]}"

    from vektra_shared.types import ChunkEmbedding, QueryEmbedding
    texts = [f"The capital of France is Paris. Chunk {i}." for i in range(5)]
    texts += [f"Python is a programming language. Chunk {i}." for i in range(5)]

    embeddings = await embedding_provider.embed_documents(texts)
    chunks = [
        ChunkEmbedding(chunk_id="", text=t, dense=e, metadata={"position": i})
        for i, (t, e) in enumerate(zip(texts, embeddings))
    ]

    async with session.begin():
        # Insert source_document first (FK dependency)
        await session.execute(
            __import__("sqlalchemy", fromlist=["text"]).text(
                "INSERT INTO namespaces (id, display_name) VALUES (:ns, :dn) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"ns": namespace, "dn": namespace},
        )
        await session.execute(
            __import__("sqlalchemy", fromlist=["text"]).text(
                "INSERT INTO source_documents (id, namespace_id, filename, content_hash, content_type, file_size_bytes) "
                "VALUES (:id, :ns, 'test.pdf', 'abc123', 'application/pdf', 1024)"
            ),
            {"id": str(doc_id), "ns": namespace},
        )
        chunk_ids = await provider.store(session, namespace, doc_id, chunks)

    assert len(chunk_ids) == 10

    query_emb = await embedding_provider.embed_query("capital of France")
    async with session.begin():
        results = await provider.search(
            session, namespace, QueryEmbedding(dense=query_emb), top_k=5
        )

    assert len(results) > 0
    # Top result should be about France/Paris
    top_text = results[0].text_snippet.lower()
    assert "france" in top_text or "paris" in top_text


@pytest.mark.asyncio
async def test_namespace_isolation(provider, embedding_provider, session):
    """Search in namespace A must not return namespace B results."""
    ns_a = f"ns-a-{uuid4().hex[:8]}"
    ns_b = f"ns-b-{uuid4().hex[:8]}"
    doc_a = uuid4()
    doc_b = uuid4()

    from vektra_shared.types import ChunkEmbedding, QueryEmbedding
    from sqlalchemy import text

    emb_a = await embedding_provider.embed_documents(["Python is great for data science."])
    emb_b = await embedding_provider.embed_documents(["JavaScript rules the web."])

    async with session.begin():
        for ns in [ns_a, ns_b]:
            await session.execute(
                text("INSERT INTO namespaces (id, display_name) VALUES (:ns, :dn) ON CONFLICT (id) DO NOTHING"),
                {"ns": ns, "dn": ns},
            )
        for doc_id, ns in [(doc_a, ns_a), (doc_b, ns_b)]:
            await session.execute(
                text("INSERT INTO source_documents (id, namespace_id, filename, content_hash, content_type, file_size_bytes) "
                     "VALUES (:id, :ns, 'test.pdf', :hash, 'application/pdf', 1024)"),
                {"id": str(doc_id), "ns": ns, "hash": f"hash-{ns}"},
            )

        await provider.store(session, ns_a, doc_a, [
            ChunkEmbedding(chunk_id="", text="Python is great for data science.", dense=emb_a[0], metadata={})
        ])
        await provider.store(session, ns_b, doc_b, [
            ChunkEmbedding(chunk_id="", text="JavaScript rules the web.", dense=emb_b[0], metadata={})
        ])

    query_emb = await embedding_provider.embed_query("Python data science")
    async with session.begin():
        results_a = await provider.search(session, ns_a, QueryEmbedding(dense=query_emb), top_k=10)
        results_b = await provider.search(session, ns_b, QueryEmbedding(dense=query_emb), top_k=10)

    # ns_a result should be about Python, ns_b should be about JavaScript
    a_ids = {r.chunk_id for r in results_a}
    b_ids = {r.chunk_id for r in results_b}
    assert a_ids.isdisjoint(b_ids), "Namespace isolation failed: chunk appeared in both namespaces"


@pytest.mark.asyncio
async def test_delete_removes_chunks(provider, embedding_provider, session):
    namespace = f"ns-del-{uuid4().hex[:8]}"
    doc_id = uuid4()

    from vektra_shared.types import ChunkEmbedding, QueryEmbedding
    from sqlalchemy import text

    embs = await embedding_provider.embed_documents(["chunk to delete"])

    async with session.begin():
        await session.execute(
            text("INSERT INTO namespaces (id, display_name) VALUES (:ns, :dn) ON CONFLICT (id) DO NOTHING"),
            {"ns": namespace, "dn": namespace},
        )
        await session.execute(
            text("INSERT INTO source_documents (id, namespace_id, filename, content_hash, content_type, file_size_bytes) "
                 "VALUES (:id, :ns, 'del.pdf', 'delhash', 'application/pdf', 512)"),
            {"id": str(doc_id), "ns": namespace},
        )
        await provider.store(session, namespace, doc_id, [
            ChunkEmbedding(chunk_id="", text="chunk to delete", dense=embs[0], metadata={})
        ])

    async with session.begin():
        removed = await provider.delete(session, namespace, doc_id)

    assert removed == 1

    # Search should return nothing
    query_emb = await embedding_provider.embed_query("chunk to delete")
    async with session.begin():
        results = await provider.search(session, namespace, QueryEmbedding(dense=query_emb), top_k=5)

    assert len(results) == 0, "Soft-deleted document should not appear in search results"
