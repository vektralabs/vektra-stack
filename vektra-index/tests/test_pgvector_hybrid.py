"""Unit tests for PgvectorProvider hybrid search (T4-T6).

Tests verify that the correct search paths are taken for each SearchMode
and that RRF fusion logic works correctly. Uses mocked sessions (no DB).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from vektra_shared.types import (
    ChunkEmbedding,
    QueryEmbedding,
    SearchMode,
    SearchResult,
    SparseVector,
)


class FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value

    def all(self):
        return []


class FakeRow:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _make_session():
    session = AsyncMock()
    session.begin = MagicMock(
        return_value=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock())
    )
    session.flush = AsyncMock()
    session.execute = AsyncMock()
    session.add = MagicMock()
    return session


class TestPgvectorStoreWithSparse:
    """T4: store() persists sparse_vector as JSONB."""

    @pytest.mark.asyncio
    async def test_store_with_sparse_sets_sparse_vector(self):
        from vektra_index.providers.pgvector import PgvectorProvider

        session = _make_session()
        provider = PgvectorProvider(active_index_version=1)

        doc_id = uuid4()
        chunks = [
            ChunkEmbedding(
                chunk_id="",
                text="hello",
                dense=[0.1] * 384,
                sparse=SparseVector(indices=[0, 5], values=[0.5, 0.8]),
                metadata={},
            ),
        ]

        captured_orms = []
        original_add = session.add

        def capture_add(obj):
            captured_orms.append(obj)
            return original_add(obj)

        session.add = capture_add

        with patch("vektra_index.providers.pgvector.uuid4", return_value=uuid4()):
            result = await provider.store(session, "default", doc_id, chunks)

        assert len(result) == 1
        assert captured_orms[0].sparse_vector == {
            "indices": [0, 5],
            "values": [0.5, 0.8],
        }

    @pytest.mark.asyncio
    async def test_store_without_sparse_sets_null(self):
        from vektra_index.providers.pgvector import PgvectorProvider

        session = _make_session()
        provider = PgvectorProvider(active_index_version=1)

        doc_id = uuid4()
        chunks = [
            ChunkEmbedding(chunk_id="", text="hello", dense=[0.1] * 384, metadata={}),
        ]

        captured_orms = []
        original_add = session.add

        def capture_add(obj):
            captured_orms.append(obj)
            return original_add(obj)

        session.add = capture_add

        with patch("vektra_index.providers.pgvector.uuid4", return_value=uuid4()):
            await provider.store(session, "default", doc_id, chunks)

        assert captured_orms[0].sparse_vector is None


class TestPgvectorSearchModeDispatch:
    """Verify correct method dispatch for each SearchMode."""

    @pytest.mark.asyncio
    async def test_dense_mode_calls_search_dense(self):
        from vektra_index.providers.pgvector import PgvectorProvider

        provider = PgvectorProvider()
        session = _make_session()
        query_embedding = QueryEmbedding(dense=[0.1] * 384)

        with patch.object(
            provider, "_search_dense", new_callable=AsyncMock, return_value=[]
        ) as mock:
            await provider.search(session, "ns", query_embedding, 5, SearchMode.DENSE)
            mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_sparse_mode_calls_search_sparse(self):
        from vektra_index.providers.pgvector import PgvectorProvider

        provider = PgvectorProvider()
        session = _make_session()
        query_embedding = QueryEmbedding(
            dense=[0.1] * 384,
            sparse=SparseVector(indices=[1], values=[0.5]),
        )

        with patch.object(
            provider, "_search_sparse", new_callable=AsyncMock, return_value=[]
        ) as mock:
            await provider.search(session, "ns", query_embedding, 5, SearchMode.SPARSE)
            mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_hybrid_mode_calls_search_hybrid(self):
        from vektra_index.providers.pgvector import PgvectorProvider

        provider = PgvectorProvider()
        session = _make_session()
        query_embedding = QueryEmbedding(
            dense=[0.1] * 384,
            sparse=SparseVector(indices=[1], values=[0.5]),
        )

        with patch.object(
            provider, "_search_hybrid", new_callable=AsyncMock, return_value=[]
        ) as mock:
            await provider.search(session, "ns", query_embedding, 5, SearchMode.HYBRID)
            mock.assert_called_once()


class TestPgvectorSparseFallback:
    """Sparse and hybrid fall back to DENSE when no sparse embedding."""

    @pytest.mark.asyncio
    async def test_sparse_without_sparse_embedding_falls_back_to_dense(self):
        from vektra_index.providers.pgvector import PgvectorProvider

        provider = PgvectorProvider()
        session = _make_session()
        query_embedding = QueryEmbedding(dense=[0.1] * 384, sparse=None)

        with patch.object(
            provider, "_search_dense", new_callable=AsyncMock, return_value=[]
        ) as mock:
            await provider._search_sparse(session, "ns", query_embedding, 5)
            mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_hybrid_without_sparse_embedding_falls_back_to_dense(self):
        from vektra_index.providers.pgvector import PgvectorProvider

        provider = PgvectorProvider()
        session = _make_session()
        query_embedding = QueryEmbedding(dense=[0.1] * 384, sparse=None)

        with patch.object(
            provider, "_search_dense", new_callable=AsyncMock, return_value=[]
        ) as mock:
            await provider._search_hybrid(session, "ns", query_embedding, 5)
            mock.assert_called_once()


class TestRRFFusion:
    """Test Reciprocal Rank Fusion scoring logic."""

    @pytest.mark.asyncio
    async def test_rrf_combines_dense_and_sparse_ranks(self):
        """Chunk appearing in both dense and sparse results gets higher RRF score."""
        from vektra_index.providers.pgvector import PgvectorProvider

        provider = PgvectorProvider()
        session = _make_session()

        chunk_a = uuid4()
        chunk_b = uuid4()
        chunk_c = uuid4()
        doc_id = uuid4()

        dense_results = [
            SearchResult(
                chunk_id=str(chunk_a), score=0.9, text_snippet="a", document_id=doc_id
            ),
            SearchResult(
                chunk_id=str(chunk_b), score=0.8, text_snippet="b", document_id=doc_id
            ),
        ]
        sparse_results = [
            SearchResult(
                chunk_id=str(chunk_b), score=0.7, text_snippet="b", document_id=doc_id
            ),
            SearchResult(
                chunk_id=str(chunk_c), score=0.6, text_snippet="c", document_id=doc_id
            ),
        ]

        query_embedding = QueryEmbedding(
            dense=[0.1] * 384,
            sparse=SparseVector(indices=[1], values=[0.5]),
        )

        with (
            patch.object(
                provider,
                "_search_dense",
                new_callable=AsyncMock,
                return_value=dense_results,
            ),
            patch.object(
                provider,
                "_search_sparse",
                new_callable=AsyncMock,
                return_value=sparse_results,
            ),
        ):
            results = await provider._search_hybrid(session, "ns", query_embedding, 3)

        # chunk_b appears in both, so should have highest RRF score
        assert results[0].chunk_id == str(chunk_b)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_rrf_respects_top_k(self):
        from vektra_index.providers.pgvector import PgvectorProvider

        provider = PgvectorProvider()
        session = _make_session()
        doc_id = uuid4()

        dense_results = [
            SearchResult(
                chunk_id=str(uuid4()),
                score=0.9 - i * 0.1,
                text_snippet=f"d{i}",
                document_id=doc_id,
            )
            for i in range(5)
        ]
        sparse_results = [
            SearchResult(
                chunk_id=str(uuid4()),
                score=0.8 - i * 0.1,
                text_snippet=f"s{i}",
                document_id=doc_id,
            )
            for i in range(5)
        ]

        query_embedding = QueryEmbedding(
            dense=[0.1] * 384,
            sparse=SparseVector(indices=[1], values=[0.5]),
        )

        with (
            patch.object(
                provider,
                "_search_dense",
                new_callable=AsyncMock,
                return_value=dense_results,
            ),
            patch.object(
                provider,
                "_search_sparse",
                new_callable=AsyncMock,
                return_value=sparse_results,
            ),
        ):
            results = await provider._search_hybrid(session, "ns", query_embedding, 3)

        assert len(results) == 3
