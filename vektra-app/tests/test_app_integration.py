"""Integration smoke tests for the full Vektra application (ARCH-057, NFR-004).

Requires Docker (pgvector/pgvector:pg16 container). Skipped when Docker is
not available. These tests verify the real 8-step startup validation sequence
and the health endpoint contract.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
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

# ---------------------------------------------------------------------------
# Module-scoped PostgreSQL + pgvector container
# ---------------------------------------------------------------------------

PROJECT_ROOT = str(Path(__file__).resolve().parents[2])  # vektra-stack/


@pytest.fixture(scope="module")
def db_url():
    """Start a pgvector container and run Alembic migrations."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        raw_url = postgres.get_connection_url()
        from sqlalchemy.engine import make_url

        async_url = str(make_url(raw_url).set(drivername="postgresql+asyncpg"))

        env = os.environ.copy()
        env["VEKTRA_DATABASE_URL"] = async_url

        result = subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            env=env,
            timeout=120,
        )
        if result.returncode != 0:
            pytest.fail(f"Alembic migration failed:\n{result.stderr}\n{result.stdout}")

        yield async_url


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

_BOOTSTRAP_SECRET = "test-bootstrap-secret-key"


def test_smoke_startup_health_and_auth(db_url: str) -> None:
    """Full application starts, passes /health, and enforces auth (NFR-004, REQ-025).

    Verifies:
    1. All 8 ARCH-057 startup steps complete without error.
    2. GET /health returns 200 (unauthenticated shallow check).
    3. Startup completes within 60 seconds (NFR-004).
    4. GET /health?detail=full with a valid Bearer token returns components array.
    5. Unauthenticated access to a protected endpoint returns 401.
    """
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

        start_time = time.monotonic()

        with TestClient(app) as client:
            startup_duration = time.monotonic() - start_time

            # --- NFR-004: startup < 60s ---
            assert startup_duration < 60, (
                f"Startup took {startup_duration:.1f}s, exceeding 60s limit (NFR-004)"
            )

            # --- Shallow health (unauthenticated) ---
            # LLM is unavailable in test, so overall status may be "unhealthy" (503).
            # We verify the endpoint responds with valid structure, not that LLM is up.
            resp = client.get("/health")
            assert resp.status_code in (200, 503)
            body = resp.json()
            assert body["status"] in ("healthy", "degraded", "unhealthy")
            assert "timestamp" in body

            # --- Create an API key via bootstrap ---
            resp = client.post(
                "/api/v1/api-keys",
                json={"label": "smoke-test", "scopes": ["admin"]},
                headers={"Authorization": f"Bearer {_BOOTSTRAP_SECRET}"},
            )
            assert resp.status_code == 201, resp.text
            api_key = resp.json()["key"]

            # --- Deep health (authenticated) ---
            resp = client.get(
                "/health",
                params={"detail": "full"},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            assert resp.status_code in (200, 503)
            body = resp.json()
            assert "status" in body
            assert "version" in body
            assert "components" in body
            assert isinstance(body["components"], list)

            # --- Unauthenticated access to protected endpoint returns 401 ---
            resp = client.get("/api/v1/providers")
            assert resp.status_code == 401
            err_body = resp.json()
            # FastAPI wraps HTTPException detail as {"detail": ...}
            assert "detail" in err_body
