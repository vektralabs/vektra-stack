"""Shared fixtures for integration and NFR tests against the Docker Compose stack."""

from __future__ import annotations

import os

import httpx
import pytest

API_URL = os.environ.get("VEKTRA_API_URL", "http://localhost:8000")
BOOTSTRAP_KEY = os.environ.get("VEKTRA_BOOTSTRAP_KEY", "")


@pytest.fixture(scope="module")
def api():
    """HTTP client pointed at the running Vektra stack."""
    with httpx.Client(base_url=API_URL, timeout=60.0) as client:
        yield client


@pytest.fixture(scope="module")
def admin_key(api: httpx.Client) -> str:
    """Create an admin-scoped API key via bootstrap."""
    resp = api.post(
        "/api/v1/api-keys",
        json={"label": "ci-test-admin", "scopes": ["admin"]},
        headers={"Authorization": f"Bearer {BOOTSTRAP_KEY}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]
