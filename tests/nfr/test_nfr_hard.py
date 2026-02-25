"""Hard NFR gate assertions for CI (merge-blocking).

Runs against a live Docker Compose stack started by integration.yml.

Required environment variables (set by the workflow):
  VEKTRA_API_URL       - base URL of the running Vektra API
  VEKTRA_BOOTSTRAP_KEY - bootstrap key for API key creation
  STARTUP_MS           - container startup time in milliseconds (NFR-004)

Gates:
  NFR-002: Search p95 < 500ms (100 queries on 10k seeded chunks)
  NFR-004: Container startup < 60s
  NFR-007: Audit log completeness (authenticated requests == audit_log rows)
  NFR-009: All 13 error codes exist with required remediation field
"""

from __future__ import annotations

import math
import os
import subprocess
import time

import httpx
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _psql(sql: str) -> str:
    """Execute SQL in the postgres container and return stdout."""
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "vektra",
            "-d",
            "vektra",
            "-tAc",
            sql,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# NFR-004: Container startup < 60s
# ---------------------------------------------------------------------------


def test_nfr_004_startup_time() -> None:
    """Container startup (docker compose up to /health 200) under 60s."""
    startup_ms = int(os.environ.get("STARTUP_MS", "0"))
    assert startup_ms > 0, "STARTUP_MS env var not set (set by integration.yml)"
    assert startup_ms < 60_000, (
        f"Container startup took {startup_ms}ms, exceeding 60s limit (NFR-004)"
    )


# ---------------------------------------------------------------------------
# NFR-007: Audit log completeness
# ---------------------------------------------------------------------------


def test_nfr_007_audit_completeness(api: httpx.Client, admin_key: str) -> None:
    """Every authenticated request must produce one audit_log row.

    Makes 50 authenticated requests and verifies audit_log has >= 50 new rows.
    """
    count_before = int(_psql("SELECT COUNT(*) FROM audit_log"))

    n_requests = 50
    for i in range(n_requests):
        resp = api.get(
            "/api/v1/providers",
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        assert resp.status_code == 200, (
            f"Audit probe request {i} failed: {resp.status_code} {resp.text}"
        )

    # Poll for audit rows (async writes may lag under CI load)
    deadline = time.monotonic() + 10
    count_after = count_before
    while time.monotonic() < deadline:
        count_after = int(_psql("SELECT COUNT(*) FROM audit_log"))
        if count_after - count_before >= n_requests:
            break
        time.sleep(1)

    new_rows = count_after - count_before

    assert new_rows >= n_requests, (
        f"Expected >= {n_requests} audit log rows, got {new_rows} "
        f"(before={count_before}, after={count_after}) -- NFR-007 violated"
    )


# ---------------------------------------------------------------------------
# NFR-009: Error code remediation completeness
# ---------------------------------------------------------------------------


_NORMATIVE_CODES = [
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


def test_nfr_009_error_codes_exist() -> None:
    """All 13 normative error codes are defined with correct format."""
    from vektra_shared import errors

    for name in _NORMATIVE_CODES:
        assert hasattr(errors, name), f"Missing error code constant: {name}"
        code_value = getattr(errors, name)
        assert code_value.startswith("ERR-") and code_value.count("-") == 2, (
            f"Invalid code format for {name}: {code_value}"
        )


def test_nfr_009_error_response_requires_remediation() -> None:
    """ErrorResponse.remediation is a required field (cannot be omitted)."""
    from vektra_shared.errors import ErrorResponse

    with pytest.raises(TypeError):
        ErrorResponse(  # type: ignore[call-arg]
            category="PERMANENT",
            code="ERR-TEST-000",
            message="test",
            # remediation intentionally omitted
        )


def test_nfr_009_triggerable_codes_have_remediation(
    api: httpx.Client, admin_key: str
) -> None:
    """Error codes triggered via HTTP include non-empty remediation."""
    # ERR-AUTH-001: missing token
    resp = api.get("/api/v1/providers")
    assert resp.status_code == 401
    _assert_remediation(resp.json(), "ERR-AUTH-001")

    # ERR-AUTH-003: insufficient scope (query key on admin endpoint)
    query_resp = api.post(
        "/api/v1/api-keys",
        json={"label": "nfr-query", "scopes": ["query"]},
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert query_resp.status_code == 201
    query_key = query_resp.json()["key"]

    resp = api.post(
        "/api/v1/api-keys",
        json={"label": "should-fail"},
        headers={"Authorization": f"Bearer {query_key}"},
    )
    assert resp.status_code == 403
    _assert_remediation(resp.json(), "ERR-AUTH-003")


def _assert_remediation(body: dict, expected_code: str) -> None:
    """Assert error envelope has non-empty remediation."""
    envelope = body.get("detail", body)
    error = envelope.get("error", envelope)
    assert error.get("code") == expected_code, (
        f"Expected {expected_code}, got {error.get('code')}"
    )
    assert error.get("remediation"), (
        f"Empty or missing remediation for {expected_code} (NFR-009)"
    )


# ---------------------------------------------------------------------------
# NFR-002: Search latency p95 < 500ms
# ---------------------------------------------------------------------------

_SEED_CHUNKS = 10_000
_SEARCH_QUERIES = 100
_EMBEDDING_DIM = 384

_SEED_DOC_ID = "10000000-0000-0000-0000-000000000001"


def _seed_benchmark_chunks() -> int:
    """Insert synthetic chunks for latency testing. Returns chunk count."""
    _psql(
        f"INSERT INTO source_documents "
        f"(id, namespace_id, filename, content_hash, content_type, file_size_bytes) "
        f"VALUES ('{_SEED_DOC_ID}'::uuid, 'default', 'nfr-002-bench.txt', "
        f"'nfr002benchhash', 'text/plain', 1024) "
        f"ON CONFLICT (id) DO NOTHING"
    )

    _psql(f"DELETE FROM document_chunks WHERE document_id = '{_SEED_DOC_ID}'::uuid")

    _psql(
        f"INSERT INTO document_chunks "
        f"(document_id, namespace_id, content, embedding, position) "
        f"SELECT "
        f"  '{_SEED_DOC_ID}'::uuid, "
        f"  'default', "
        f"  'Synthetic benchmark chunk ' || g || ' for latency testing', "
        f"  (SELECT array_agg(random()::float4)::vector({_EMBEDDING_DIM}) "
        f"   FROM generate_series(1, {_EMBEDDING_DIM})), "
        f"  g "
        f"FROM generate_series(1, {_SEED_CHUNKS}) AS g"
    )

    return int(_psql("SELECT COUNT(*) FROM document_chunks"))


def test_nfr_002_search_latency(api: httpx.Client, admin_key: str) -> None:
    """Search p95 latency on 10k chunks must be under 500ms (NFR-002)."""
    count = _seed_benchmark_chunks()
    assert count >= _SEED_CHUNKS, (
        f"Expected >= {_SEED_CHUNKS} seeded chunks, got {count}"
    )

    latencies_ms: list[float] = []

    for i in range(_SEARCH_QUERIES):
        start = time.monotonic()
        resp = api.post(
            "/api/v1/search",
            json={"query": f"benchmark query number {i}", "top_k": 5},
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        elapsed_ms = (time.monotonic() - start) * 1000
        latencies_ms.append(elapsed_ms)

        assert resp.status_code == 200, (
            f"Search request {i} failed: {resp.status_code} {resp.text}"
        )

    latencies_ms.sort()
    p95_idx = math.ceil(0.95 * len(latencies_ms)) - 1
    p95 = latencies_ms[p95_idx]
    p50 = latencies_ms[len(latencies_ms) // 2]

    print(f"\nNFR-002 Search latency ({_SEARCH_QUERIES} queries, {count} chunks):")
    print(f"  p50:  {p50:.1f}ms")
    print(f"  p95:  {p95:.1f}ms")
    print(f"  min:  {latencies_ms[0]:.1f}ms")
    print(f"  max:  {latencies_ms[-1]:.1f}ms")

    assert p95 < 500, (
        f"Search p95 latency is {p95:.1f}ms, exceeding 500ms limit (NFR-002)"
    )
