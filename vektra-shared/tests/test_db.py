"""Unit tests for vektra_shared.db (engine factory and session dependency)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import vektra_shared.db as db_mod


class TestDbModule:
    """Tests for init_db, get_engine, get_session_factory, and get_session."""

    def setup_method(self) -> None:
        self._orig_engine = db_mod._engine
        self._orig_factory = db_mod._session_factory
        db_mod._engine = None
        db_mod._session_factory = None

    def teardown_method(self) -> None:
        db_mod._engine = self._orig_engine
        db_mod._session_factory = self._orig_factory

    def test_get_engine_raises_before_init(self) -> None:
        with pytest.raises(RuntimeError, match="Database not initialized"):
            db_mod.get_engine()

    def test_get_session_factory_raises_before_init(self) -> None:
        with pytest.raises(RuntimeError, match="Database not initialized"):
            db_mod.get_session_factory()

    @patch("vektra_shared.db.create_async_engine")
    @patch("vektra_shared.db.async_sessionmaker")
    def test_init_db_creates_engine_and_factory(
        self, mock_sessionmaker: MagicMock, mock_create_engine: MagicMock
    ) -> None:
        mock_engine = MagicMock()
        mock_create_engine.return_value = mock_engine
        mock_factory = MagicMock()
        mock_sessionmaker.return_value = mock_factory

        db_mod.init_db("postgresql+asyncpg://user:pass@host/db")

        mock_create_engine.assert_called_once()
        call_kwargs = mock_create_engine.call_args
        assert call_kwargs[0][0] == "postgresql+asyncpg://user:pass@host/db"
        assert call_kwargs[1]["pool_size"] == 10
        assert call_kwargs[1]["pool_pre_ping"] is True

        assert db_mod.get_engine() is mock_engine
        assert db_mod.get_session_factory() is mock_factory

    @patch("vektra_shared.db.create_async_engine")
    @patch("vektra_shared.db.async_sessionmaker")
    def test_init_db_forwards_custom_kwargs(
        self, mock_sessionmaker: MagicMock, mock_create_engine: MagicMock
    ) -> None:
        db_mod.init_db(
            "postgresql+asyncpg://u:p@h/d",
            pool_size=5,
            max_overflow=10,
            echo=True,
        )
        call_kwargs = mock_create_engine.call_args[1]
        assert call_kwargs["pool_size"] == 5
        assert call_kwargs["max_overflow"] == 10
        assert call_kwargs["echo"] is True

    async def test_get_session_raises_before_init(self) -> None:
        with pytest.raises(RuntimeError, match="Database not initialized"):
            async for _ in db_mod.get_session():
                pass
