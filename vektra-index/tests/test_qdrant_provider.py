"""Unit tests for QdrantVectorStoreProvider using mocked AsyncQdrantClient.

qdrant-client is not installed in the test environment, so we bypass
the import guard and use mock objects for the qdrant_client module.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from vektra_shared.types import (
    ChunkEmbedding,
    QueryEmbedding,
    SearchMode,
    SparseVector,
)

# --- Mock qdrant_client module before importing the provider ---

_mock_qdrant_models = MagicMock()
_mock_qdrant = ModuleType("qdrant_client")
_mock_qdrant.models = _mock_qdrant_models  # type: ignore[attr-defined]
_mock_qdrant.AsyncQdrantClient = MagicMock  # type: ignore[attr-defined]

# Install the mock module in sys.modules so `from qdrant_client import models` works
sys.modules["qdrant_client"] = _mock_qdrant
sys.modules["qdrant_client.models"] = _mock_qdrant_models


def _make_provider(**kwargs):
    """Create a QdrantVectorStoreProvider with a mock client."""
    from vektra_index.providers.qdrant import QdrantVectorStoreProvider

    mock_client = AsyncMock()
    provider = QdrantVectorStoreProvider(_client=mock_client, **kwargs)
    return provider, mock_client


def _make_scored_point(point_id: str, score: float, payload: dict):
    point = MagicMock()
    point.id = point_id
    point.score = score
    point.payload = payload
    return point


class TestQdrantStore:
    @pytest.mark.asyncio
    async def test_store_upserts_with_wait(self):
        provider, client = _make_provider()
        doc_id = str(uuid4())

        chunks = [
            ChunkEmbedding(
                chunk_id="c1",
                text="hello",
                dense=[0.1] * 384,
                sparse=SparseVector(indices=[0, 5], values=[0.5, 0.8]),
                metadata={"document_id": doc_id},
            ),
        ]

        result = await provider.store("default", chunks)

        assert result == ["c1"]
        client.upsert.assert_called_once()
        call_kwargs = client.upsert.call_args
        assert call_kwargs.kwargs["wait"] is True

    @pytest.mark.asyncio
    async def test_store_empty_returns_empty(self):
        provider, client = _make_provider()
        result = await provider.store("default", [])
        assert result == []
        client.upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_compensating_delete_on_failure(self):
        provider, client = _make_provider()

        client.upsert.side_effect = Exception("connection lost")

        chunks = [
            ChunkEmbedding(
                chunk_id="c1",
                text="hello",
                dense=[0.1] * 384,
                metadata={"document_id": str(uuid4())},
            ),
        ]

        with pytest.raises(Exception, match="connection lost"):
            await provider.store("default", chunks)

        # Compensating delete should have been called
        client.delete.assert_called_once()


class TestQdrantSearch:
    @pytest.mark.asyncio
    async def test_search_dense(self):
        provider, client = _make_provider()
        doc_id = str(uuid4())

        query_result = MagicMock()
        query_result.points = [
            _make_scored_point(
                "p1",
                0.95,
                {
                    "text": "hello world",
                    "document_id": doc_id,
                    "metadata": {},
                    "namespace_id": "default",
                    "index_version": 1,
                },
            ),
        ]
        client.query_points = AsyncMock(return_value=query_result)

        qe = QueryEmbedding(dense=[0.1] * 384)
        results = await provider.search("default", qe, 5, SearchMode.DENSE)

        assert len(results) == 1
        assert results[0].score == 0.95
        assert results[0].text_snippet == "hello world"
        client.query_points.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_sparse(self):
        provider, client = _make_provider()
        doc_id = str(uuid4())

        query_result = MagicMock()
        query_result.points = [
            _make_scored_point(
                "p1",
                0.8,
                {
                    "text": "sparse result",
                    "document_id": doc_id,
                    "metadata": {},
                    "namespace_id": "default",
                    "index_version": 1,
                },
            ),
        ]
        client.query_points = AsyncMock(return_value=query_result)

        qe = QueryEmbedding(
            dense=[0.1] * 384,
            sparse=SparseVector(indices=[1, 5], values=[0.5, 0.8]),
        )
        results = await provider.search("default", qe, 5, SearchMode.SPARSE)

        assert len(results) == 1
        assert results[0].text_snippet == "sparse result"

    @pytest.mark.asyncio
    async def test_search_hybrid_uses_prefetch(self):
        provider, client = _make_provider()
        doc_id = str(uuid4())

        query_result = MagicMock()
        query_result.points = [
            _make_scored_point(
                "p1",
                0.9,
                {
                    "text": "hybrid result",
                    "document_id": doc_id,
                    "metadata": {},
                    "namespace_id": "default",
                    "index_version": 1,
                },
            ),
        ]
        client.query_points = AsyncMock(return_value=query_result)

        qe = QueryEmbedding(
            dense=[0.1] * 384,
            sparse=SparseVector(indices=[1], values=[0.5]),
        )
        results = await provider.search("default", qe, 5, SearchMode.HYBRID)

        assert len(results) == 1
        # Verify prefetch was passed
        call_kwargs = client.query_points.call_args.kwargs
        assert "prefetch" in call_kwargs

    @pytest.mark.asyncio
    async def test_search_sparse_fallback_when_no_sparse_embedding(self):
        provider, client = _make_provider()

        query_result = MagicMock()
        query_result.points = []
        client.query_points = AsyncMock(return_value=query_result)

        qe = QueryEmbedding(dense=[0.1] * 384, sparse=None)
        results = await provider.search("default", qe, 5, SearchMode.SPARSE)

        assert results == []
        # Should have fallen back to dense search
        call_kwargs = client.query_points.call_args.kwargs
        assert call_kwargs.get("using") == "dense"


class TestQdrantDelete:
    @pytest.mark.asyncio
    async def test_delete_by_document_id(self):
        provider, client = _make_provider()

        await provider.delete("default", ["doc-123"])

        client.delete.assert_called_once()
        call_kwargs = client.delete.call_args.kwargs
        assert call_kwargs["wait"] is True


class TestQdrantHealthCheck:
    @pytest.mark.asyncio
    async def test_healthy(self):
        provider, client = _make_provider()

        collections_result = MagicMock()
        collections_result.collections = []
        client.get_collections = AsyncMock(return_value=collections_result)

        status = await provider.health_check()
        assert status.status == "healthy"
        assert status.latency_ms is not None

    @pytest.mark.asyncio
    async def test_unhealthy(self):
        provider, client = _make_provider()

        client.get_collections = AsyncMock(side_effect=Exception("connection refused"))

        status = await provider.health_check()
        assert status.status == "unhealthy"
        assert "connection refused" in status.message


class TestQdrantParentChunks:
    """Parent chunk linkage, search exclusion, and retrieve-by-id (FEAT-017)."""

    @pytest.mark.asyncio
    async def test_store_includes_parent_id_in_payload(self):
        provider, _client = _make_provider()
        doc_id = str(uuid4())

        _mock_qdrant_models.PointStruct.reset_mock()
        chunks = [
            ChunkEmbedding(
                chunk_id="child-1",
                text="child text",
                dense=[0.1] * 384,
                metadata={"document_id": doc_id, "chunk_level": "child"},
                parent_id="parent-1",
            ),
            ChunkEmbedding(
                chunk_id="parent-1",
                text="parent text",
                dense=[0.1] * 384,
                metadata={"document_id": doc_id, "chunk_level": "parent"},
            ),
        ]

        await provider.store("default", chunks)

        payloads = [
            c.kwargs["payload"] for c in _mock_qdrant_models.PointStruct.call_args_list
        ]
        assert payloads[0]["parent_id"] == "parent-1"
        assert payloads[1]["parent_id"] is None

    @pytest.mark.asyncio
    async def test_build_filter_excludes_parent_chunks(self):
        provider, _client = _make_provider()

        _mock_qdrant_models.Filter.reset_mock()
        _mock_qdrant_models.FieldCondition.reset_mock()
        provider._build_filter("default")

        filter_kwargs = _mock_qdrant_models.Filter.call_args.kwargs
        assert "must_not" in filter_kwargs
        assert len(filter_kwargs["must_not"]) == 1
        condition_keys = [
            c.kwargs.get("key")
            for c in _mock_qdrant_models.FieldCondition.call_args_list
        ]
        assert "metadata.chunk_level" in condition_keys

    @pytest.mark.asyncio
    async def test_search_results_carry_parent_id(self):
        provider, client = _make_provider()
        doc_id = str(uuid4())

        query_result = MagicMock()
        query_result.points = [
            _make_scored_point(
                "child-1",
                0.9,
                {
                    "text": "child text",
                    "document_id": doc_id,
                    "metadata": {"chunk_level": "child"},
                    "namespace_id": "default",
                    "parent_id": "parent-1",
                },
            ),
        ]
        client.query_points = AsyncMock(return_value=query_result)

        results = await provider.search(
            "default", QueryEmbedding(dense=[0.1] * 384), 5, SearchMode.DENSE
        )

        assert results[0].parent_id == "parent-1"

    @pytest.mark.asyncio
    async def test_retrieve_filters_by_namespace(self):
        from types import SimpleNamespace

        provider, client = _make_provider()
        doc_id = str(uuid4())
        parent_id = str(uuid4())
        alien_id = str(uuid4())

        # Qdrant retrieve() returns Record objects with no score attribute
        records = [
            SimpleNamespace(
                id=parent_id,
                payload={
                    "text": "parent text",
                    "document_id": doc_id,
                    "metadata": {"chunk_level": "parent"},
                    "namespace_id": "default",
                    "parent_id": None,
                },
            ),
            SimpleNamespace(
                id=alien_id,
                payload={
                    "text": "other tenant",
                    "document_id": doc_id,
                    "metadata": {},
                    "namespace_id": "other",
                    "parent_id": None,
                },
            ),
        ]
        client.retrieve = AsyncMock(return_value=records)

        results = await provider.retrieve("default", [parent_id, alien_id])

        client.retrieve.assert_awaited_once()
        assert [r.chunk_id for r in results] == [parent_id]
        assert results[0].score == 0.0
        assert results[0].text_snippet == "parent text"

    @pytest.mark.asyncio
    async def test_retrieve_skips_invalid_ids(self):
        """Non-UUID ids never reach the Qdrant client (it would reject the batch)."""
        provider, client = _make_provider()
        client.retrieve = AsyncMock(return_value=[])
        valid = str(uuid4())

        await provider.retrieve("default", ["not-a-uuid", valid])

        assert client.retrieve.await_args.kwargs["ids"] == [valid]

    @pytest.mark.asyncio
    async def test_retrieve_empty_ids_short_circuits(self):
        provider, client = _make_provider()

        results = await provider.retrieve("default", [])

        assert results == []
        client.retrieve.assert_not_called()

    @pytest.mark.asyncio
    async def test_retrieve_all_invalid_ids_short_circuits(self):
        provider, client = _make_provider()

        results = await provider.retrieve("default", ["parent-1", "child-2"])

        assert results == []
        client.retrieve.assert_not_called()
