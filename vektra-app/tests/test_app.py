"""Tests for vektra_app.main (ARCH-015, ARCH-057).

Unit tests cover:
- Application factory (create_app)
- Correlation ID middleware
- Global exception handler
- Structlog PII redaction
- CORS origin parsing
- Startup validation error handling (step 1)

Integration tests requiring PostgreSQL are in test_app_integration.py.
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vektra_app.main import (
    _get_cors_origins,
    _pii_redactor,
    configure_structlog,
    create_app,
)
from vektra_shared.registry import ProviderRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def app() -> FastAPI:
    """Create a test app without starting the lifespan.

    Sets app.state.registry to an empty ProviderRegistry so that
    route handlers (e.g., /health) don't crash with NoneType errors.
    """
    test_app = create_app()
    test_app.state.registry = ProviderRegistry()
    test_app.state.version = "test"
    return test_app


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    """TestClient that skips lifespan (no database needed for unit tests)."""
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# PII redactor
# ---------------------------------------------------------------------------


class TestPiiRedactor:
    def test_redacts_sensitive_fields_at_warning_level(self) -> None:
        event_dict = {
            "query": "what is my SSN",
            "answer": "your SSN is 123-45-6789",
            "other": "safe",
        }
        result = _pii_redactor(None, "warning", event_dict)
        assert result["query"] == "[REDACTED]"
        assert result["answer"] == "[REDACTED]"
        assert result["other"] == "safe"

    def test_redacts_sensitive_fields_at_error_level(self) -> None:
        event_dict = {"question": "test query", "response_text": "test response"}
        result = _pii_redactor(None, "error", event_dict)
        assert result["question"] == "[REDACTED]"
        assert result["response_text"] == "[REDACTED]"

    def test_preserves_fields_at_info_level(self) -> None:
        event_dict = {"query": "test query", "answer": "test answer"}
        result = _pii_redactor(None, "info", event_dict)
        assert result["query"] == "test query"
        assert result["answer"] == "test answer"

    def test_preserves_fields_at_debug_level(self) -> None:
        event_dict = {"query": "test query"}
        result = _pii_redactor(None, "debug", event_dict)
        assert result["query"] == "test query"


# ---------------------------------------------------------------------------
# CORS origins
# ---------------------------------------------------------------------------


class TestCorsOrigins:
    def test_default_origins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VEKTRA_CORS_ORIGINS", raising=False)
        origins = _get_cors_origins()
        assert origins == ["http://localhost:3000"]

    def test_custom_origins(self) -> None:
        with patch.dict(
            os.environ,
            {
                "VEKTRA_CORS_ORIGINS": "https://app.example.com,https://admin.example.com"
            },
        ):
            origins = _get_cors_origins()
            assert origins == ["https://app.example.com", "https://admin.example.com"]

    def test_empty_string_gives_default(self) -> None:
        with patch.dict(os.environ, {"VEKTRA_CORS_ORIGINS": ""}):
            origins = _get_cors_origins()
            assert origins == ["http://localhost:3000"]


# ---------------------------------------------------------------------------
# Structlog configuration
# ---------------------------------------------------------------------------


class TestStructlogConfiguration:
    def test_configure_does_not_raise(self) -> None:
        configure_structlog()


# ---------------------------------------------------------------------------
# Correlation ID middleware
# ---------------------------------------------------------------------------


class TestCorrelationIdMiddleware:
    def test_generates_id_when_absent(self, client: TestClient) -> None:
        """Requests without X-Request-ID get one generated."""
        response = client.get("/metrics")
        assert "X-Request-ID" in response.headers
        uuid.UUID(response.headers["X-Request-ID"])

    def test_propagates_id_from_header(self, client: TestClient) -> None:
        """Requests with X-Request-ID have it propagated."""
        test_id = str(uuid.uuid4())
        response = client.get("/metrics", headers={"X-Request-ID": test_id})
        assert response.headers["X-Request-ID"] == test_id

    def test_invalid_uuid_generates_new_id(self, client: TestClient) -> None:
        """Invalid X-Request-ID values are replaced with a new UUID."""
        response = client.get("/metrics", headers={"X-Request-ID": "not-a-uuid"})
        returned_id = response.headers["X-Request-ID"]
        assert returned_id != "not-a-uuid"
        uuid.UUID(returned_id)


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------


class TestGlobalExceptionHandler:
    def test_unhandled_exception_returns_error_envelope(
        self, app: FastAPI, client: TestClient
    ) -> None:
        """Unhandled exceptions return ERR-CONFIG-001 envelope, not stack traces."""

        @app.get("/test-error")
        async def _raise_error() -> None:
            raise RuntimeError("unexpected failure")

        response = client.get("/test-error")
        assert response.status_code == 500
        body = response.json()
        assert "error" in body
        assert body["error"]["code"] == "ERR-CONFIG-001"
        assert body["error"]["remediation"]
        assert "Traceback" not in str(body)


# ---------------------------------------------------------------------------
# Startup validation: config failure (step 1)
# ---------------------------------------------------------------------------


class TestStartupConfigValidation:
    def test_missing_llm_provider_aborts_startup(self) -> None:
        """Missing VEKTRA_LLM_PROVIDER prevents startup.

        The lifespan raises SystemExit(1), which the TestClient's event loop
        propagates as CancelledError (BaseException wrapping).
        """
        import concurrent.futures

        test_app = create_app()
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("VEKTRA_")}
        with patch.dict(os.environ, clean_env, clear=True):
            with pytest.raises((SystemExit, concurrent.futures.CancelledError)):
                with TestClient(test_app):
                    pass


# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------


class TestRouterRegistration:
    def test_health_endpoint_exists(self, client: TestClient) -> None:
        """GET /health is registered (from vektra_admin router)."""
        response = client.get("/health")
        # Health returns 200 with empty registry (no health checks registered)
        assert response.status_code == 200

    def test_metrics_endpoint_exists(self, client: TestClient) -> None:
        """GET /metrics is registered (prometheus)."""
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "HELP" in response.text or "TYPE" in response.text

    def test_docs_endpoint_exists(self, client: TestClient) -> None:
        """GET /docs is available (FastAPI OpenAPI UI)."""
        response = client.get("/docs")
        assert response.status_code == 200
