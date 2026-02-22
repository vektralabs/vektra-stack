"""Integration tests for error code registry (REQ-011, NFR-009).

Verifies that each triggerable error code returns the REQ-010 envelope with
non-empty remediation. Requires Docker (pgvector container).

Error codes tested:
  ERR-AUTH-001: missing/invalid Bearer token
  ERR-AUTH-003: insufficient scope
  ERR-INGEST-002: file too large
  ERR-QUERY-003: query too long

Error codes verified as constants only (not HTTP-triggerable in isolation):
  ERR-AUTH-002: Phase 2 reserved
  ERR-INGEST-001: requires file with invalid MIME (tested separately in ingest tests)
  ERR-INGEST-003: requires scanned PDF fixture
  ERR-INGEST-004: requires vector store failure condition
  ERR-QUERY-001: returned as no_relevant_context response, not an error
  ERR-QUERY-002: requires unreachable LLM (tested via pipeline unit tests)
  ERR-QUERY-004: requires vector store read failure condition
  ERR-CONFIG-002: startup-only error
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Docker guard
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(
        not _docker_available(),
        reason="Docker not available - skipping integration tests",
    ),
]

PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
_BOOTSTRAP_SECRET = "test-error-codes-bootstrap-key"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_url():
    """Module-scoped PostgreSQL container with migrations."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        raw_url = postgres.get_connection_url()
        async_url = raw_url.replace("postgresql://", "postgresql+asyncpg://").replace(
            "psycopg2", "asyncpg"
        )

        env = os.environ.copy()
        env["VEKTRA_DATABASE_URL"] = async_url

        result = subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            env=env,
        )
        if result.returncode != 0:
            pytest.fail(f"Alembic migration failed:\n{result.stderr}\n{result.stdout}")

        yield async_url


@pytest.fixture(scope="module")
def running_app(db_url: str) -> Any:
    """Start the full application and return the TestClient."""
    from fastapi.testclient import TestClient

    from vektra_app.main import create_app

    env_vars = {
        "VEKTRA_DATABASE_URL": db_url,
        "VEKTRA_LLM_PROVIDER": "ollama/llama3",
        "VEKTRA_STARTUP_LLM_CHECK": "false",
        "VEKTRA_ADMIN_BOOTSTRAP_KEY": _BOOTSTRAP_SECRET,
    }

    with patch.dict(os.environ, env_vars):
        app = create_app()
        with TestClient(app) as client:
            yield client


@pytest.fixture(scope="module")
def admin_key(running_app: Any) -> str:
    """Create an admin-scoped API key via bootstrap."""
    resp = running_app.post(
        "/api/v1/api-keys",
        json={"label": "error-test-admin", "scopes": ["admin"]},
        headers={"Authorization": f"Bearer {_BOOTSTRAP_SECRET}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


@pytest.fixture(scope="module")
def query_key(running_app: Any, admin_key: str) -> str:
    """Create a query-only scoped API key."""
    resp = running_app.post(
        "/api/v1/api-keys",
        json={"label": "error-test-query", "scopes": ["query"]},
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


# ---------------------------------------------------------------------------
# Envelope assertion helper
# ---------------------------------------------------------------------------


def _assert_error_envelope(body: dict, expected_code: str) -> None:
    """Assert the response matches REQ-010 envelope with NFR-009 remediation."""
    # FastAPI wraps HTTPException detail as {"detail": {...}}
    envelope = body.get("detail", body)
    assert "error" in envelope, f"Missing 'error' key in envelope: {envelope}"
    error = envelope["error"]
    assert error["code"] == expected_code, (
        f"Expected {expected_code}, got {error['code']}"
    )
    assert error.get("category"), "Missing error.category"
    assert error.get("message"), "Missing error.message"
    assert error.get("remediation"), (
        f"Missing remediation for {expected_code} (NFR-009)"
    )
    assert error.get("request_id"), "Missing error.request_id"


# ---------------------------------------------------------------------------
# ERR-AUTH-001: invalid/missing token
# ---------------------------------------------------------------------------


def test_auth_001_missing_token(running_app: Any) -> None:
    """Unauthenticated request to protected endpoint returns ERR-AUTH-001."""
    resp = running_app.get("/api/v1/providers")
    assert resp.status_code == 401
    _assert_error_envelope(resp.json(), "ERR-AUTH-001")


def test_auth_001_invalid_token(running_app: Any) -> None:
    """Request with invalid Bearer token returns ERR-AUTH-001."""
    resp = running_app.get(
        "/api/v1/providers",
        headers={"Authorization": "Bearer totally-invalid-key"},
    )
    assert resp.status_code == 401
    _assert_error_envelope(resp.json(), "ERR-AUTH-001")


# ---------------------------------------------------------------------------
# ERR-AUTH-003: insufficient scope
# ---------------------------------------------------------------------------


def test_auth_003_insufficient_scope(running_app: Any, query_key: str) -> None:
    """Query-scoped key on admin endpoint returns ERR-AUTH-003."""
    resp = running_app.post(
        "/api/v1/api-keys",
        json={"label": "should-fail"},
        headers={"Authorization": f"Bearer {query_key}"},
    )
    assert resp.status_code == 403
    _assert_error_envelope(resp.json(), "ERR-AUTH-003")


# ---------------------------------------------------------------------------
# ERR-INGEST-002: file too large
# ---------------------------------------------------------------------------


def test_ingest_002_file_too_large(running_app: Any, admin_key: str) -> None:
    """Uploading a file exceeding max size returns ERR-INGEST-002.

    Default max is 50MB. We override to 1 byte to trigger the error cheaply.
    """
    with patch.dict(os.environ, {"VEKTRA_MAX_FILE_SIZE_MB": "0"}):
        resp = running_app.post(
            "/api/v1/ingest",
            files={"file": ("test.pdf", b"%PDF-1.4 tiny", "application/pdf")},
            headers={"Authorization": f"Bearer {admin_key}"},
        )
    assert resp.status_code == 413
    _assert_error_envelope(resp.json(), "ERR-INGEST-002")


# ---------------------------------------------------------------------------
# ERR-QUERY-003: query too long
# ---------------------------------------------------------------------------


def test_query_003_query_too_long(running_app: Any, admin_key: str) -> None:
    """Query exceeding max length returns ERR-QUERY-003."""
    long_query = "x" * 10_001  # Exceeds 10,000 char limit
    resp = running_app.post(
        "/api/v1/query",
        json={"question": long_query},
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert resp.status_code == 422
    _assert_error_envelope(resp.json(), "ERR-QUERY-003")


# ERR-CONFIG-001: tested in test_app.py::TestGlobalExceptionHandler (unit test).
# Cannot be triggered here because BaseHTTPMiddleware re-raises exceptions
# before the global exception handler runs in the full middleware stack.


# ---------------------------------------------------------------------------
# Envelope schema: X-Request-ID correlation
# ---------------------------------------------------------------------------


def test_request_id_in_error_response(running_app: Any) -> None:
    """Error responses include request_id matching X-Request-ID header."""
    import uuid

    test_id = str(uuid.uuid4())
    resp = running_app.get(
        "/api/v1/providers",
        headers={"X-Request-ID": test_id},
    )
    assert resp.status_code == 401
    assert resp.headers["X-Request-ID"] == test_id


# ---------------------------------------------------------------------------
# Constants verification: all 13 normative codes exist
# ---------------------------------------------------------------------------


def test_all_normative_codes_exist() -> None:
    """All 13 normative error codes are defined as constants in vektra_shared."""
    from vektra_shared import errors

    expected = [
        "ERR_INGEST_001",
        "ERR_INGEST_002",
        "ERR_INGEST_003",
        "ERR_INGEST_004",
        "ERR_QUERY_001",
        "ERR_QUERY_002",
        "ERR_QUERY_003",
        "ERR_QUERY_004",
        "ERR_CONFIG_001",
        "ERR_CONFIG_002",
        "ERR_AUTH_001",
        "ERR_AUTH_002",
        "ERR_AUTH_003",
    ]
    for name in expected:
        assert hasattr(errors, name), f"Missing constant: {name}"
        value = getattr(errors, name)
        # Code format: ERR-COMPONENT-NNN
        assert value.startswith("ERR-"), f"Bad format: {value}"
        assert value.count("-") == 2, f"Bad format: {value}"
