"""Unit tests for namespace configuration utilities (FEAT-020)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from vektra_shared.namespace import resolve_grounding_mode


@pytest.mark.asyncio
async def test_returns_mode_from_namespace_config():
    """Namespace config.grounding_mode overrides default."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = {"grounding_mode": "hybrid"}
    mock_session.execute.return_value = mock_result

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await resolve_grounding_mode(
        "test-ns", mock_factory, default_mode="strict"
    )
    assert result == "hybrid"


@pytest.mark.asyncio
async def test_returns_default_when_namespace_has_no_grounding_mode():
    """Namespace config without grounding_mode falls back to default."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = {"other_key": "value"}
    mock_session.execute.return_value = mock_result

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await resolve_grounding_mode(
        "test-ns", mock_factory, default_mode="strict"
    )
    assert result == "strict"


@pytest.mark.asyncio
async def test_returns_default_when_namespace_not_found():
    """Non-existent namespace falls back to default."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_result

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await resolve_grounding_mode(
        "nonexistent", mock_factory, default_mode="strict"
    )
    assert result == "strict"


@pytest.mark.asyncio
async def test_returns_default_on_db_error():
    """Database errors fall back gracefully to default."""
    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(
        side_effect=RuntimeError("DB down")
    )
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await resolve_grounding_mode(
        "test-ns", mock_factory, default_mode="strict"
    )
    assert result == "strict"


@pytest.mark.asyncio
async def test_returns_default_for_invalid_mode_in_config():
    """Invalid grounding_mode value in namespace config falls back to default."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = {"grounding_mode": "invalid"}
    mock_session.execute.return_value = mock_result

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await resolve_grounding_mode(
        "test-ns", mock_factory, default_mode="strict"
    )
    assert result == "strict"
