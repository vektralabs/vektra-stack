"""Unit tests: bootstrap key enforcement (REQ-036, ARCH-025)."""

from unittest.mock import AsyncMock, MagicMock

from vektra_admin.bootstrap import (
    get_bootstrap_key,
    is_bootstrap_consumed,
    is_bootstrap_key,
    warn_if_bootstrap_in_production,
)


def test_get_bootstrap_key_none_when_unset(monkeypatch):
    monkeypatch.delenv("VEKTRA_ADMIN_BOOTSTRAP_KEY", raising=False)
    assert get_bootstrap_key() is None


def test_get_bootstrap_key_returns_env_value(monkeypatch):
    monkeypatch.setenv("VEKTRA_ADMIN_BOOTSTRAP_KEY", "secret123")
    assert get_bootstrap_key() == "secret123"


def test_is_bootstrap_key_false_when_unset(monkeypatch):
    monkeypatch.delenv("VEKTRA_ADMIN_BOOTSTRAP_KEY", raising=False)
    assert is_bootstrap_key("anything") is False


def test_is_bootstrap_key_matches(monkeypatch):
    monkeypatch.setenv("VEKTRA_ADMIN_BOOTSTRAP_KEY", "mybootstrapkey")
    assert is_bootstrap_key("mybootstrapkey") is True


def test_is_bootstrap_key_no_match(monkeypatch):
    monkeypatch.setenv("VEKTRA_ADMIN_BOOTSTRAP_KEY", "mybootstrapkey")
    assert is_bootstrap_key("wrong") is False


async def test_is_bootstrap_consumed_false_when_value_is_false():
    # Do NOT mock ORM class — mock session.execute() directly (see MEMORY.md pattern).
    # The ORM builds the SELECT; the mocked execute returns whatever we need.
    session = AsyncMock()
    mock_row = MagicMock()
    mock_row.value = "false"

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_row
    session.execute.return_value = mock_result

    result = await is_bootstrap_consumed(session)
    assert result is False


async def test_is_bootstrap_consumed_true_when_value_is_true():
    session = AsyncMock()
    mock_row = MagicMock()
    mock_row.value = "true"

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_row
    session.execute.return_value = mock_result

    result = await is_bootstrap_consumed(session)
    assert result is True


async def test_is_bootstrap_consumed_true_when_row_missing():
    """Missing row is treated as consumed for safety."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute.return_value = mock_result

    result = await is_bootstrap_consumed(session)
    assert result is True


def test_warn_if_bootstrap_in_production_logs(monkeypatch):
    monkeypatch.setenv("VEKTRA_ADMIN_BOOTSTRAP_KEY", "secret")
    monkeypatch.setenv("VEKTRA_ENV", "production")
    import structlog.testing

    with structlog.testing.capture_logs() as captured:
        warn_if_bootstrap_in_production()
    assert any("bootstrap_key_set_in_production" in str(e) for e in captured)


def test_warn_if_bootstrap_not_production_no_warn(monkeypatch):
    monkeypatch.setenv("VEKTRA_ADMIN_BOOTSTRAP_KEY", "secret")
    monkeypatch.setenv("VEKTRA_ENV", "development")
    import structlog.testing

    with structlog.testing.capture_logs() as captured:
        warn_if_bootstrap_in_production()
    assert not any("bootstrap_key_set_in_production" in str(e) for e in captured)
