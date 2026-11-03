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


# ---------------------------------------------------------------------------
# require_exact_scope: no admin superscope (plan assess-foundations 4.2, NFR-014)
# ---------------------------------------------------------------------------


def _exact_app(store: MockKeyStore) -> FastAPI:
    from vektra_shared.auth import require_exact_scope

    app = FastAPI()
    registry = ProviderRegistry()
    registry.register("key_store", "default", store)
    app.state.registry = registry

    @app.get("/mint")
    async def mint(key: ApiKeyInfo = Depends(require_exact_scope("assess:mint"))):
        return {"key_id": str(key.key_id)}

    register_error_handlers(app)
    return app


class TestRequireExactScope:
    def _get(self, scopes: list[str]):
        key_id = uuid4()
        store = MockKeyStore({"t": ApiKeyInfo(key_id=key_id, scopes=scopes)})
        resp = TestClient(_exact_app(store)).get(
            "/mint", headers={"Authorization": "Bearer t"}
        )
        return resp, key_id

    def test_admin_only_key_rejected(self):
        resp, _ = self._get(["admin"])
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == ERR_AUTH_003

    def test_admin_with_every_legacy_scope_rejected(self):
        resp, _ = self._get(["admin", "ingest", "query"])
        assert resp.status_code == 403

    def test_exact_scope_passes(self):
        resp, key_id = self._get(["assess:mint"])
        assert resp.status_code == 200
        assert resp.json()["key_id"] == str(key_id)

    def test_other_assess_scope_does_not_satisfy(self):
        resp, _ = self._get(["assess:teacher"])
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == ERR_AUTH_003

    def test_missing_token_401(self):
        resp = TestClient(_exact_app(MockKeyStore({}))).get("/mint")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == ERR_AUTH_001

    def test_require_scope_still_honours_admin(self):
        """The superscope is removed only for exact scopes, not globally."""
        store = MockKeyStore({"t": ApiKeyInfo(key_id=uuid4(), scopes=["admin"])})
        resp = TestClient(_make_app(store)).get(
            "/query-only", headers={"Authorization": "Bearer t"}
        )
        assert resp.status_code == 200

    def test_dependency_names(self):
        from vektra_shared.auth import require_exact_scope

        assert (
            require_exact_scope("assess:mint").__name__
            == "require_exact_scope_assess:mint"
        )
        assert require_scope("admin").__name__ == "require_scope_admin"


class TestScopeSetError:
    def test_valid_sets(self):
        from vektra_shared.auth import scope_set_error

        assert scope_set_error(["admin", "query"]) is None
        assert scope_set_error(["assess:mint", "assess:privacy-erase"]) is None

    def test_invalid_and_empty(self):
        from vektra_shared.auth import scope_set_error

        assert "Invalid scopes" in (scope_set_error(["root"]) or "")
        assert scope_set_error([]) is not None

    def test_mixed_refused_for_every_pair(self):
        from vektra_shared.auth import ASSESS_SCOPES, LEGACY_SCOPES, scope_set_error

        for legacy in LEGACY_SCOPES:
            for assess in ASSESS_SCOPES:
                msg = scope_set_error([legacy, assess])
                assert msg is not None and "ck_api_keys_assess_scope_exclusive" in msg

    def test_model_constraints_mirror_the_vocabulary(self):
        """vektra_admin's ORM CHECKs name exactly ALL_SCOPES (migration 0009 mirror)."""
        import re
        from pathlib import Path

        from vektra_shared.auth import ALL_SCOPES, ASSESS_SCOPES

        root = Path(__file__).resolve().parents[2]
        for src in (
            root / "vektra-admin/src/vektra_admin/models.py",
            root / "migrations/versions/0009_assess_scopes_roles.py",
        ):
            text = src.read_text(encoding="utf-8")
            found = set(re.findall(r"'((?:admin|ingest|query|assess:[a-z-]+))'", text))
            assert found == set(ALL_SCOPES), (src.name, found ^ set(ALL_SCOPES))
            assert ASSESS_SCOPES <= found
