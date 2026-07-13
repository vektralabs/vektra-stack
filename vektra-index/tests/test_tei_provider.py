"""Unit tests for TEIEmbeddingProvider (FEAT-024). All HTTP mocked."""

from __future__ import annotations

import json

import httpx
import pytest

from vektra_index.providers.tei import _MAX_BATCH, TEIEmbeddingProvider


def _embed_handler(dim: int = 4):
    """MockTransport handler: /embed returns one vector per input."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/embed":
            inputs = json.loads(request.content)["inputs"]
            calls.append(len(inputs))
            return httpx.Response(200, json=[[0.1] * dim for _ in inputs])
        return httpx.Response(404)

    return handler, calls


def _make_provider(handler) -> TEIEmbeddingProvider:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://tei.test"
    )
    return TEIEmbeddingProvider(url="http://tei.test", _client=client)


async def test_embed_query_returns_single_vector():
    handler, _ = _embed_handler(dim=4)
    provider = _make_provider(handler)
    vec = await provider.embed_query("hello")
    assert vec == [0.1] * 4
    await provider.aclose()


async def test_embed_documents_chunks_to_batch_limit():
    handler, calls = _embed_handler(dim=4)
    provider = _make_provider(handler)
    texts = [f"doc {i}" for i in range(_MAX_BATCH + 5)]
    vectors = await provider.embed_documents(texts)
    assert len(vectors) == _MAX_BATCH + 5
    assert calls == [_MAX_BATCH, 5]
    await provider.aclose()


async def test_health_check_healthy():
    handler, _ = _embed_handler()
    provider = _make_provider(handler)
    status = await provider.health_check()
    assert status.status == "healthy"
    await provider.aclose()


async def test_health_check_unhealthy_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    provider = _make_provider(handler)
    status = await provider.health_check()
    assert status.status == "unhealthy"
    await provider.aclose()


def test_fetch_dimensions_from_info():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json={"model_id": "m", "embedding_size": 1024})
        return httpx.Response(404)

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://tei.test"
    )
    assert TEIEmbeddingProvider._fetch_dimensions(client) == 1024


def test_fetch_dimensions_probe_fallback_when_info_lacks_size():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json={"model_id": "m"})
        if request.url.path == "/embed":
            return httpx.Response(200, json=[[0.0] * 768])
        return httpx.Response(404)

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://tei.test"
    )
    assert TEIEmbeddingProvider._fetch_dimensions(client) == 768


def test_fetch_dimensions_probe_fallback_when_info_denied():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(401)
        if request.url.path == "/embed":
            return httpx.Response(200, json=[[0.0] * 384])
        return httpx.Response(404)

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://tei.test"
    )
    assert TEIEmbeddingProvider._fetch_dimensions(client) == 384


async def test_auth_header_sent_when_api_key_set():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json=[[0.0] * 4])

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://tei.test",
        headers={"Authorization": "Bearer sekret"},
    )
    provider = TEIEmbeddingProvider(
        url="http://tei.test", api_key="sekret", _client=client
    )
    await provider.embed_query("q")
    assert seen["auth"] == "Bearer sekret"
    await provider.aclose()


async def test_embed_raises_on_server_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    provider = _make_provider(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await provider.embed_documents(["doc"])
    await provider.aclose()
