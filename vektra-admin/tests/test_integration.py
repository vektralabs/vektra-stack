"""Integration tests for vektra-admin: full API key lifecycle, audit log, health.

Requires Docker (testcontainers PostgreSQL with pgvector extension).
Automatically skipped when Docker is unavailable.

Coverage:
- Full API key lifecycle: create (bootstrap), list, use, revoke, verify rejected
- Bootstrap key: single-use enforcement (REQ-036)
- Deep health check: returns component array when authenticated (REQ-025)
- GET /admin: returns valid HTML (REQ-006)
- Audit log completeness (NFR-007): N authenticated requests → N audit_log rows

Event loop strategy:
  `asyncio_default_test_loop_scope = "function"` (from pyproject.toml) gives each
  test its own event loop. asyncpg connection pools are bound to the loop that
  first uses them — sharing a pool across loops causes RuntimeError.
  Solution: each test creates a *fresh* AsyncEngine (per-test), matching the
  pattern used in vektra-index integration tests.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.middleware.base import BaseHTTPMiddleware

from vektra_shared.registry import ProviderRegistry


# ---------------------------------------------------------------------------
# Docker availability guard
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    try:
        import docker
        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


# Use module-level event loop for all integration tests to avoid asyncpg
# "attached to a different loop" errors. async fixtures use the module loop
# (asyncio_default_fixture_loop_scope=module in pyproject.toml); forcing tests
# to the same loop keeps asyncpg connections consistent across setup/test/teardown.
pytestmark = [
    pytest.mark.skipif(
        not _docker_available(),
        reason="Docker not available - skipping integration tests",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]


# ---------------------------------------------------------------------------
# Module-scoped fixture: one PostgreSQL container for the entire module
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_url():
    """Start a PostgreSQL container, apply migrations, yield the async URL."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        raw_url = postgres.get_connection_url()
        async_url = (
            raw_url
            .replace("postgresql://", "postgresql+asyncpg://")
            .replace("psycopg2", "asyncpg")
        )

        project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(__file__))
        )
        env = os.environ.copy()
        env["VEKTRA_DATABASE_URL"] = async_url

        result = subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
            cwd=project_root,
            env=env,
        )
        if result.returncode != 0:
            pytest.fail(f"Alembic migration failed:\n{result.stderr}\n{result.stdout}")

        yield async_url


# ---------------------------------------------------------------------------
# Per-test fixtures: fresh engine per test to avoid asyncpg loop contamination
# (same pattern as vektra-index/tests/test_integration.py)
# ---------------------------------------------------------------------------


@pytest.fixture
async def fresh_engine(db_url):
    """Create a fresh AsyncEngine for this test, bound to the test's event loop.

    Overrides vektra_shared.db module-level globals so that get_session()
    FastAPI dependency uses this engine. Disposes the engine after the test.
    """
    import vektra_shared.db as db_mod

    engine = create_async_engine(db_url, pool_size=2, max_overflow=0)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Save and replace module-level engine/factory
    old_engine = db_mod._engine
    old_factory = db_mod._session_factory
    db_mod._engine = engine
    db_mod._session_factory = factory

    yield factory

    # Restore original state and dispose the per-test engine
    db_mod._engine = old_engine
    db_mod._session_factory = old_factory
    await engine.dispose()


@pytest.fixture
async def session(fresh_engine):
    """Per-test AsyncSession from the fresh engine."""
    async with fresh_engine() as s:
        yield s


@pytest.fixture
def registry(fresh_engine):
    """Per-test ProviderRegistry with InMemoryKeyStore (fresh per test)."""
    from vektra_admin.keystore import InMemoryKeyStore

    reg = ProviderRegistry()
    store = InMemoryKeyStore()
    reg.register("key_store", "default", store)
    return reg


@pytest.fixture
def app(registry):
    """Per-test FastAPI app with admin router, AuditMiddleware, RequestIdMiddleware."""
    from vektra_admin.api import router
    from vektra_admin.middleware import AuditMiddleware

    class RequestIdMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            request.state.request_id = uuid4()
            return await call_next(request)

    _app = FastAPI()
    _app.state.registry = registry
    _app.state.version = "test-0.1.0"
    _app.add_middleware(AuditMiddleware)
    _app.add_middleware(RequestIdMiddleware)
    _app.include_router(router)
    return _app


@pytest.fixture
async def client(app):
    """Per-test AsyncClient backed by the test app."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture
async def bootstrap_key(monkeypatch, session, registry):
    """Configure bootstrap key, reset system_state, reload key store, yield key."""
    monkeypatch.setenv("VEKTRA_ADMIN_BOOTSTRAP_KEY", "test-bootstrap-secret-key")

    await session.execute(
        text("UPDATE system_state SET value='false' WHERE key='bootstrap_consumed'")
    )
    await session.commit()

    key_store = registry.get("key_store", "default")
    key_store._by_hash.clear()
    key_store._by_preview.clear()
    await key_store.load_from_db(session)

    yield "test-bootstrap-secret-key"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_admin_key(client, bootstrap_key: str) -> str:
    """Create an admin key via bootstrap and return the plaintext key."""
    resp = await client.post(
        "/api/v1/api-keys",
        json={"label": "test-admin"},
        headers={"Authorization": f"Bearer {bootstrap_key}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


async def test_create_key_with_bootstrap(client, bootstrap_key):
    """POST /api/v1/api-keys with bootstrap key returns plaintext key once (REQ-020)."""
    resp = await client.post(
        "/api/v1/api-keys",
        json={"label": "first-key", "scopes": ["admin"]},
        headers={"Authorization": f"Bearer {bootstrap_key}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "key" in body
    assert len(body["key"]) > 20
    assert body["key_preview"] == body["key"][-4:]
    assert body["label"] == "first-key"
    assert body["scopes"] == ["admin"]


async def test_bootstrap_key_single_use(client, bootstrap_key):
    """Second request with bootstrap key must return 401 (REQ-036)."""
    resp1 = await client.post(
        "/api/v1/api-keys",
        json={"label": "bootstrap-test"},
        headers={"Authorization": f"Bearer {bootstrap_key}"},
    )
    assert resp1.status_code == 201, resp1.text

    resp2 = await client.post(
        "/api/v1/api-keys",
        json={"label": "second-attempt"},
        headers={"Authorization": f"Bearer {bootstrap_key}"},
    )
    assert resp2.status_code == 401, (
        f"Bootstrap key should be rejected on second use; got {resp2.status_code}: {resp2.text}"
    )


async def test_list_keys_requires_admin(client, bootstrap_key):
    """GET /api/v1/api-keys returns key list. Requires admin scope."""
    admin_key = await _create_admin_key(client, bootstrap_key)

    resp = await client.get(
        "/api/v1/api-keys",
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert resp.status_code == 200, resp.text
    keys = resp.json()
    assert isinstance(keys, list)
    assert len(keys) >= 1
    # Plaintext key must NOT appear (only preview of 4 chars)
    for k in keys:
        assert len(k.get("key_preview", "")) == 4


async def test_revoke_key(client, bootstrap_key, session):
    """DELETE /api/v1/api-keys/{id} marks the key revoked; revoked key gets 401."""
    admin_key = await _create_admin_key(client, bootstrap_key)

    # Create a second key to revoke
    resp_create = await client.post(
        "/api/v1/api-keys",
        json={"label": "to-revoke"},
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert resp_create.status_code == 201, resp_create.text
    target_key = resp_create.json()["key"]
    target_id = resp_create.json()["id"]

    # Revoke it
    resp_revoke = await client.delete(
        f"/api/v1/api-keys/{target_id}",
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert resp_revoke.status_code == 204, resp_revoke.text

    # Revoked key must be rejected on next use
    resp_use = await client.get(
        "/api/v1/api-keys",
        headers={"Authorization": f"Bearer {target_key}"},
    )
    assert resp_use.status_code == 401, (
        f"Revoked key should return 401; got {resp_use.status_code}: {resp_use.text}"
    )


async def test_revoke_same_key_twice_returns_409(client, bootstrap_key):
    """Revoking an already-revoked key returns 409."""
    admin_key = await _create_admin_key(client, bootstrap_key)

    resp_create = await client.post(
        "/api/v1/api-keys",
        json={"label": "double-revoke"},
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert resp_create.status_code == 201
    target_id = resp_create.json()["id"]

    await client.delete(
        f"/api/v1/api-keys/{target_id}",
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    resp2 = await client.delete(
        f"/api/v1/api-keys/{target_id}",
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert resp2.status_code == 409


async def test_health_shallow_unauthenticated(client):
    """GET /health returns {status, timestamp} without Bearer token (REQ-025)."""
    resp = await client.get("/health")
    assert resp.status_code in (200, 503)
    body = resp.json()
    assert "status" in body
    assert "timestamp" in body
    # Shallow: must NOT expose component names or internal details
    assert "components" not in body
    assert "version" not in body


async def test_health_deep_requires_token(client, bootstrap_key):
    """GET /health?detail=full returns component breakdown when authenticated (REQ-025)."""
    admin_key = await _create_admin_key(client, bootstrap_key)

    resp = await client.get(
        "/health?detail=full",
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert resp.status_code in (200, 503)
    body = resp.json()
    assert "status" in body
    assert "timestamp" in body
    assert "version" in body
    assert "components" in body
    assert isinstance(body["components"], list)


async def test_health_deep_without_token_returns_401(client):
    """GET /health?detail=full without token must return 401."""
    resp = await client.get("/health?detail=full")
    assert resp.status_code == 401


async def test_admin_dashboard_returns_html(client, bootstrap_key):
    """GET /admin returns valid HTML. Requires Bearer token (REQ-006)."""
    admin_key = await _create_admin_key(client, bootstrap_key)

    resp = await client.get(
        "/admin",
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert resp.status_code == 200
    content_type = resp.headers.get("content-type", "")
    assert "text/html" in content_type
    assert "<html" in resp.text.lower()
    assert "vektra" in resp.text.lower()
    assert any(s in resp.text for s in ("healthy", "degraded", "unhealthy"))


async def test_audit_log_completeness(client, bootstrap_key, session):
    """N authenticated requests produce N audit_log rows (NFR-007)."""
    admin_key = await _create_admin_key(client, bootstrap_key)

    result = await session.execute(text("SELECT COUNT(*) FROM audit_log"))
    before = result.scalar()

    n = 3
    for _ in range(n):
        resp = await client.get(
            "/api/v1/api-keys",
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        assert resp.status_code == 200

    # Allow fire-and-forget audit tasks to complete
    await asyncio.sleep(0.2)

    result = await session.execute(text("SELECT COUNT(*) FROM audit_log"))
    after = result.scalar()

    assert after - before >= n, (
        f"Expected at least {n} new audit_log rows; got {after - before}"
    )


async def test_no_auth_returns_error_envelope(client):
    """Unauthenticated requests return REQ-010 error envelope with ERR-AUTH-001."""
    resp = await client.get("/api/v1/api-keys")
    assert resp.status_code == 401
    body = resp.json()
    detail = body.get("detail", {})
    assert "error" in detail
    assert detail["error"]["code"] == "ERR-AUTH-001"
