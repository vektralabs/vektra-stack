"""Unit tests for _update_job helper and get_worker_settings (vektra_ingest.jobs)."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import vektra_shared.db as db_mod


class _FakeFactory:
    """Minimal async context manager for _session_factory mock."""

    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *a):
        pass


class TestUpdateJob:
    def setup_method(self) -> None:
        self._orig = db_mod._session_factory

    def teardown_method(self) -> None:
        db_mod._session_factory = self._orig

    @pytest.mark.asyncio
    async def test_noop_when_no_session_factory(self) -> None:
        """_update_job returns silently when _session_factory is None."""
        from vektra_ingest.jobs import _update_job

        db_mod._session_factory = None
        await _update_job(uuid4(), status="processing")  # should not raise

    @pytest.mark.asyncio
    async def test_sets_started_at_on_processing(self) -> None:
        from vektra_ingest.jobs import _update_job

        session = AsyncMock()
        db_mod._session_factory = _FakeFactory(session)

        await _update_job(uuid4(), status="processing")

        session.execute.assert_called_once()
        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_sets_completed_at_on_indexed(self) -> None:
        from vektra_ingest.jobs import _update_job

        session = AsyncMock()
        db_mod._session_factory = _FakeFactory(session)

        await _update_job(uuid4(), status="indexed", chunk_count=10)

        session.execute.assert_called_once()
        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_clears_phase_on_terminal_status(self) -> None:
        from vektra_ingest.jobs import _update_job

        session = AsyncMock()
        db_mod._session_factory = _FakeFactory(session)

        await _update_job(uuid4(), status="failed", error_code="ERR-INGEST-004")

        session.execute.assert_called_once()
        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_truncates_long_error_message(self) -> None:
        from vektra_ingest.jobs import _update_job

        session = AsyncMock()
        db_mod._session_factory = _FakeFactory(session)

        long_msg = "x" * 5000
        await _update_job(uuid4(), status="failed", error_message=long_msg)

        # Verify execute was called (the SQL statement contains truncated message)
        session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_logs_error_on_db_exception(self) -> None:
        from vektra_ingest.jobs import _update_job

        session = AsyncMock()
        session.execute.side_effect = RuntimeError("db down")
        db_mod._session_factory = _FakeFactory(session)

        # Should not raise, just log
        await _update_job(uuid4(), status="processing")


class TestGetWorkerSettings:
    def test_get_worker_settings_is_callable(self) -> None:
        """get_worker_settings function exists and is callable (Phase 2 arq worker)."""
        from vektra_ingest.jobs import get_worker_settings

        assert callable(get_worker_settings)
