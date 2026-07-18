"""Integration tests for vektra-core API (REQ-013, REQ-042, REQ-053).

Uses a real FastAPI test client with mocked ProviderRegistry + pipeline.
No real LLM or database required.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from vektra_core.api import router
from vektra_shared.auth import ApiKeyInfo
from vektra_shared.http_errors import register_error_handlers
from vektra_shared.registry import ProviderRegistry
from vektra_shared.types import (
    QueryChunk,
    QueryResponse,
    QueryTrace,
    SourceRef,
    StepTrace,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_registry(api_key: str = "test-key-xxxx") -> ProviderRegistry:
    """Build a ProviderRegistry with a mock key store and mock pipeline."""
    reg = ProviderRegistry()

    # Mock key store
    key_store = MagicMock()
    key_info = ApiKeyInfo(key_id=uuid4(), scopes=["query", "admin"])

    async def _lookup(token: str) -> ApiKeyInfo | None:
        return key_info if token == api_key else None

    key_store.lookup_by_token = _lookup
    reg.register("key_store", "default", key_store)

    # Mock query pipeline
    pipeline = MagicMock()
    response_id = uuid4()

    async def _execute(query_req):
        from vektra_shared.types import ChunkRef

        response = QueryResponse(
            response_id=response_id,
            answer="Test answer.",
            sources=[
                SourceRef(
                    doc_id=uuid4(),
                    chunk_id="chunk-1",
                    score=0.9,
                    snippet="Some relevant snippet.",
                    citation_id=uuid4(),
                    title="lecture-07.pdf, p.3",  # FEAT-021
                )
            ],
            conversation_id=query_req.conversation_id,
        )
        trace = QueryTrace(
            response_id=response_id,
            steps=[StepTrace(name="embed_query", duration_ms=5)],
            total_duration_ms=50,
            chunks_retrieved=[ChunkRef(chunk_id="chunk-1", score=0.9)],
            llm_model="mock",
            prompt_version="deadbeef",
            created_at=datetime.now(UTC),
        )
        return response, trace

    pipeline.execute = AsyncMock(side_effect=_execute)

    async def _execute_stream(query_req):
        async def _gen():
            yield QueryChunk(type="token", data="Hello ")
            yield QueryChunk(type="token", data="world")
            yield QueryChunk(type="sources", data=[])
            yield QueryChunk(type="done", data="")

        return _gen()

    pipeline.execute_stream = AsyncMock(side_effect=_execute_stream)
    reg.register("query_pipeline", "default", pipeline)

    # Mock safeguard
    safeguard = MagicMock()
    from vektra_shared.types import SafeguardResult

    safeguard.pre_query = AsyncMock(return_value=SafeguardResult(allowed=True))
    reg.register("safeguard", "default", safeguard)

    return reg


def _make_app(registry: ProviderRegistry) -> FastAPI:
    app = FastAPI()
    app.state.registry = registry
    app.include_router(router)
    register_error_handlers(app)
    return app


TEST_KEY = "test-key-xxxx"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_query_json_returns_response():
    reg = _make_registry(TEST_KEY)
    app = _make_app(reg)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/query",
            json={"question": "What is RAG?"},
            headers={"Authorization": f"Bearer {TEST_KEY}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "response_id" in body
    assert body["answer"] == "Test answer."
    assert isinstance(body["sources"], list)
    assert len(body["sources"]) == 1
    assert "citation_id" in body["sources"][0]
    # FEAT-021: the title must survive the SourceRef -> SourceRefBody mapping
    assert body["sources"][0]["title"] == "lecture-07.pdf, p.3"


async def test_query_requires_auth():
    reg = _make_registry(TEST_KEY)
    app = _make_app(reg)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/query",
            json={"question": "What is RAG?"},
        )
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "ERR-AUTH-001"


async def test_query_wrong_key_returns_401():
    reg = _make_registry(TEST_KEY)
    app = _make_app(reg)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/query",
            json={"question": "What is RAG?"},
            headers={"Authorization": "Bearer wrong-key-zzzz"},
        )
    assert resp.status_code == 401


async def test_query_sse_returns_stream():
    reg = _make_registry(TEST_KEY)
    app = _make_app(reg)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/query",
            json={"question": "Stream this", "stream": True},
            headers={
                "Authorization": f"Bearer {TEST_KEY}",
                "Accept": "text/event-stream",
            },
        )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    body = resp.text
    assert "data: Hello " in body
    assert "data: world" in body
    assert "data: [DONE]" in body


async def test_providers_returns_list():
    reg = _make_registry(TEST_KEY)

    # Register a mock LLM provider
    llm = MagicMock()
    llm.model_name = "ollama/llama3"
    from vektra_shared.types import HealthStatus

    llm.health_check = AsyncMock(
        return_value=HealthStatus(status="healthy", latency_ms=42)
    )
    reg.register("llm", "default", llm)

    app = _make_app(reg)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/providers",
            headers={"Authorization": f"Bearer {TEST_KEY}"},
        )
    assert resp.status_code == 200
    providers = resp.json()
    assert isinstance(providers, list)
    assert providers[0]["name"] == "default"
    assert providers[0]["model"] == "ollama/llama3"
    assert providers[0]["status"] == "healthy"


async def test_query_no_relevant_context_flag():
    """When pipeline returns no_relevant_context=True, response body reflects it."""
    reg = _make_registry(TEST_KEY)

    # Override pipeline to return no_relevant_context
    pipeline = reg.get("query_pipeline", "default")

    async def _execute_no_context(query_req):
        response = QueryResponse(
            response_id=uuid4(),
            answer=None,
            sources=[],
            conversation_id=None,
            no_relevant_context=True,
        )
        trace = QueryTrace(
            response_id=response.response_id,
            steps=[],
            total_duration_ms=10,
            chunks_retrieved=[],
            llm_model="mock",
            prompt_version="deadbeef",
            created_at=datetime.now(UTC),
        )
        return response, trace

    pipeline.execute = AsyncMock(side_effect=_execute_no_context)

    app = _make_app(reg)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/query",
            json={"question": "Unknown topic"},
            headers={"Authorization": f"Bearer {TEST_KEY}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["no_relevant_context"] is True
    assert body["answer"] is None


# ---------------------------------------------------------------------------
# BUG-026: top_k bounds (ge=1, le=100), mirroring /api/v1/search
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("top_k", [0, -1, 101, 100000])
async def test_query_top_k_out_of_bounds_returns_422(top_k: int) -> None:
    """Out-of-bounds top_k is rejected at validation, not silently accepted.

    top_k<=0 previously answered 200 no_relevant_context; top_k>100 drove an
    unbounded retrieval fetch and cross-encoder pass. Both are now 422.
    """
    reg = _make_registry(TEST_KEY)
    app = _make_app(reg)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/query",
            json={"question": "What is RAG?", "top_k": top_k},
            headers={"Authorization": f"Bearer {TEST_KEY}"},
        )
    assert resp.status_code == 422
    # Consistent with /api/v1/search: a Field-bound violation is FastAPI's
    # standard RequestValidationError shape ({"detail": [...]}), not the
    # REQ-010 envelope (which only wraps HTTPException-raised errors).
    body = resp.json()
    assert isinstance(body["detail"], list)
    assert body["detail"][0]["loc"][-1] == "top_k"


@pytest.mark.parametrize("top_k", [1, 5, 100])
async def test_query_top_k_within_bounds_returns_200(top_k: int) -> None:
    """Valid boundary values pass validation and reach the pipeline."""
    reg = _make_registry(TEST_KEY)
    app = _make_app(reg)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/query",
            json={"question": "What is RAG?", "top_k": top_k},
            headers={"Authorization": f"Bearer {TEST_KEY}"},
        )
    assert resp.status_code == 200
