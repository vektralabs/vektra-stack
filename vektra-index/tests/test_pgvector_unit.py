"""Unit tests for PgvectorProvider using mocked AsyncSession.

Tests verify SQL structure, transaction behavior, and BLOCKER B-1/B-3
resolution without requiring a live PostgreSQL instance.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from vektra_shared.types import ChunkEmbedding


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


class TestPgvectorProvider:
    """Unit tests with mocked sessions."""

    def _make_session(self):
        session = AsyncMock()
        session.begin = MagicMock(
            return_value=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock())
        )
        session.flush = AsyncMock()
        session.execute = AsyncMock()
        session.add = MagicMock()
        return session

    @pytest.mark.asyncio
    async def test_store_returns_chunk_ids(self):
        """store() should return one chunk_id per input chunk."""
        from vektra_index.providers.pgvector import PgvectorProvider

        session = self._make_session()
        provider = PgvectorProvider(active_index_version=1)

        doc_id = uuid4()
        chunks = [
            ChunkEmbedding(chunk_id="", text="hello", dense=[0.1] * 384, metadata={}),
            ChunkEmbedding(chunk_id="", text="world", dense=[0.2] * 384, metadata={}),
        ]

        with patch("vektra_index.models.DocumentChunkOrm") as MockOrm:
            MockOrm.return_value = MagicMock()
            result = await provider.store(session, "default", doc_id, chunks)

        assert len(result) == 2
        # Each returned ID should be a valid UUID string
        for chunk_id in result:
            UUID(chunk_id)  # raises ValueError if invalid
        session.flush.assert_called()

    @pytest.mark.asyncio
    async def test_store_empty_returns_empty(self):
        from vektra_index.providers.pgvector import PgvectorProvider

        session = self._make_session()
        provider = PgvectorProvider()
        result = await provider.store(session, "default", uuid4(), [])
        assert result == []
        session.flush.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_returns_chunks_removed_count(self):
        """delete() returns the chunk count captured before deletion.

        The ORM models are imported as real classes; only session.execute()
        is mocked to return controlled results without hitting a DB.
        """
        from vektra_index.providers.pgvector import PgvectorProvider

        session = self._make_session()
        # All three session.execute() calls (COUNT, DELETE, UPDATE) return
        # the same mock; only the first one's .scalar_one() is checked.
        count_result = MagicMock()
        count_result.scalar_one.return_value = 5
        session.execute = AsyncMock(return_value=count_result)
        session.flush = AsyncMock()

        provider = PgvectorProvider()
        doc_id = uuid4()

        result = await provider.delete(session, "default", doc_id)
        assert result == 5
        # Verify execute was called at least 3 times: COUNT + DELETE + UPDATE
        assert session.execute.call_count >= 3

    @pytest.mark.asyncio
    async def test_namespace_stats_returns_counts(self):
        from vektra_index.providers.pgvector import PgvectorProvider

        session = self._make_session()
        # Two calls: doc_count=10, chunk_count=100
        results = [
            MagicMock(**{"scalar_one.return_value": 10}),
            MagicMock(**{"scalar_one.return_value": 100}),
        ]
        session.execute = AsyncMock(side_effect=results)

        provider = PgvectorProvider()
        stats = await provider.namespace_stats(session, "default")

        assert stats["document_count"] == 10
        assert stats["chunk_count"] == 100
        assert stats["namespace"] == "default"

    @pytest.mark.asyncio
    async def test_health_check_returns_healthy(self):
        from vektra_index.providers.pgvector import PgvectorProvider

        session = self._make_session()
        session.execute = AsyncMock()

        provider = PgvectorProvider()
        status = await provider.health_check(session)
        assert status.status == "healthy"
        assert status.latency_ms is not None

    @pytest.mark.asyncio
    async def test_health_check_returns_unhealthy_on_db_error(self):
        from vektra_index.providers.pgvector import PgvectorProvider

        session = self._make_session()
        session.execute = AsyncMock(side_effect=Exception("connection refused"))

        provider = PgvectorProvider()
        status = await provider.health_check(session)
        assert status.status == "unhealthy"
        assert "connection refused" in status.message


class TestPgvectorIndexVersionFilter:
    """Verify that index_version filter is always applied."""

    @pytest.mark.asyncio
    async def test_active_index_version_default_is_1(self):
        from vektra_index.providers.pgvector import PgvectorProvider

        provider = PgvectorProvider()
        assert provider._active_index_version == 1

    @pytest.mark.asyncio
    async def test_custom_index_version_used(self):
        from vektra_index.providers.pgvector import PgvectorProvider

        provider = PgvectorProvider(active_index_version=2)
        assert provider._active_index_version == 2


class TestPgvectorParentChunks:
    """Deterministic ids, parent linkage, search exclusion, retrieve (FEAT-017)."""

    def _make_session(self):
        session = AsyncMock()
        session.flush = AsyncMock()
        session.execute = AsyncMock()
        session.add = MagicMock()
        return session

    @pytest.mark.asyncio
    async def test_store_honors_deterministic_ids_and_parent_linkage(self):
        """store() keeps caller-provided uuid ids and fills the parent_id column."""
        from vektra_index.providers.pgvector import PgvectorProvider

        session = self._make_session()
        provider = PgvectorProvider()

        parent_uuid = uuid4()
        child_uuid = uuid4()
        chunks = [
            ChunkEmbedding(
                chunk_id=str(parent_uuid),
                text="parent",
                dense=[0.1] * 384,
                metadata={"chunk_level": "parent"},
            ),
            ChunkEmbedding(
                chunk_id=str(child_uuid),
                text="child",
                dense=[0.1] * 384,
                metadata={"chunk_level": "child"},
                parent_id=str(parent_uuid),
            ),
        ]

        with patch("vektra_index.models.DocumentChunkOrm") as MockOrm:
            MockOrm.return_value = MagicMock()
            result = await provider.store(session, "default", uuid4(), chunks)

        assert result == [str(parent_uuid), str(child_uuid)]
        orm_kwargs = [c.kwargs for c in MockOrm.call_args_list]
        assert orm_kwargs[0]["id"] == parent_uuid
        assert orm_kwargs[0]["parent_id"] is None
        assert orm_kwargs[1]["id"] == child_uuid
        assert orm_kwargs[1]["parent_id"] == parent_uuid

    @pytest.mark.asyncio
    async def test_store_falls_back_to_random_id_on_non_uuid(self):
        """Non-UUID caller ids (external store-chunks callers) get random uuids."""
        from vektra_index.providers.pgvector import PgvectorProvider

        session = self._make_session()
        provider = PgvectorProvider()

        chunks = [
            ChunkEmbedding(
                chunk_id="not-a-uuid",
                text="x",
                dense=[0.1] * 384,
                metadata={},
                parent_id="also-not-a-uuid",
            ),
        ]

        with patch("vektra_index.models.DocumentChunkOrm") as MockOrm:
            MockOrm.return_value = MagicMock()
            result = await provider.store(session, "default", uuid4(), chunks)

        UUID(result[0])  # random but valid
        assert MockOrm.call_args.kwargs["parent_id"] is None

    @pytest.mark.asyncio
    async def test_dense_search_excludes_parent_chunks(self):
        """The dense search SQL filters out chunk_level=parent rows."""
        from sqlalchemy.dialects import postgresql

        from vektra_index.providers.pgvector import PgvectorProvider
        from vektra_shared.types import QueryEmbedding

        session = self._make_session()
        empty_result = MagicMock()
        empty_result.all.return_value = []
        session.execute = AsyncMock(return_value=empty_result)

        provider = PgvectorProvider()
        await provider.search(
            session, "default", QueryEmbedding(dense=[0.1] * 384), top_k=5
        )

        stmt = session.execute.call_args.args[0]
        compiled = stmt.compile(dialect=postgresql.dialect())
        assert "IS DISTINCT FROM" in str(compiled)
        # The JSONB accessor key and the excluded value are bind parameters
        assert "chunk_level" in compiled.params.values()
        assert "parent" in compiled.params.values()

    @pytest.mark.asyncio
    async def test_retrieve_maps_rows_and_skips_invalid_ids(self):
        from vektra_index.providers.pgvector import PgvectorProvider

        session = self._make_session()
        parent_uuid = uuid4()
        doc_id = uuid4()
        row = FakeRow(
            id=parent_uuid,
            document_id=doc_id,
            content="parent text",
            chunk_metadata={"chunk_level": "parent"},
            parent_id=None,
            score=0.0,
            document_version=1,
        )
        rows_result = MagicMock()
        rows_result.all.return_value = [row]
        session.execute = AsyncMock(return_value=rows_result)

        provider = PgvectorProvider()
        results = await provider.retrieve(
            session, "default", [str(parent_uuid), "not-a-uuid"]
        )

        assert len(results) == 1
        assert results[0].chunk_id == str(parent_uuid)
        assert results[0].score == 0.0
        assert results[0].text_snippet == "parent text"
        assert results[0].parent_id is None

    @pytest.mark.asyncio
    async def test_retrieve_all_invalid_ids_returns_empty(self):
        from vektra_index.providers.pgvector import PgvectorProvider

        session = self._make_session()
        provider = PgvectorProvider()

        results = await provider.retrieve(session, "default", ["nope", "still-nope"])

        assert results == []
        session.execute.assert_not_called()
