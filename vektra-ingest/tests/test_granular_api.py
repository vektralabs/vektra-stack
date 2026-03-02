"""Unit tests for granular ingest APIs (EX-010): extract, chunk, embed."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from starlette.middleware.base import BaseHTTPMiddleware

from vektra_shared.auth import ApiKeyInfo
from vektra_shared.registry import ProviderRegistry
from vektra_shared.types import DocumentChunk, ElementType


def _make_app():
    """Build a FastAPI test app with ingest router."""
    from vektra_ingest.api import router
    from vektra_shared.db import get_session

    class RequestIdMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            request.state.request_id = uuid4()
            return await call_next(request)

    app = FastAPI()
    reg = ProviderRegistry()

    # Mock key store
    store = AsyncMock()
    info = ApiKeyInfo(key_id=uuid4(), scopes=["ingest"])
    store.lookup_by_token = AsyncMock(return_value=info)
    reg.register("key_store", "default", store)

    # Mock embedding provider
    mock_embed = AsyncMock()
    mock_embed.embed_documents = AsyncMock(return_value=[[0.1] * 384])
    reg.register("embedding", "default", mock_embed)

    app.state.registry = reg
    app.add_middleware(RequestIdMiddleware)
    app.include_router(router)

    async def _mock_get_session():
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=mock_result)
        session.add = MagicMock()

        async def _refresh(obj):
            if hasattr(obj, "id") and obj.id is None:
                obj.id = uuid4()

        session.refresh = _refresh
        yield session

    app.dependency_overrides[get_session] = _mock_get_session
    return app


async def _fake_extract(req):
    async def _gen():
        yield DocumentChunk(text="extracted text", element_type=ElementType.TEXT)

    return _gen()


async def _fake_chunk(elements):
    async def _gen():
        async for e in elements:
            yield e

    return _gen()


# ---------------------------------------------------------------------------
# POST /api/v1/ingest/extract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_returns_document_chunks():
    """Extract endpoint returns extracted elements as JSON."""
    app = _make_app()

    mock_extractor = MagicMock()
    mock_extractor.extract = _fake_extract

    with patch("vektra_ingest.detection.detect_content_type", return_value="application/pdf"), \
         patch("vektra_ingest.pipeline._get_extractor", return_value=mock_extractor):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.post(
                "/api/v1/ingest/extract",
                files={"file": ("test.pdf", b"%PDF-1.4 data")},
                headers={"Authorization": "Bearer token"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["text"] == "extracted text"
    assert body[0]["element_type"] == "text"


@pytest.mark.asyncio
async def test_extract_unsupported_type_returns_422():
    """Extract endpoint rejects unsupported content type."""
    app = _make_app()

    with patch("vektra_ingest.detection.detect_content_type", return_value="image/jpeg"), \
         patch("vektra_ingest.pipeline._get_extractor", return_value=None):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.post(
                "/api/v1/ingest/extract",
                files={"file": ("test.jpg", b"fake image")},
                headers={"Authorization": "Bearer token"},
            )

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/v1/ingest/chunk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chunk_returns_chunked_elements():
    """Chunk endpoint returns chunked elements as JSON."""
    app = _make_app()

    mock_extractor = MagicMock()
    mock_extractor.extract = _fake_extract

    with patch("vektra_ingest.detection.detect_content_type", return_value="application/pdf"), \
         patch("vektra_ingest.pipeline._get_extractor", return_value=mock_extractor), \
         patch("vektra_ingest.chunking.FixedSizeChunking") as mock_chunker_cls:
        mc = MagicMock()
        mc.chunk = _fake_chunk
        mock_chunker_cls.return_value = mc

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.post(
                "/api/v1/ingest/chunk",
                files={"file": ("test.pdf", b"%PDF-1.4 data")},
                headers={"Authorization": "Bearer token"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["text"] == "extracted text"


# ---------------------------------------------------------------------------
# POST /api/v1/ingest/embed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_returns_embeddings():
    """Embed endpoint returns embeddings as JSON."""
    app = _make_app()

    mock_extractor = MagicMock()
    mock_extractor.extract = _fake_extract

    with patch("vektra_ingest.detection.detect_content_type", return_value="application/pdf"), \
         patch("vektra_ingest.pipeline._get_extractor", return_value=mock_extractor), \
         patch("vektra_ingest.chunking.FixedSizeChunking") as mock_chunker_cls:
        mc = MagicMock()
        mc.chunk = _fake_chunk
        mock_chunker_cls.return_value = mc

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.post(
                "/api/v1/ingest/embed",
                files={"file": ("test.pdf", b"%PDF-1.4 data")},
                headers={"Authorization": "Bearer token"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["text"] == "extracted text"
    assert "dense" in body[0]
    assert len(body[0]["dense"]) == 384
