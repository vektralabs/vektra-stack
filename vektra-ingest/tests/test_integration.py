"""Integration tests for vektra-ingest: full document ingestion with real PostgreSQL.

Requires Docker (testcontainers PostgreSQL with pgvector extension).
Automatically skipped when Docker is unavailable.

Coverage:
- Full ingest flow with a real PDF → chunks in DB + indexed status
- Duplicate detection: same file twice → second returns status='exists'
- Alias detection: same content, different filename
- Filename conflict → 409

Uses a stub EmbeddingProvider (deterministic random embeddings) instead of
the real sentence-transformers model to keep tests fast.
"""

from __future__ import annotations

import os
import subprocess
from uuid import uuid4

import pytest

from vektra_shared.registry import ProviderRegistry
from vektra_shared.types import HealthStatus

# ---------------------------------------------------------------------------
# Docker availability guard
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(
        not _docker_available(),
        reason="Docker not available - skipping integration tests",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]


# ---------------------------------------------------------------------------
# Stub EmbeddingProvider (deterministic, no ML model)
# ---------------------------------------------------------------------------


class StubEmbeddingProvider:
    """Returns deterministic float32 embeddings for testing."""

    def dimensions(self) -> int:
        return 384

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    async def embed_query(self, query: str) -> list[float]:
        return self._embed(query)

    def _embed(self, text: str) -> list[float]:
        # Deterministic pseudo-embedding based on text hash
        import hashlib

        h = int(hashlib.sha256(text.encode()).hexdigest(), 16)
        dims = self.dimensions()
        emb = [(((h >> (i * 8)) & 0xFF) / 255.0) - 0.5 for i in range(dims)]
        norm = sum(x * x for x in emb) ** 0.5
        return [x / (norm + 1e-9) for x in emb]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(status="healthy")


# ---------------------------------------------------------------------------
# Module-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_url():
    """Start a PostgreSQL+pgvector container and apply migrations."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        raw_url = postgres.get_connection_url()
        async_url = raw_url.replace("postgresql://", "postgresql+asyncpg://").replace(
            "psycopg2", "asyncpg"
        )

        project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        env = os.environ.copy()
        env["VEKTRA_DATABASE_URL"] = async_url

        result = subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
            cwd=project_root,
            env=env,
        )
        if result.returncode != 0:
            pytest.fail(f"Migration failed:\n{result.stderr}\n{result.stdout}")

        yield async_url


@pytest.fixture
async def fresh_engine(db_url):
    """Per-test engine bound to the test's event loop."""
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    import vektra_shared.db as db_mod

    engine = create_async_engine(db_url, pool_size=2, max_overflow=0)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    old_engine = db_mod._engine
    old_factory = db_mod._session_factory
    db_mod._engine = engine
    db_mod._session_factory = factory

    yield factory

    db_mod._engine = old_engine
    db_mod._session_factory = old_factory
    await engine.dispose()


@pytest.fixture
async def session(fresh_engine):
    async with fresh_engine() as s:
        yield s


@pytest.fixture
def registry(fresh_engine):
    """Per-test ProviderRegistry with stub providers."""
    from vektra_index.adapters import VectorStoreServiceAdapter

    reg = ProviderRegistry()
    reg.register("embedding", "default", StubEmbeddingProvider())
    reg.register(
        "vector_store", "default", VectorStoreServiceAdapter(active_index_version=1)
    )
    return reg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_real_pdf() -> bytes:
    """Build a minimal real PDF using pdfplumber-compatible bytes."""
    try:
        from fpdf import FPDF  # type: ignore[import-untyped]

        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        for i in range(10):
            pdf.cell(
                200,
                10,
                text=f"Page content paragraph {i}: Machine learning is fascinating.",
            )
            pdf.ln()
        return bytes(pdf.output())
    except ImportError:
        pass

    # Minimal PDF if fpdf2 not available (text must exceed 100-char scanned threshold)
    text = (
        "Machine learning is transforming artificial intelligence research today. "
        "Deep learning models have revolutionized natural language processing tasks. "
        "Vector embeddings enable semantic search across large document collections."
    )
    safe = text.replace("(", "\\(").replace(")", "\\)")
    body = f"BT /F1 12 Tf 100 700 Td ({safe}) Tj ET"
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<</Type/Catalog/Pages 2 0 R>>\nendobj\n"
        b"2 0 obj\n<</Type/Pages/Kids [3 0 R]/Count 1>>\nendobj\n"
        b"3 0 obj\n<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>\nendobj\n"
        + f"4 0 obj\n<</Length {len(body)}>>\nstream\n{body}\nendstream\nendobj\n".encode()
        + b"5 0 obj\n<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>\nendobj\n"
        b"xref\n0 6\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000115 00000 n \n"
        b"0000000266 00000 n \n"
        b"0000000400 00000 n \n"
        b"trailer\n<</Size 6/Root 1 0 R>>\n"
        b"startxref\n460\n%%EOF\n"
    )


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


async def test_full_ingest_returns_indexed(session, registry):
    """Full PDF ingest stores chunks and returns status='indexed'."""
    from sqlalchemy import text

    from vektra_ingest.pipeline import run_ingest

    namespace = f"ns-{uuid4().hex[:8]}"

    # Seed namespace
    await session.execute(
        text(
            "INSERT INTO namespaces (id, display_name) VALUES (:ns, :dn) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"ns": namespace, "dn": namespace},
    )
    await session.commit()

    pdf_bytes = _make_real_pdf()

    result = await run_ingest(
        file_content=pdf_bytes,
        filename="lecture.pdf",
        namespace=namespace,
        session=session,
        registry=registry,
    )

    assert result.status == "indexed"
    assert result.document_id is not None
    assert result.chunk_count is not None
    assert result.chunk_count >= 1

    # Verify chunks in database
    rows = await session.execute(
        text("SELECT COUNT(*) FROM document_chunks WHERE namespace_id = :ns"),
        {"ns": namespace},
    )
    count = rows.scalar()
    assert count == result.chunk_count


async def test_duplicate_detection_exact_match(session, registry):
    """Uploading the same file twice returns status='exists' on second upload."""
    from sqlalchemy import text

    from vektra_ingest.pipeline import run_ingest

    namespace = f"ns-{uuid4().hex[:8]}"
    await session.execute(
        text(
            "INSERT INTO namespaces (id, display_name) VALUES (:ns, :dn) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"ns": namespace, "dn": namespace},
    )
    await session.commit()

    pdf_bytes = _make_real_pdf()

    # First ingest
    result1 = await run_ingest(
        file_content=pdf_bytes,
        filename="doc.pdf",
        namespace=namespace,
        session=session,
        registry=registry,
    )
    assert result1.status == "indexed"

    # Second ingest: same content + same filename → exists
    result2 = await run_ingest(
        file_content=pdf_bytes,
        filename="doc.pdf",
        namespace=namespace,
        session=session,
        registry=registry,
    )
    assert result2.status == "exists"
    assert result2.document_id == result1.document_id


async def test_alias_same_content_different_filename(session, registry):
    """Same content, different filename → alias added, status='alias'."""
    from sqlalchemy import text

    from vektra_ingest.pipeline import run_ingest

    namespace = f"ns-{uuid4().hex[:8]}"
    await session.execute(
        text(
            "INSERT INTO namespaces (id, display_name) VALUES (:ns, :dn) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"ns": namespace, "dn": namespace},
    )
    await session.commit()

    pdf_bytes = _make_real_pdf()

    result1 = await run_ingest(
        file_content=pdf_bytes,
        filename="original.pdf",
        namespace=namespace,
        session=session,
        registry=registry,
    )
    assert result1.status == "indexed"

    result2 = await run_ingest(
        file_content=pdf_bytes,
        filename="copy.pdf",  # different filename, same bytes
        namespace=namespace,
        session=session,
        registry=registry,
    )
    assert result2.status == "alias"
    assert result2.document_id == result1.document_id
    assert result2.alias_count == 1


async def test_filename_reingest_creates_new_version(session, registry):
    """Different content + same filename → version increment (Phase 2)."""
    from sqlalchemy import text

    from vektra_ingest.pipeline import run_ingest

    namespace = f"ns-{uuid4().hex[:8]}"
    await session.execute(
        text(
            "INSERT INTO namespaces (id, display_name) VALUES (:ns, :dn) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"ns": namespace, "dn": namespace},
    )
    await session.commit()

    pdf1 = _make_real_pdf()
    pdf2 = pdf1 + b"  extra bytes making it different"

    # Ingest first document
    result1 = await run_ingest(
        file_content=pdf1,
        filename="report.pdf",
        namespace=namespace,
        session=session,
        registry=registry,
    )
    assert result1.status == "indexed"
    assert result1.version == 1

    # Re-ingest with different content → creates version 2
    result2 = await run_ingest(
        file_content=pdf2,
        filename="report.pdf",  # same name, different content
        namespace=namespace,
        session=session,
        registry=registry,
    )
    assert result2.status == "indexed"
    assert result2.version == 2
    assert result2.supersedes_id == result1.document_id
    assert result2.document_id != result1.document_id
