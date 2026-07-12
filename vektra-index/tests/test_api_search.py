"""Unit tests for /api/v1/search provider resolution (BUG-021).

The endpoint must resolve embedding, sparse embedding, and vector store from
the ProviderRegistry (same contract as the query pipeline), so that Qdrant
deployments search the active store instead of a hardcoded pgvector instance.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from vektra_shared.auth import ApiKeyInfo
from vektra_shared.registry import ProviderRegistry
from vektra_shared.types import SearchMode, SearchResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_key_store(scopes: list[str]) -> AsyncMock:
    """Minimal mock key store that always accepts any token."""
    store = AsyncMock()
    store.lookup_by_token = AsyncMock(
        return_value=ApiKeyInfo(key_id=uuid4(), scopes=scopes)
    )
    return store


def _make_vector_store(results: list[SearchResult]) -> AsyncMock:
    store = AsyncMock()
    store.search = AsyncMock(return_value=results)
    return store


def _make_embedding_provider() -> AsyncMock:
    provider = AsyncMock()
    provider.embed_query = AsyncMock(return_value=[0.1, 0.2, 0.3])
    return provider


def _result(chunk_id: str = "chunk-1", score: float = 0.9) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        score=score,
        text_snippet="Art. 2 diritti inviolabili",
        document_id=uuid4(),
        document_version=1,
        metadata={},
    )


def _make_app(
    vector_store: AsyncMock,
    sparse_provider: AsyncMock | None = None,
) -> FastAPI:
    """Build a FastAPI test app with the index router and a stub registry."""
    from vektra_index.api import router

    app = FastAPI()
    app.state.registry = ProviderRegistry()
    app.state.registry.register("key_store", "default", _make_key_store(["query"]))
    app.state.registry.register("embedding", "default", _make_embedding_provider())
    app.state.registry.register("vector_store", "default", vector_store)
    if sparse_provider is not None:
        app.state.registry.register("sparse_embedding", "default", sparse_provider)
    app.include_router(router)
    return app


async def _post_search(app: FastAPI, body: dict) -> tuple[int, dict]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/search",
            json=body,
            headers={"Authorization": "Bearer test-token"},
        )
    return resp.status_code, resp.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_uses_registry_vector_store() -> None:
    """Results must come from the registry's vector store, not pgvector."""
    vector_store = _make_vector_store([_result()])
    app = _make_app(vector_store)

    status, data = await _post_search(
        app,
        {
            "query": "diritti",
            "namespace": "default",
            "top_k": 5,
            "search_mode": "dense",
        },
    )

    assert status == 200
    assert data["total"] == 1
    assert data["results"][0]["chunk_id"] == "chunk-1"

    vector_store.search.assert_awaited_once()
    kwargs = vector_store.search.await_args.kwargs
    assert kwargs["namespace"] == "default"
    assert kwargs["top_k"] == 5
    assert kwargs["search_mode"] == SearchMode.DENSE
    assert kwargs["query_embedding"].dense == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_search_hybrid_falls_back_to_dense_without_sparse() -> None:
    """HYBRID with no registered sparse provider degrades to DENSE."""
    vector_store = _make_vector_store([])
    app = _make_app(vector_store, sparse_provider=None)

    status, data = await _post_search(
        app,
        {"query": "q", "namespace": "default", "top_k": 3, "search_mode": "hybrid"},
    )

    assert status == 200
    assert data["results"] == []
    kwargs = vector_store.search.await_args.kwargs
    assert kwargs["search_mode"] == SearchMode.DENSE
    assert kwargs["query_embedding"].sparse is None


@pytest.mark.asyncio
async def test_search_hybrid_uses_registered_sparse_provider() -> None:
    """HYBRID with a registered sparse provider keeps hybrid mode and sparse vector."""
    vector_store = _make_vector_store([_result()])
    sparse_vector = MagicMock(name="sparse_vector")
    sparse_provider = AsyncMock()
    sparse_provider.embed_query = AsyncMock(return_value=sparse_vector)
    app = _make_app(vector_store, sparse_provider=sparse_provider)

    status, _ = await _post_search(
        app,
        {"query": "q", "namespace": "default", "top_k": 3, "search_mode": "hybrid"},
    )

    assert status == 200
    sparse_provider.embed_query.assert_awaited_once_with("q")
    kwargs = vector_store.search.await_args.kwargs
    assert kwargs["search_mode"] == SearchMode.HYBRID
    assert kwargs["query_embedding"].sparse is sparse_vector
