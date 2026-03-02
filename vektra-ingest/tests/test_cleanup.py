"""Unit tests for cleanup_soft_deleted_task (REQ-057)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_cleanup_purges_expired_documents():
    """Documents soft-deleted beyond retention period are hard-deleted."""
    from vektra_ingest.jobs import cleanup_soft_deleted_task

    expired_id = uuid4()

    mock_result = MagicMock()
    mock_result.all.return_value = [(expired_id,)]

    session = AsyncMock()
    session.execute = AsyncMock(return_value=mock_result)
    session.commit = AsyncMock()

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("vektra_ingest.jobs._shared_db") as mock_db, \
         patch("vektra_shared.config.ObservabilityConfig") as mock_config_cls:
        mock_db._session_factory = mock_factory
        mock_config_cls.return_value.retention_days = 30

        await cleanup_soft_deleted_task({})

    # Should have executed 2 queries: SELECT + DELETE
    assert session.execute.call_count == 2
    session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_skips_when_retention_days_not_set():
    """No cleanup when VEKTRA_RETENTION_DAYS is None."""
    from vektra_ingest.jobs import cleanup_soft_deleted_task

    with patch("vektra_shared.config.ObservabilityConfig") as mock_config_cls:
        mock_config_cls.return_value.retention_days = None

        # Should return without accessing DB
        await cleanup_soft_deleted_task({})


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

    with patch("vektra_ingest.jobs._shared_db") as mock_db, \
         patch("vektra_shared.config.ObservabilityConfig") as mock_config_cls:
        mock_db._session_factory = mock_factory
        mock_config_cls.return_value.retention_days = 30

        await cleanup_soft_deleted_task({})

    # Only the SELECT, no DELETE
    assert session.execute.call_count == 1
    session.commit.assert_not_called()
