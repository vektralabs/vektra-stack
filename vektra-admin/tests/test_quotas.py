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
    chunk_count: int = 0,
) -> AsyncMock:
    """Build a mock AsyncSession for quota checks.

    Returns execute results in call order:
    1. Namespace quota SELECT (one_or_none)
    2. Document count SELECT (scalar_one) - only if doc quota is set
    3. Chunk count SELECT (scalar_one) - only if chunk quota is set

    Since the function skips count queries when quotas are NULL or new_count=0,
    the mock uses positional side_effect. Callers should ensure the namespace_row
    has the correct quota_documents/quota_chunks to match the expected call order.

    For simplicity, both count results are included unconditionally.
    """
    results: list[MagicMock] = []

    # First call: fetch namespace quotas
    ns_result = MagicMock()
    ns_result.one_or_none.return_value = namespace_row
    results.append(ns_result)

    # Subsequent calls: count queries (order depends on which quotas are checked)
    # The function checks docs first, then chunks. Include both in order.
    if namespace_row is not None:
        quota_docs = getattr(namespace_row, "quota_documents", None)
        quota_chunks = getattr(namespace_row, "quota_chunks", None)

        if quota_docs is not None:
            doc_result = MagicMock()
            doc_result.scalar_one.return_value = doc_count
            results.append(doc_result)

        if quota_chunks is not None:
            chunk_result = MagicMock()
            chunk_result.scalar_one.return_value = chunk_count
            results.append(chunk_result)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=results)
    return session


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
        session = _make_session(namespace_row=row, chunk_count=500)
        await check_namespace_quota(session, "ns-1", new_chunks=100)

    @pytest.mark.asyncio
    async def test_over_limit_raises(self):
        """Exceeding chunk quota raises HTTPException 422 with ERR-QUOTA-001."""
        from vektra_admin.quotas import check_namespace_quota

        row = _namespace_row(quota_chunks=1000)
        session = _make_session(namespace_row=row, chunk_count=950)
        with pytest.raises(HTTPException) as exc_info:
            await check_namespace_quota(session, "ns-1", new_chunks=100)
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["error"]["code"] == "ERR-QUOTA-001"

    @pytest.mark.asyncio
    async def test_zero_new_chunks_skips_check(self):
        """new_chunks=0 skips the chunk count query."""
        from vektra_admin.quotas import check_namespace_quota

        row = _namespace_row(quota_chunks=1000)
        session = _make_session(namespace_row=row, chunk_count=999)
        await check_namespace_quota(session, "ns-1", new_chunks=0)
        assert session.execute.call_count == 1


class TestCombinedQuotas:
    @pytest.mark.asyncio
    async def test_both_quotas_under_limit_passes(self):
        """Both document and chunk quotas under limit succeeds."""
        from vektra_admin.quotas import check_namespace_quota

        row = _namespace_row(quota_documents=10, quota_chunks=1000)
        session = _make_session(namespace_row=row, doc_count=5, chunk_count=500)
        await check_namespace_quota(session, "ns-1", new_documents=1, new_chunks=50)

    @pytest.mark.asyncio
    async def test_doc_quota_exceeded_with_both_set(self):
        """Document quota exceeded blocks even if chunk quota is fine."""
        from vektra_admin.quotas import check_namespace_quota

        row = _namespace_row(quota_documents=10, quota_chunks=1000)
        session = _make_session(namespace_row=row, doc_count=10)
        with pytest.raises(HTTPException) as exc_info:
            await check_namespace_quota(session, "ns-1", new_documents=1, new_chunks=10)
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["error"]["code"] == "ERR-QUOTA-001"
