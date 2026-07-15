"""Tests for auth middleware (REQ-019, REQ-030, REQ-031, REQ-041)."""

from uuid import uuid4

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from vektra_shared.auth import ApiKeyInfo, require_scope
from vektra_shared.errors import ERR_AUTH_001, ERR_AUTH_003
from vektra_shared.http_errors import register_error_handlers
from vektra_shared.registry import ProviderRegistry


def _make_app(key_store=None, register_store: bool = True) -> FastAPI:
    """Build a minimal FastAPI app wired with the auth middleware."""
    app = FastAPI()
    registry = ProviderRegistry()
    if register_store and key_store is not None:
        registry.register("key_store", "default", key_store)
    app.state.registry = registry

    @app.get("/admin-only")
    async def admin_only(key: ApiKeyInfo = Depends(require_scope("admin"))):
        return {"key_id": str(key.key_id), "scopes": key.scopes}

    @app.get("/query-only")
    async def query_only(key: ApiKeyInfo = Depends(require_scope("query"))):
        return {"key_id": str(key.key_id)}

    register_error_handlers(app)
    return app


class MockKeyStore:
    """In-memory key store for testing."""

    def __init__(self, keys: dict[str, ApiKeyInfo]):
        self._keys = keys

    async def lookup_by_token(self, token: str) -> ApiKeyInfo | None:
        return self._keys.get(token)


class TestRequireScope:
    def test_valid_admin_token_passes(self):
        key_id = uuid4()
        store = MockKeyStore(
            {"valid-token": ApiKeyInfo(key_id=key_id, scopes=["admin", "query"])}
        )
        client = TestClient(_make_app(store))
        response = client.get(
            "/admin-only", headers={"Authorization": "Bearer valid-token"}
        )
        assert response.status_code == 200
        assert response.json()["key_id"] == str(key_id)

    def test_missing_authorization_header_returns_401(self):
        store = MockKeyStore({})
        client = TestClient(_make_app(store))
        response = client.get("/admin-only")
        assert response.status_code == 401
        # REQ-010 envelope at the document root (DEBT-034)
        body = response.json()
        assert body["error"]["code"] == ERR_AUTH_001

    def test_invalid_token_returns_401(self):
        store = MockKeyStore({})
        client = TestClient(_make_app(store))
        response = client.get(
            "/admin-only", headers={"Authorization": "Bearer bad-token"}
        )
        assert response.status_code == 401
        body = response.json()
        assert body["error"]["code"] == ERR_AUTH_001

    def test_revoked_token_returns_401(self):
        # A revoked token would not be in the key store, same as invalid
        store = MockKeyStore({})
        client = TestClient(_make_app(store))
        response = client.get(
            "/admin-only", headers={"Authorization": "Bearer revoked"}
        )
        assert response.status_code == 401

    def test_wrong_scope_returns_403(self):
        # Token is valid but only has "query" scope, not "admin"
        store = MockKeyStore(
            {"query-token": ApiKeyInfo(key_id=uuid4(), scopes=["query"])}
        )
        client = TestClient(_make_app(store))
        response = client.get(
            "/admin-only", headers={"Authorization": "Bearer query-token"}
        )
        assert response.status_code == 403
        body = response.json()
        assert body["error"]["code"] == ERR_AUTH_003
        assert "admin" in body["error"]["message"]

    def test_correct_scope_on_query_endpoint(self):
        store = MockKeyStore({"q-token": ApiKeyInfo(key_id=uuid4(), scopes=["query"])})
        client = TestClient(_make_app(store))
        response = client.get(
            "/query-only", headers={"Authorization": "Bearer q-token"}
        )
        assert response.status_code == 200

    def test_multi_scope_key_can_access_both_endpoints(self):
        key_id = uuid4()
        store = MockKeyStore(
            {
                "full-token": ApiKeyInfo(
                    key_id=key_id, scopes=["admin", "ingest", "query"]
                )
            }
        )
        client = TestClient(_make_app(store))
        assert (
            client.get(
                "/admin-only", headers={"Authorization": "Bearer full-token"}
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/query-only", headers={"Authorization": "Bearer full-token"}
            ).status_code
            == 200
        )

    def test_admin_superscope_accesses_query_endpoint(self):
        """Admin keys can access query-scoped endpoints (ARCH-059)."""
        store = MockKeyStore(
            {"admin-token": ApiKeyInfo(key_id=uuid4(), scopes=["admin"])}
        )
        client = TestClient(_make_app(store))
        response = client.get(
            "/query-only", headers={"Authorization": "Bearer admin-token"}
        )
        assert response.status_code == 200

    def test_ingest_scope_cannot_access_query_endpoint(self):
        """Ingest keys cannot access query-scoped endpoints."""
        store = MockKeyStore(
            {"ingest-token": ApiKeyInfo(key_id=uuid4(), scopes=["ingest"])}
        )
        client = TestClient(_make_app(store))
        response = client.get(
            "/query-only", headers={"Authorization": "Bearer ingest-token"}
        )
        assert response.status_code == 403

    def test_query_scope_cannot_access_admin_endpoint(self):
        """Query keys cannot access admin-scoped endpoints (admin is not a sub-scope)."""
        store = MockKeyStore(
            {"query-token": ApiKeyInfo(key_id=uuid4(), scopes=["query"])}
        )
        client = TestClient(_make_app(store))
        response = client.get(
            "/admin-only", headers={"Authorization": "Bearer query-token"}
        )
        assert response.status_code == 403

    def test_require_scope_none_accepts_any_scope(self):
        """require_scope(None) accepts any valid token regardless of scope."""
        app = FastAPI()
        registry = ProviderRegistry()
        store = MockKeyStore(
            {"ingest-token": ApiKeyInfo(key_id=uuid4(), scopes=["ingest"])}
        )
        registry.register("key_store", "default", store)
        app.state.registry = registry

        @app.get("/any-scope")
        async def any_scope(key: ApiKeyInfo = Depends(require_scope(None))):
            return {"scopes": key.scopes}

        client = TestClient(app)
        response = client.get(
            "/any-scope", headers={"Authorization": "Bearer ingest-token"}
        )
        assert response.status_code == 200

    def test_no_registry_on_app_state_returns_500(self):
        app = FastAPI()
        # No registry set on app.state

        @app.get("/protected")
        async def protected(key: ApiKeyInfo = Depends(require_scope("admin"))):
            return {}

        client = TestClient(app)
        response = client.get(
            "/protected", headers={"Authorization": "Bearer anything"}
        )
        assert response.status_code == 500
