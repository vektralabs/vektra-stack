"""Unit tests for cleanup_soft_deleted_task (REQ-057)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


def _make_ctx(vector_store: AsyncMock | None = None) -> dict:
    """arq context carrying the ProviderRegistry the task needs."""
    store = vector_store or AsyncMock()
    registry = MagicMock()
    registry.get = MagicMock(return_value=store)
    return {"registry": registry}


@pytest.mark.asyncio
async def test_cleanup_purges_expired_documents():
    """Documents soft-deleted beyond retention are purged from both stores.

    The chunks must leave the vector store, not just Postgres: relying on the
    document_chunks CASCADE purged nothing in Qdrant mode and left the content
    searchable and untraceable (BUG-023, ADR-0026).
    """
    from vektra_ingest.jobs import cleanup_soft_deleted_task

    expired_id = uuid4()

    mock_result = MagicMock()
    mock_result.all.return_value = [(expired_id, "default")]

    session = AsyncMock()
    session.execute = AsyncMock(return_value=mock_result)
    session.commit = AsyncMock()

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    vector_store = AsyncMock()

    with (
        patch("vektra_ingest.jobs._shared_db") as mock_db,
        patch("vektra_shared.config.ObservabilityConfig") as mock_config_cls,
    ):
        mock_db._session_factory = mock_factory
        mock_config_cls.return_value.retention_days = 30

        await cleanup_soft_deleted_task(_make_ctx(vector_store))

    vector_store.delete.assert_awaited_once_with("default", [str(expired_id)])
    # SELECT + DELETE
    assert session.execute.call_count == 2
    session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_keeps_document_when_chunk_removal_fails():
    """A document whose chunks survive must not lose its Postgres row.

    Dropping the row while the content stays in the vector store is the one
    outcome retention must never produce: the orphaned chunks are then
    untraceable to any document.
    """
    from vektra_ingest.jobs import cleanup_soft_deleted_task

    mock_result = MagicMock()
    mock_result.all.return_value = [(uuid4(), "default")]

    session = AsyncMock()
    session.execute = AsyncMock(return_value=mock_result)
    session.commit = AsyncMock()

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    vector_store = AsyncMock()
    vector_store.delete = AsyncMock(side_effect=Exception("qdrant unreachable"))

    with (
        patch("vektra_ingest.jobs._shared_db") as mock_db,
        patch("vektra_shared.config.ObservabilityConfig") as mock_config_cls,
    ):
        mock_db._session_factory = mock_factory
        mock_config_cls.return_value.retention_days = 30

        await cleanup_soft_deleted_task(_make_ctx(vector_store))

    # Only the SELECT ran: no hard-delete, so the next run retries it
    assert session.execute.call_count == 1
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_skips_when_retention_days_not_set():
    """No cleanup when VEKTRA_RETENTION_DAYS is None."""
    from vektra_ingest.jobs import cleanup_soft_deleted_task

    with (
        patch("vektra_ingest.jobs._shared_db") as mock_db,
        patch("vektra_shared.config.ObservabilityConfig") as mock_config_cls,
    ):
        mock_config_cls.return_value.retention_days = None
        await cleanup_soft_deleted_task(_make_ctx())

    # Should never access the database
    mock_db._session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_skips_when_nothing_to_purge():
    """No hard-deletes when no expired documents exist."""
    from vektra_ingest.jobs import cleanup_soft_deleted_task

    mock_result = MagicMock()
    mock_result.all.return_value = []  # no expired docs

    session = AsyncMock()
    session.execute = AsyncMock(return_value=mock_result)

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    vector_store = AsyncMock()

    with (
        patch("vektra_ingest.jobs._shared_db") as mock_db,
        patch("vektra_shared.config.ObservabilityConfig") as mock_config_cls,
    ):
        mock_db._session_factory = mock_factory
        mock_config_cls.return_value.retention_days = 30

        await cleanup_soft_deleted_task(_make_ctx(vector_store))

    # Only the SELECT, no DELETE
    assert session.execute.call_count == 1
    session.commit.assert_not_called()
    vector_store.delete.assert_not_awaited()
