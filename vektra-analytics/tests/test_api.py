"""Unit tests for analytics API router: auth, response format, query parameters."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from vektra_analytics.api import _get_session, router
from vektra_analytics.service import AnalyticsService, MetricsResponse
from vektra_shared.auth import ApiKeyInfo
from vektra_shared.registry import ProviderRegistry
from vektra_shared.types import ChunkRef, QueryTrace, StepTrace

# ---------------------------------------------------------------------------
# Mock key store (same pattern as vektra-shared/tests/test_auth.py)
# ---------------------------------------------------------------------------

_ADMIN_TOKEN = "test-admin-token"
_ADMIN_KEY_ID = uuid4()


class _MockKeyStore:
    async def lookup_by_token(self, token: str) -> ApiKeyInfo | None:
        if token == _ADMIN_TOKEN:
            return ApiKeyInfo(key_id=_ADMIN_KEY_ID, scopes=["admin"])
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_app(
    service: AnalyticsService | None = None,
    with_auth: bool = True,
) -> FastAPI:
    """Create a test app with analytics router and mock auth."""
    app = FastAPI()
    app.include_router(router)

    if with_auth:
        registry = ProviderRegistry()
        registry.register("key_store", "default", _MockKeyStore())
        app.state.registry = registry

    if service is not None:
        app.state.analytics_service = service

    # Override session dependency with a no-op mock (service is fully mocked)
    async def _mock_session():
        return AsyncMock()

    app.dependency_overrides[_get_session] = _mock_session

    return app


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


def _make_trace(
    *,
    response_id=None,
    duration_ms: int = 250,
    llm_model: str = "gpt-4",
) -> QueryTrace:
    return QueryTrace(
        response_id=response_id or uuid4(),
        steps=[
            StepTrace(name="embed_query", duration_ms=10),
            StepTrace(name="llm_call", duration_ms=200, metadata={"model": llm_model}),
        ],
        total_duration_ms=duration_ms,
        chunks_retrieved=[ChunkRef(chunk_id="chunk-1", score=0.9)],
        llm_model=llm_model,
        prompt_version="abc12345",
        created_at=datetime.now(UTC),
    )


def _mock_service() -> MagicMock:
    svc = MagicMock(spec=AnalyticsService)
    svc.list_traces = AsyncMock(return_value=[])
    svc.get_trace = AsyncMock(return_value=None)
    svc.get_metrics = AsyncMock(
        return_value=MetricsResponse(
            total_queries=0,
            avg_latency_ms=0.0,
            p95_latency_ms=0.0,
            avg_retrieval_score=0.0,
            queries_per_hour=0.0,
            model_distribution={},
            period_start=datetime.now(UTC),
            period_end=datetime.now(UTC),
        )
    )
    return svc


# ---------------------------------------------------------------------------
# GET /api/v1/traces
# ---------------------------------------------------------------------------


class TestListTraces:
    def test_list_traces_empty(self):
        svc = _mock_service()
        app = _make_app(service=svc)
        client = TestClient(app)

        resp = client.get("/api/v1/traces", headers=_auth_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["count"] == 0

    def test_list_traces_with_results(self):
        trace = _make_trace(duration_ms=300)
        svc = _mock_service()
        svc.list_traces = AsyncMock(return_value=[trace])

        app = _make_app(service=svc)
        client = TestClient(app)

        resp = client.get("/api/v1/traces", headers=_auth_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        item = data["items"][0]
        assert item["total_duration_ms"] == 300
        assert item["llm_model"] == "gpt-4"
        assert len(item["steps"]) == 2
        assert len(item["chunks_retrieved"]) == 1

    def test_list_traces_query_params(self):
        svc = _mock_service()
        app = _make_app(service=svc)
        client = TestClient(app)

        resp = client.get(
            "/api/v1/traces",
            headers=_auth_headers(),
            params={
                "namespace": "test-ns",
                "model": "claude-3",
                "min_duration_ms": 100,
                "limit": 10,
                "offset": 5,
            },
        )
        assert resp.status_code == 200
        svc.list_traces.assert_awaited_once()
        call_kwargs = svc.list_traces.call_args[1]
        assert call_kwargs["namespace"] == "test-ns"
        assert call_kwargs["llm_model"] == "claude-3"
        assert call_kwargs["min_duration_ms"] == 100
        assert call_kwargs["limit"] == 10
        assert call_kwargs["offset"] == 5

    def test_list_traces_limit_validation(self):
        svc = _mock_service()
        app = _make_app(service=svc)
        client = TestClient(app)

        resp = client.get(
            "/api/v1/traces",
            headers=_auth_headers(),
            params={"limit": 0},
        )
        assert resp.status_code == 422

        resp = client.get(
            "/api/v1/traces",
            headers=_auth_headers(),
            params={"limit": 501},
        )
        assert resp.status_code == 422

    def test_list_traces_service_unavailable(self):
        app = _make_app(service=None)
        client = TestClient(app)

        resp = client.get("/api/v1/traces", headers=_auth_headers())
        assert resp.status_code == 503
        data = resp.json()
        assert "error" in data["detail"]
        assert data["detail"]["error"]["code"] == "ERR-ANALYTICS-001"


# ---------------------------------------------------------------------------
# GET /api/v1/traces/{response_id}
# ---------------------------------------------------------------------------


class TestGetTrace:
    def test_get_trace_found(self):
        rid = uuid4()
        trace = _make_trace(response_id=rid)
        svc = _mock_service()
        svc.get_trace = AsyncMock(return_value=trace)

        app = _make_app(service=svc)
        client = TestClient(app)

        resp = client.get(f"/api/v1/traces/{rid}", headers=_auth_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["response_id"] == str(rid)
        assert data["llm_model"] == "gpt-4"

    def test_get_trace_not_found(self):
        svc = _mock_service()
        svc.get_trace = AsyncMock(return_value=None)

        app = _make_app(service=svc)
        client = TestClient(app)

        resp = client.get(f"/api/v1/traces/{uuid4()}", headers=_auth_headers())
        assert resp.status_code == 404
        data = resp.json()
        assert "error" in data["detail"]
        assert data["detail"]["error"]["code"] == "ERR-ANALYTICS-002"

    def test_get_trace_invalid_uuid(self):
        svc = _mock_service()
        app = _make_app(service=svc)
        client = TestClient(app)

        resp = client.get("/api/v1/traces/not-a-uuid", headers=_auth_headers())
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/metrics
# ---------------------------------------------------------------------------


class TestGetMetrics:
    def test_get_metrics_empty(self):
        svc = _mock_service()
        app = _make_app(service=svc)
        client = TestClient(app)

        resp = client.get("/api/v1/metrics", headers=_auth_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_queries"] == 0
        assert data["avg_latency_ms"] == 0.0
        assert data["p95_latency_ms"] == 0.0

    def test_get_metrics_with_filters(self):
        svc = _mock_service()
        app = _make_app(service=svc)
        client = TestClient(app)

        resp = client.get(
            "/api/v1/metrics",
            headers=_auth_headers(),
            params={"namespace": "test-ns"},
        )
        assert resp.status_code == 200
        svc.get_metrics.assert_awaited_once()
        call_kwargs = svc.get_metrics.call_args[1]
        assert call_kwargs["namespace"] == "test-ns"

    def test_get_metrics_with_data(self):
        now = datetime.now(UTC)
        svc = _mock_service()
        svc.get_metrics = AsyncMock(
            return_value=MetricsResponse(
                total_queries=100,
                avg_latency_ms=250.5,
                p95_latency_ms=480.0,
                avg_retrieval_score=0.85,
                queries_per_hour=50.0,
                model_distribution={"gpt-4": 70, "claude-3": 30},
                period_start=now,
                period_end=now,
            )
        )

        app = _make_app(service=svc)
        client = TestClient(app)

        resp = client.get("/api/v1/metrics", headers=_auth_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_queries"] == 100
        assert data["avg_latency_ms"] == 250.5
        assert data["p95_latency_ms"] == 480.0
        assert data["avg_retrieval_score"] == 0.85
        assert data["queries_per_hour"] == 50.0
        assert data["model_distribution"]["gpt-4"] == 70


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------


class TestAuthEnforcement:
    def test_no_auth_header_returns_401(self):
        svc = _mock_service()
        app = _make_app(service=svc)
        client = TestClient(app)

        # No Authorization header
        assert client.get("/api/v1/traces").status_code == 401
        assert client.get(f"/api/v1/traces/{uuid4()}").status_code == 401
        assert client.get("/api/v1/metrics").status_code == 401

    def test_invalid_token_returns_401(self):
        svc = _mock_service()
        app = _make_app(service=svc)
        client = TestClient(app)

        headers = {"Authorization": "Bearer invalid-token"}
        assert client.get("/api/v1/traces", headers=headers).status_code == 401

    def test_admin_scope_required(self):
        """All analytics endpoints require admin scope."""
        import inspect

        from vektra_analytics.api import get_metrics, get_trace, list_traces

        for fn in [list_traces, get_trace, get_metrics]:
            sig = inspect.signature(fn)
            assert "_key" in sig.parameters, f"{fn.__name__} missing _key param"
