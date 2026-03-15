"""Unit tests for admin UI: auth dependency, login flow, secret masking, template rendering."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from vektra_admin.ui import (
    _COOKIE_NAME,
    _mask_value,
    get_static_files,
    register_ui_exception_handlers,
    ui_router,
)
from vektra_shared.auth import ApiKeyInfo
from vektra_shared.registry import ProviderRegistry

# ---------------------------------------------------------------------------
# Test app factory
# ---------------------------------------------------------------------------


def _make_app(*, admin_token: str = "test-admin-token") -> FastAPI:
    """Create a minimal FastAPI app with the UI router and a mock key store."""
    app = FastAPI()

    # Mock key store
    key_store = AsyncMock()
    admin_key_info = ApiKeyInfo(key_id=uuid4(), scopes=["admin"], rate_limit_rpm=None)
    non_admin_key_info = ApiKeyInfo(
        key_id=uuid4(), scopes=["query"], rate_limit_rpm=None
    )

    async def _lookup(token: str) -> ApiKeyInfo | None:
        if token == admin_token:
            return admin_key_info
        if token == "query-only-token":
            return non_admin_key_info
        return None

    key_store.lookup_by_token = AsyncMock(side_effect=_lookup)

    registry = ProviderRegistry()
    registry.register("key_store", "default", key_store)

    app.state.registry = registry
    app.state.version = "0.2.0-test"

    # Mount UI router and static files
    app.mount("/admin/static", get_static_files(), name="admin-static")
    app.include_router(ui_router)
    register_ui_exception_handlers(app)

    return app


def _client(app: FastAPI | None = None) -> AsyncClient:
    if app is None:
        app = _make_app()
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    )


# ---------------------------------------------------------------------------
# Secret masking tests
# ---------------------------------------------------------------------------


def test_mask_value_masks_key():
    assert _mask_value("VEKTRA_API_KEY", "secret123") == "****"


def test_mask_value_masks_secret():
    assert _mask_value("VEKTRA_DB_SECRET", "s3cr3t") == "****"


def test_mask_value_masks_password():
    assert _mask_value("VEKTRA_DB_PASSWORD", "hunter2") == "****"


def test_mask_value_masks_token():
    assert _mask_value("VEKTRA_BOOTSTRAP_TOKEN", "abc") == "****"


def test_mask_value_keeps_normal():
    assert _mask_value("VEKTRA_LLM_PROVIDER", "ollama/llama3") == "ollama/llama3"


def test_mask_value_case_insensitive():
    assert _mask_value("vektra_api_key", "x") == "****"


# ---------------------------------------------------------------------------
# Login flow tests
# ---------------------------------------------------------------------------


async def test_login_page_renders():
    async with _client() as client:
        resp = await client.get("/admin/login")
    assert resp.status_code == 200
    assert "Vektra admin" in resp.text
    assert '<input type="password"' in resp.text


async def test_login_valid_token_sets_cookie():
    async with _client() as client:
        resp = await client.post(
            "/admin/login",
            data={"token": "test-admin-token"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/"
    assert _COOKIE_NAME in resp.cookies


async def test_login_invalid_token_redirects_with_error():
    async with _client() as client:
        resp = await client.post(
            "/admin/login",
            data={"token": "bad-token"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "error=invalid" in resp.headers["location"]


async def test_login_non_admin_token_rejected():
    async with _client() as client:
        resp = await client.post(
            "/admin/login",
            data={"token": "query-only-token"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "error=invalid" in resp.headers["location"]


async def test_login_empty_token_redirects():
    async with _client() as client:
        resp = await client.post(
            "/admin/login",
            data={"token": ""},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "error=missing" in resp.headers["location"]


async def test_logout_clears_cookie():
    async with _client() as client:
        resp = await client.get("/admin/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


# ---------------------------------------------------------------------------
# Auth dependency tests
# ---------------------------------------------------------------------------


async def test_unauthenticated_redirects_to_login():
    async with _client() as client:
        resp = await client.get("/admin/", follow_redirects=False)
    assert resp.status_code == 303
    assert "/admin/login" in resp.headers["location"]


async def test_auth_via_cookie():
    """Admin pages accessible with a valid cookie."""
    app = _make_app()

    # We need to mock health.check_all to avoid registry issues
    with patch("vektra_admin.ui._health") as mock_health:
        mock_health.check_all = AsyncMock(
            return_value=(
                MagicMock(status="healthy"),
                MagicMock(status="healthy", timestamp="2026-03-06", components=[]),
            )
        )
        mock_health.check_memory = MagicMock(
            return_value=MagicMock(rss_mb=100.0, vms_mb=200.0, percent=5.0)
        )
        async with _client(app) as client:
            client.cookies.set(_COOKIE_NAME, "test-admin-token")
            resp = await client.get("/admin/")
    assert resp.status_code == 200
    assert "Health dashboard" in resp.text


async def test_query_param_token_rejected():
    """Query param token no longer grants access (cookie-only auth)."""
    async with _client() as client:
        resp = await client.get(
            "/admin/?token=test-admin-token", follow_redirects=False
        )
    assert resp.status_code == 303
    assert "/admin/login" in resp.headers["location"]


async def test_auth_invalid_token_redirects():
    """Invalid token in cookie redirects to login."""
    async with _client() as client:
        client.cookies.set(_COOKIE_NAME, "bad-token")
        resp = await client.get("/admin/", follow_redirects=False)
    assert resp.status_code == 303


# ---------------------------------------------------------------------------
# Config page: secret masking in rendered output
# ---------------------------------------------------------------------------


async def test_config_page_masks_secrets():
    """Config page renders with masked secret values."""
    app = _make_app()
    env_patch = {
        "VEKTRA_LLM_PROVIDER": "ollama/llama3",
        "VEKTRA_API_KEY": "super-secret",
        "VEKTRA_DB_PASSWORD": "hunter2",
    }
    with (
        patch.dict("os.environ", env_patch, clear=False),
        patch("vektra_admin.ui.os.environ", env_patch),
    ):
        async with _client(app) as client:
            client.cookies.set(_COOKIE_NAME, "test-admin-token")
            resp = await client.get("/admin/config")
    assert resp.status_code == 200
    assert "ollama/llama3" in resp.text
    assert "super-secret" not in resp.text
    assert "hunter2" not in resp.text
    assert "****" in resp.text


# ---------------------------------------------------------------------------
# Health page: renders component data
# ---------------------------------------------------------------------------


async def test_health_page_renders_components():
    app = _make_app()
    component = MagicMock(name="llm", status="healthy", latency_ms=42, message=None)
    component.name = "llm"
    with patch("vektra_admin.ui._health") as mock_health:
        mock_health.check_all = AsyncMock(
            return_value=(
                MagicMock(status="healthy"),
                MagicMock(
                    status="healthy",
                    timestamp="2026-03-06T12:00:00Z",
                    components=[component],
                ),
            )
        )
        mock_health.check_memory = MagicMock(
            return_value=MagicMock(rss_mb=50.0, vms_mb=150.0, percent=3.0)
        )
        async with _client(app) as client:
            client.cookies.set(_COOKIE_NAME, "test-admin-token")
            resp = await client.get("/admin/")
    assert resp.status_code == 200
    assert "llm" in resp.text
    assert "every 30s" in resp.text


async def test_health_partial_returns_table():
    app = _make_app()
    component = MagicMock(name="db", status="healthy", latency_ms=5, message=None)
    component.name = "db"
    with patch("vektra_admin.ui._health") as mock_health:
        mock_health.check_all = AsyncMock(
            return_value=(
                MagicMock(status="healthy"),
                MagicMock(
                    status="healthy",
                    timestamp="2026-03-06T12:00:00Z",
                    components=[component],
                ),
            )
        )
        async with _client(app) as client:
            client.cookies.set(_COOKIE_NAME, "test-admin-token")
            resp = await client.get("/admin/partials/health")
    assert resp.status_code == 200
    assert "<table>" in resp.text
    assert "db" in resp.text
