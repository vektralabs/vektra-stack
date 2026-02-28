"""Shared fixtures for integration and NFR tests against the Docker Compose stack.

The admin_key fixture creates an API key via the single-use bootstrap token
and caches it to a temp file so that separate pytest invocations (integration,
NFR hard gates, performance) can share the same key within one CI run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

API_URL = os.environ.get("VEKTRA_API_URL", "http://localhost:8000")
BOOTSTRAP_KEY = os.environ.get("VEKTRA_BOOTSTRAP_KEY", "")
_KEY_CACHE = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "vektra-ci-admin-key.json"


@pytest.fixture(scope="module")
def api():
    """HTTP client pointed at the running Vektra stack."""
    with httpx.Client(base_url=API_URL, timeout=60.0) as client:
        yield client


@pytest.fixture(scope="module")
def admin_key(api: httpx.Client) -> str:
    """Obtain an all-scope API key for CI tests.

    First checks for a cached key from a prior pytest invocation (the
    bootstrap token is single-use per REQ-036). Falls back to creating
    a new key via the bootstrap token and caching it for later steps.
    """
    # Try cached key from a prior step
    if _KEY_CACHE.exists():
        try:
            cached = json.loads(_KEY_CACHE.read_text())
            key = cached["key"]
        except (json.JSONDecodeError, KeyError):
            _KEY_CACHE.unlink(missing_ok=True)
            key = None
        else:
            # Validate it still works (container may have been recycled)
            resp = api.get(
                "/api/v1/api-keys",
                headers={"Authorization": f"Bearer {key}"},
            )
            if resp.status_code == 200:
                return key

    # Create via bootstrap (first invocation only)
    assert BOOTSTRAP_KEY, (
        "VEKTRA_BOOTSTRAP_KEY must be set (and key cache is missing/invalid)"
    )
    resp = api.post(
        "/api/v1/api-keys",
        json={"label": "ci-test-admin", "scopes": ["admin", "ingest", "query"]},
        headers={"Authorization": f"Bearer {BOOTSTRAP_KEY}"},
    )
    assert resp.status_code == 201, (
        f"Bootstrap key creation failed ({resp.status_code}): {resp.text}"
    )
    key = resp.json()["key"]

    # Cache for subsequent pytest processes (owner-only permissions)
    _KEY_CACHE.write_text(json.dumps({"key": key}))
    os.chmod(_KEY_CACHE, 0o600)
    return key
