"""End-to-end integration test implementing REQ-032.

Flow: bootstrap key -> create API key -> POST /ingest -> verify indexed
      -> POST /query -> assert sources reference the ingested document
      -> complete in < 60s elapsed.

Runs against a live Docker Compose stack. Requires:
  VEKTRA_API_URL       - base URL of the running Vektra API
  VEKTRA_BOOTSTRAP_KEY - bootstrap key for API key creation
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
SAMPLE_PDF = FIXTURES_DIR / "sample.pdf"


@pytest.fixture(scope="module")
def flow_clock() -> dict[str, float]:
    """Shared wall-clock tracker for the 60s total elapsed assertion."""
    return {"start": time.monotonic()}


# ---------------------------------------------------------------------------
# REQ-032: Full auth-ingest-query integration flow
# ---------------------------------------------------------------------------


class TestE2EFlow:
    """Full integration flow, ordered by dependency (pytest-ordering not needed,
    class methods execute in source order with pytest)."""

    def test_01_health_reachable(
        self, api: httpx.Client, flow_clock: dict[str, float]
    ) -> None:
        """Vektra stack is up and /health responds."""
        flow_clock["start"] = time.monotonic()
        resp = api.get("/health")
        assert resp.status_code in (200, 503), (
            f"Health endpoint returned {resp.status_code}"
        )
        body = resp.json()
        if resp.status_code == 503 or body["status"] != "healthy":
            pytest.skip(
                f"Stack unhealthy (status={body['status']}), skipping E2E suite"
            )

    def test_02_create_api_key(self, api: httpx.Client, admin_key: str) -> None:
        """Admin-scoped API key was created via bootstrap (fixture)."""
        # admin_key fixture already asserts 201; verify the key works
        resp = api.get(
            "/api/v1/api-keys",
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        assert resp.status_code == 200
        keys = resp.json()
        assert any(k["label"] == "ci-test-admin" for k in keys)

    def test_03_ingest_document(self, api: httpx.Client, admin_key: str) -> None:
        """POST /ingest with sample PDF returns 200 with document_id."""
        if not SAMPLE_PDF.exists():
            pytest.skip(f"Sample PDF not found at {SAMPLE_PDF}")

        with open(SAMPLE_PDF, "rb") as f:
            resp = api.post(
                "/api/v1/ingest",
                files={"file": ("sample.pdf", f, "application/pdf")},
                params={"namespace": "default"},
                headers={"Authorization": f"Bearer {admin_key}"},
            )

        assert resp.status_code == 200, f"Ingest failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert "document_id" in body
        assert body["chunk_count"] is not None and body["chunk_count"] > 0
        assert body["status"] in ("indexed", "completed", "complete")

        # Stash document_id for later assertions
        self.__class__._document_id = body["document_id"]
        self.__class__._chunk_count = body["chunk_count"]

    def test_04_verify_stats(self, api: httpx.Client, admin_key: str) -> None:
        """GET /stats shows the ingested document and chunks."""
        resp = api.get(
            "/api/v1/stats",
            params={"namespace": "default"},
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["document_count"] >= 1
        assert body["chunk_count"] >= 1

    def test_05_query_returns_sources(self, api: httpx.Client, admin_key: str) -> None:
        """POST /query returns sources referencing the ingested document.

        LLM is unavailable in CI, so the response is context_only with
        answer=None but sources populated from the vector search.
        """
        resp = api.post(
            "/api/v1/query",
            json={
                "question": "What is Retrieval-Augmented Generation?",
                "namespace": "default",
                "top_k": 5,
            },
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        assert resp.status_code == 200, f"Query failed: {resp.status_code} {resp.text}"
        body = resp.json()

        # Response fields present
        assert "response_id" in body
        assert "sources" in body

        # LLM unavailable -> context_only mode: sources populated, answer=None
        # (If LLM is available, answer is non-None and sources are also populated)
        if body.get("context_only"):
            assert body["answer"] is None
            # Sources should still be populated from vector search
            assert len(body["sources"]) > 0, (
                "context_only response should include sources from vector search"
            )
        else:
            # LLM available: answer should be non-empty
            assert body["answer"] is not None

        # Verify sources reference our ingested document
        doc_id = getattr(self.__class__, "_document_id", None)
        assert doc_id is not None, (
            "test_03_ingest_document must run first to set _document_id"
        )
        assert body["sources"], "Expected non-empty sources from vector search"
        source_doc_ids = [s.get("doc_id") for s in body["sources"]]
        assert doc_id in source_doc_ids, (
            f"Expected document {doc_id} in sources, got {source_doc_ids}"
        )

    def test_06_total_elapsed_under_60s(self, flow_clock: dict[str, float]) -> None:
        """Full REQ-032 flow completes within 60 seconds."""
        elapsed = time.monotonic() - flow_clock["start"]
        assert elapsed < 60, (
            f"E2E flow took {elapsed:.1f}s, exceeding 60s budget (REQ-032)"
        )
