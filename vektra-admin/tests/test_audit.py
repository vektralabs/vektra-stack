"""Unit tests: audit log behavior (REQ-022, NFR-007).

log_event is async and creates its own session via vektra_shared.db._session_factory.
Tests patch the factory at its source module (vektra_shared.db).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

import vektra_admin.audit as audit_mod


def _make_mock_factory(mock_session: AsyncMock) -> MagicMock:
    """Return a mock async context manager factory yielding mock_session."""
    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_factory


async def test_log_event_commits_to_db():
    """log_event must create a session and commit an AuditLogOrm row."""
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()
    mock_factory = _make_mock_factory(mock_session)

    key_id = uuid4()
    request_id = uuid4()

    # _session_factory lives in vektra_shared.db (late import in audit.log_event)
    with patch("vektra_shared.db._session_factory", mock_factory):
        await audit_mod.log_event(
            key_id=key_id,
            endpoint="/api/v1/api-keys",
            method="POST",
            status_code=201,
            request_id=request_id,
            action="apikey_created",
        )

    mock_session.add.assert_called_once()
    mock_session.commit.assert_awaited_once()


async def test_log_event_does_not_raise_on_db_error():
    """Write failures must be logged as ERROR but must NOT propagate (NFR-007)."""
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock(side_effect=RuntimeError("DB down"))
    mock_factory = _make_mock_factory(mock_session)

    with patch("vektra_shared.db._session_factory", mock_factory):
        # Must not raise
        await audit_mod.log_event(
            key_id=uuid4(),
            endpoint="/health",
            method="GET",
            status_code=200,
            request_id=uuid4(),
        )
    # No exception means fire-and-forget safety is intact


async def test_log_event_no_op_without_factory():
    """log_event must return silently when _session_factory is None (before init_db)."""
    with patch("vektra_shared.db._session_factory", None):
        # Must not raise
        await audit_mod.log_event(
            key_id=uuid4(),
            endpoint="/health",
            method="GET",
            status_code=200,
            request_id=uuid4(),
        )
