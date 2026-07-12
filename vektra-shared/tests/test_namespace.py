"""Unit tests for namespace configuration utilities (FEAT-020)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from vektra_shared.namespace import (
    resolve_citations_enabled,
    resolve_grounding_mode,
)


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


# --- citations_enabled tests (FEAT-021) ---


@pytest.mark.asyncio
async def test_citations_enabled_from_namespace_config():
    """Namespace config.citations_enabled=true overrides the default."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = {"citations_enabled": True}
    mock_session.execute.return_value = mock_result

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await resolve_citations_enabled("test-ns", mock_factory)
    assert result is True


@pytest.mark.asyncio
async def test_citations_enabled_defaults_false_when_key_missing():
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = {"grounding_mode": "strict"}
    mock_session.execute.return_value = mock_result

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await resolve_citations_enabled("test-ns", mock_factory)
    assert result is False


@pytest.mark.asyncio
async def test_citations_enabled_ignores_non_bool_value():
    """A truthy non-bool ("yes", 1) in config must not enable citations."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = {"citations_enabled": "yes"}
    mock_session.execute.return_value = mock_result

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await resolve_citations_enabled("test-ns", mock_factory)
    assert result is False


@pytest.mark.asyncio
async def test_citations_enabled_defaults_false_on_db_error():
    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(
        side_effect=RuntimeError("db down")
    )
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await resolve_citations_enabled("test-ns", mock_factory)
    assert result is False
