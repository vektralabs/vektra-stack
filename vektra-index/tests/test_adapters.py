"""Unit tests for VectorStoreServiceAdapter (vektra_index.adapters)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from vektra_shared.types import ChunkEmbedding, HealthStatus, QueryEmbedding


def _make_chunk(doc_id: str, text: str = "test content") -> ChunkEmbedding:
    return ChunkEmbedding(
        chunk_id="c1",
        text=text,
        dense=[0.1, 0.2, 0.3],
        metadata={"document_id": doc_id},
    )


class _FakeSession:
    """Minimal async context manager for session factory mock."""

    def __init__(self) -> None:
        self.session = AsyncMock()
        self.session.commit = AsyncMock()
        self.session.rollback = AsyncMock()

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *a):
        pass


class TestVectorStoreServiceAdapter:
    def _make_adapter(self):
        from vektra_index.adapters import VectorStoreServiceAdapter

        return VectorStoreServiceAdapter(active_index_version=1)

    async def test_store_empty_chunks_returns_empty(self) -> None:
        adapter = self._make_adapter()
        result = await adapter.store("default", [])
        assert result == []

    async def test_store_missing_document_id_raises(self) -> None:
        adapter = self._make_adapter()
        chunk = ChunkEmbedding(chunk_id="c1", text="x", dense=[0.1], metadata={})
        with pytest.raises(ValueError, match="document_id"):
            await adapter.store("default", [chunk])

    async def test_store_mismatched_document_ids_raises(self) -> None:
        adapter = self._make_adapter()
        c1 = _make_chunk(str(uuid4()))
        c2 = _make_chunk(str(uuid4()))
        with pytest.raises(ValueError, match="same document_id"):
            await adapter.store("default", [c1, c2])

    async def test_store_delegates_to_pgvector(self) -> None:
        adapter = self._make_adapter()
        doc_id = str(uuid4())
        chunks = [_make_chunk(doc_id)]

        fake_session = _FakeSession()
        mock_pgvector = MagicMock()
        mock_pgvector.store = AsyncMock(return_value=["chunk-1"])

        with (
            patch.object(adapter, "_get_session_factory", return_value=fake_session),
            patch.object(adapter, "_get_pgvector", return_value=mock_pgvector),
        ):
            ids = await adapter.store("default", chunks)

        assert ids == ["chunk-1"]
        mock_pgvector.store.assert_called_once()
        call_args = mock_pgvector.store.call_args[0]
        assert call_args[1] == "default"  # namespace forwarded
        assert call_args[3] is chunks  # chunks forwarded
        fake_session.session.commit.assert_called_once()

    async def test_search_delegates_to_pgvector(self) -> None:
        adapter = self._make_adapter()
        qe = QueryEmbedding(dense=[0.1, 0.2])

        fake_session = _FakeSession()
        mock_pgvector = MagicMock()
        mock_pgvector.search = AsyncMock(return_value=[])

        with (
            patch.object(adapter, "_get_session_factory", return_value=fake_session),
            patch.object(adapter, "_get_pgvector", return_value=mock_pgvector),
        ):
            results = await adapter.search("default", qe, top_k=5)

        assert results == []
        mock_pgvector.search.assert_called_once()
        call_args = mock_pgvector.search.call_args[0]
        assert call_args[1] == "default"  # namespace forwarded
        assert call_args[2] is qe  # query embedding forwarded
        assert call_args[3] == 5  # top_k forwarded

    async def test_delete_delegates_to_pgvector(self) -> None:
        adapter = self._make_adapter()
        doc_id = str(uuid4())

        fake_session = _FakeSession()
        mock_pgvector = MagicMock()
        mock_pgvector.delete = AsyncMock(return_value=3)

        with (
            patch.object(adapter, "_get_session_factory", return_value=fake_session),
            patch.object(adapter, "_get_pgvector", return_value=mock_pgvector),
        ):
            total = await adapter.delete("default", [doc_id])

        assert total == 3
        mock_pgvector.delete.assert_called_once()
        call_args = mock_pgvector.delete.call_args[0]
        assert call_args[1] == "default"  # namespace forwarded
        assert call_args[2] == UUID(doc_id)  # document_id as UUID
        fake_session.session.commit.assert_called_once()

    async def test_health_check_returns_status(self) -> None:
        adapter = self._make_adapter()

        fake_session = _FakeSession()
        mock_pgvector = MagicMock()
        mock_pgvector.health_check = AsyncMock(
            return_value=HealthStatus(status="healthy")
        )

        with (
            patch.object(adapter, "_get_session_factory", return_value=fake_session),
            patch.object(adapter, "_get_pgvector", return_value=mock_pgvector),
        ):
            status = await adapter.health_check()

        assert status.status == "healthy"
        assert status.latency_ms is not None

    async def test_health_check_returns_unhealthy_on_exception(self) -> None:
        adapter = self._make_adapter()

        fake_session = _FakeSession()
        mock_pgvector = MagicMock()
        mock_pgvector.health_check = AsyncMock(side_effect=RuntimeError("no db"))

        with (
            patch.object(adapter, "_get_session_factory", return_value=fake_session),
            patch.object(adapter, "_get_pgvector", return_value=mock_pgvector),
        ):
            status = await adapter.health_check()

        assert status.status == "unhealthy"
        assert "no db" in status.message

    async def test_store_rollback_on_exception(self) -> None:
        adapter = self._make_adapter()
        doc_id = str(uuid4())
        chunks = [_make_chunk(doc_id)]

        fake_session = _FakeSession()
        mock_pgvector = MagicMock()
        mock_pgvector.store = AsyncMock(side_effect=RuntimeError("db error"))

        with (
            patch.object(adapter, "_get_session_factory", return_value=fake_session),
            patch.object(adapter, "_get_pgvector", return_value=mock_pgvector),
        ):
            with pytest.raises(RuntimeError, match="db error"):
                await adapter.store("default", chunks)

        fake_session.session.rollback.assert_called_once()
