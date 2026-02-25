"""Non-blocking performance measurements for CI (warn-only, does not block merge).

Measures query latency against the running Docker Compose stack and logs
results. Exceeding thresholds produces warnings in the job summary but
does not fail the workflow.

Requires:
  VEKTRA_API_URL       - base URL of the running Vektra API
  VEKTRA_BOOTSTRAP_KEY - bootstrap key for API key creation
"""

from __future__ import annotations

import math
import time

import httpx
import pytest

# NFR-001 target: query latency (not a hard gate in Phase 1 per EX-004)
_QUERY_LATENCY_TARGET_MS = 2000
_NOTE_THRESHOLD = 1.2  # 20% above target -> note
_WARN_THRESHOLD = 1.5  # 50% above target -> warn

_NUM_QUERIES = 100


def test_query_latency_measurement(api: httpx.Client, admin_key: str) -> None:
    """Measure end-to-end query latency (embed + search + LLM attempt).

    Results are logged to stdout for the GitHub Actions job summary.
    This test always passes; exceeding thresholds produces warnings only.
    """
    latencies_ms: list[float] = []

    for i in range(_NUM_QUERIES):
        start = time.monotonic()
        resp = api.post(
            "/api/v1/query",
            json={
                "question": f"performance measurement query {i}",
                "namespace": "default",
                "top_k": 5,
            },
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        elapsed_ms = (time.monotonic() - start) * 1000
        latencies_ms.append(elapsed_ms)

        # Accept any response status (LLM may be unavailable)
        if resp.status_code not in (200, 502):
            pytest.skip(f"Query endpoint returned unexpected status {resp.status_code}")

    latencies_ms.sort()
    p50 = latencies_ms[len(latencies_ms) // 2]
    p95_idx = math.ceil(0.95 * len(latencies_ms)) - 1
    p95 = latencies_ms[p95_idx]
    p99_idx = math.ceil(0.99 * len(latencies_ms)) - 1
    p99 = latencies_ms[p99_idx]

    target = _QUERY_LATENCY_TARGET_MS

    # Determine annotation level (check worst threshold first)
    if p95 > target * _WARN_THRESHOLD:
        annotation = f"WARN: p95 is >{int(_WARN_THRESHOLD * 100 - 100)}% above target"
    elif p95 > target * _NOTE_THRESHOLD:
        annotation = f"NOTE: p95 is >{int(_NOTE_THRESHOLD * 100 - 100)}% above target"
    else:
        annotation = "PASS"

    print(f"\n{'=' * 60}")
    print(f"Query latency ({_NUM_QUERIES} queries, NFR-001 target: {target}ms)")
    print(f"{'=' * 60}")
    print(f"  p50:  {p50:.1f}ms")
    print(f"  p95:  {p95:.1f}ms")
    print(f"  p99:  {p99:.1f}ms")
    print(f"  min:  {latencies_ms[0]:.1f}ms")
    print(f"  max:  {latencies_ms[-1]:.1f}ms")
    print(f"  annotation: {annotation}")
    print(f"{'=' * 60}")

    # Test always passes (non-blocking per EX-004)
    assert True
