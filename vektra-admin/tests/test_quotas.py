"""Unit tests for namespace quota enforcement (ARCH-047)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException


def _make_session(
    *,
    namespace_row: Any = None,
    doc_count: int = 0,
) -> AsyncMock:
    """Build a mock AsyncSession for quota checks.

    Returns execute results in call order:
    1. Namespace quota SELECT (one_or_none)
    2. Document count SELECT (scalar_one) - only if doc quota is set

    The chunk count no longer goes through the session: it is served by the
    injected VectorStoreProvider (see _make_vector_store), since document_chunks
    is private to the pgvector provider (ADR-0026).
    """
    results: list[MagicMock] = []

    # First call: fetch namespace quotas
    ns_result = MagicMock()
    ns_result.one_or_none.return_value = namespace_row
    results.append(ns_result)

    # Second call (only if a document quota is set): document count
    if namespace_row is not None:
        quota_docs = getattr(namespace_row, "quota_documents", None)
        if quota_docs is not None:
            doc_result = MagicMock()
            doc_result.scalar_one.return_value = doc_count
            results.append(doc_result)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=results)
    return session


def _make_vector_store(chunk_count: int = 0) -> AsyncMock:
    """Mock VectorStoreProvider whose count_chunks returns chunk_count."""
    store = AsyncMock()
    store.count_chunks = AsyncMock(return_value=chunk_count)
    return store


def _namespace_row(
    quota_documents: int | None = None,
    quota_chunks: int | None = None,
) -> MagicMock:
    row = MagicMock()
    row.quota_documents = quota_documents
    row.quota_chunks = quota_chunks
    return row


class TestQuotaNoLimit:
    @pytest.mark.asyncio
    async def test_namespace_not_found_passes(self):
        """Missing namespace does not block ingest."""
        from vektra_admin.quotas import check_namespace_quota

        session = _make_session(namespace_row=None)
        await check_namespace_quota(session, "ns-1", new_documents=1)

    @pytest.mark.asyncio
    async def test_both_quotas_null_passes(self):
        """NULL quotas (unlimited) skip counting entirely."""
        from vektra_admin.quotas import check_namespace_quota

        row = _namespace_row(quota_documents=None, quota_chunks=None)
        session = _make_session(namespace_row=row)
        await check_namespace_quota(session, "ns-1", new_documents=5, new_chunks=100)
        # Only 1 execute call (namespace fetch); no count queries
        assert session.execute.call_count == 1


class TestDocumentQuota:
    @pytest.mark.asyncio
    async def test_under_limit_passes(self):
        """Adding documents under the quota succeeds."""
        from vektra_admin.quotas import check_namespace_quota

        row = _namespace_row(quota_documents=10)
        session = _make_session(namespace_row=row, doc_count=5)
        await check_namespace_quota(session, "ns-1", new_documents=3)

    @pytest.mark.asyncio
    async def test_at_limit_passes(self):
        """Adding documents that exactly reach the quota succeeds."""
        from vektra_admin.quotas import check_namespace_quota

        row = _namespace_row(quota_documents=10)
        session = _make_session(namespace_row=row, doc_count=8)
        await check_namespace_quota(session, "ns-1", new_documents=2)

    @pytest.mark.asyncio
    async def test_over_limit_raises(self):
        """Exceeding document quota raises HTTPException 422 with ERR-QUOTA-001."""
        from vektra_admin.quotas import check_namespace_quota

        row = _namespace_row(quota_documents=10)
        session = _make_session(namespace_row=row, doc_count=9)
        with pytest.raises(HTTPException) as exc_info:
            await check_namespace_quota(session, "ns-1", new_documents=2)
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["error"]["code"] == "ERR-QUOTA-001"

    @pytest.mark.asyncio
    async def test_zero_new_documents_skips_check(self):
        """new_documents=0 skips the document count query."""
        from vektra_admin.quotas import check_namespace_quota

        row = _namespace_row(quota_documents=10)
        session = _make_session(namespace_row=row, doc_count=999)
        await check_namespace_quota(session, "ns-1", new_documents=0)
        # Only 1 execute call (namespace fetch); no count query
        assert session.execute.call_count == 1


class TestChunkQuota:
    @pytest.mark.asyncio
    async def test_under_limit_passes(self):
        """Adding chunks under the quota succeeds."""
        from vektra_admin.quotas import check_namespace_quota

        row = _namespace_row(quota_chunks=1000)
        session = _make_session(namespace_row=row)
        store = _make_vector_store(chunk_count=500)
        await check_namespace_quota(session, "ns-1", new_chunks=100, vector_store=store)
        store.count_chunks.assert_awaited_once_with("ns-1")

    @pytest.mark.asyncio
    async def test_over_limit_raises(self):
        """Exceeding chunk quota raises HTTPException 422 with ERR-QUOTA-001."""
        from vektra_admin.quotas import check_namespace_quota

        row = _namespace_row(quota_chunks=1000)
        session = _make_session(namespace_row=row)
        store = _make_vector_store(chunk_count=950)
        with pytest.raises(HTTPException) as exc_info:
            await check_namespace_quota(
                session, "ns-1", new_chunks=100, vector_store=store
            )
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["error"]["code"] == "ERR-QUOTA-001"

    @pytest.mark.asyncio
    async def test_at_limit_passes(self):
        """Adding chunks that exactly reach the quota succeeds."""
        from vektra_admin.quotas import check_namespace_quota

        row = _namespace_row(quota_chunks=1000)
        session = _make_session(namespace_row=row)
        store = _make_vector_store(chunk_count=900)
        await check_namespace_quota(session, "ns-1", new_chunks=100, vector_store=store)

    @pytest.mark.asyncio
    async def test_missing_vector_store_raises(self):
        """A chunk quota that must be enforced without a provider is a wiring bug."""
        from vektra_admin.quotas import check_namespace_quota

        row = _namespace_row(quota_chunks=1000)
        session = _make_session(namespace_row=row)
        with pytest.raises(ValueError, match="vector_store is required"):
            await check_namespace_quota(session, "ns-1", new_chunks=100)

    @pytest.mark.asyncio
    async def test_zero_new_chunks_skips_check(self):
        """new_chunks=0 skips the chunk count entirely (no provider call)."""
        from vektra_admin.quotas import check_namespace_quota

        row = _namespace_row(quota_chunks=1000)
        session = _make_session(namespace_row=row)
        store = _make_vector_store(chunk_count=999)
        await check_namespace_quota(session, "ns-1", new_chunks=0, vector_store=store)
        assert session.execute.call_count == 1
        store.count_chunks.assert_not_awaited()


class TestCombinedQuotas:
    @pytest.mark.asyncio
    async def test_both_quotas_under_limit_passes(self):
        """Both document and chunk quotas under limit succeeds."""
        from vektra_admin.quotas import check_namespace_quota

        row = _namespace_row(quota_documents=10, quota_chunks=1000)
        session = _make_session(namespace_row=row, doc_count=5)
        store = _make_vector_store(chunk_count=500)
        await check_namespace_quota(
            session, "ns-1", new_documents=1, new_chunks=50, vector_store=store
        )

    @pytest.mark.asyncio
    async def test_doc_quota_exceeded_with_both_set(self):
        """Document quota exceeded blocks even if chunk quota is fine."""
        from vektra_admin.quotas import check_namespace_quota

        row = _namespace_row(quota_documents=10, quota_chunks=1000)
        session = _make_session(namespace_row=row, doc_count=10)
        store = _make_vector_store(chunk_count=0)
        with pytest.raises(HTTPException) as exc_info:
            await check_namespace_quota(
                session, "ns-1", new_documents=1, new_chunks=10, vector_store=store
            )
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["error"]["code"] == "ERR-QUOTA-001"
        # Document quota is checked first, so the chunk count is never reached.
        store.count_chunks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_chunk_quota_exceeded_with_both_set(self):
        """Chunk quota exceeded blocks even if document quota is fine."""
        from vektra_admin.quotas import check_namespace_quota

        row = _namespace_row(quota_documents=10, quota_chunks=100)
        session = _make_session(namespace_row=row, doc_count=5)
        store = _make_vector_store(chunk_count=95)
        with pytest.raises(HTTPException) as exc_info:
            await check_namespace_quota(
                session, "ns-1", new_documents=1, new_chunks=10, vector_store=store
            )
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["error"]["code"] == "ERR-QUOTA-001"
