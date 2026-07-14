"""Chunk lifecycle against the live stack, on whichever vector store is active.

This file exists because of BUG-023. Every path here reported success while
doing nothing in Qdrant mode: reindex re-embedded no chunks and reported
"completed", stats reported 0 chunks for a populated namespace, and DELETE
returned 200 while the document stayed searchable.

None of it was caught, for months, because the only Qdrant test mocked the
qdrant_client module wholesale and the integration suite ran against pgvector
only. A mocked client cannot notice that a table is empty.

So these assertions are deliberately made through the public API alone, with no
knowledge of which store is behind it, and CI runs the file against both
(VEKTRA_VECTOR_STORE_PROVIDER=pgvector and =qdrant). A path that behaves
differently under one provider fails here.
"""

from __future__ import annotations

import time

import httpx
import pytest

# Runs against the live stack (integration.yml), so every unit run must exclude it.
pytestmark = pytest.mark.integration

CHUNK_TEXT = (
    "# Zarnak protocol\n\n"
    "The Zarnak protocol reaches consensus with a quorum of seven nodes.\n"
    "Its distinguishing feature is the Vremlin handshake, which runs before "
    "every commit and rejects any peer that cannot present a valid epoch token.\n"
)

NAMESPACE = "chunk-lifecycle"


@pytest.fixture(scope="module")
def document_id(api: httpx.Client, admin_key: str) -> str:
    """Ingest one document into a dedicated namespace; clean up after."""
    response = api.post(
        "/api/v1/ingest",
        params={"namespace": NAMESPACE},
        headers={"Authorization": f"Bearer {admin_key}"},
        files={"file": ("zarnak.md", CHUNK_TEXT, "text/markdown")},
    )
    assert response.status_code == 200, response.text
    doc_id = response.json()["document_id"]

    yield doc_id

    api.delete(
        f"/api/v1/documents/{doc_id}",
        params={"namespace": NAMESPACE},
        headers={"Authorization": f"Bearer {admin_key}"},
    )


def _stats(api: httpx.Client, admin_key: str) -> dict:
    response = api.get(
        "/api/v1/stats",
        params={"namespace": NAMESPACE},
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_stats_counts_the_chunks_that_were_ingested(
    api: httpx.Client, admin_key: str, document_id: str
) -> None:
    """chunk_count must reflect the active store, not an unrelated table."""
    stats = _stats(api, admin_key)

    assert stats["document_count"] == 1
    assert stats["chunk_count"] > 0


def test_list_chunks_returns_the_stored_chunks(
    api: httpx.Client, admin_key: str, document_id: str
) -> None:
    response = api.get(
        f"/api/v1/documents/{document_id}/chunks",
        params={"namespace": NAMESPACE},
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["total"] > 0
    # The text comes back from the store that actually holds it
    assert any("Vremlin" in chunk["text"] for chunk in body["chunks"])

    # The two endpoints must agree. Sole document in the namespace at this
    # point, so the namespace-wide count is this document's chunk count.
    stats = _stats(api, admin_key)
    assert stats["document_count"] == 1
    assert body["total"] == stats["chunk_count"]


def test_reindex_rewrites_the_chunks_it_reports(
    api: httpx.Client, admin_key: str, document_id: str
) -> None:
    """A reindex that stores nothing must not report success.

    chunks_reindexed is the assertion that matters: a job can walk every
    document and write nothing, which is precisely what it used to do.
    """
    chunk_count = _stats(api, admin_key)["chunk_count"]

    response = api.post(
        "/api/v1/reindex",
        headers={"Authorization": f"Bearer {admin_key}"},
        json={"namespace": NAMESPACE, "target_index_version": 2},
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        status = api.get(
            f"/api/v1/reindex/{job_id}/status",
            headers={"Authorization": f"Bearer {admin_key}"},
        ).json()
        if status["status"] in ("completed", "failed"):
            break
        time.sleep(2)
    else:
        pytest.fail(f"reindex job {job_id} did not finish within 120s")

    assert status["status"] == "completed", status.get("error_message")
    assert status["processed_documents"] == 1
    # The whole point: it actually re-embedded and wrote the chunks
    assert status["chunks_reindexed"] == chunk_count


def test_deleted_document_is_not_retrievable(api: httpx.Client, admin_key: str) -> None:
    """Deleting a document must remove its content from the vector store.

    DELETE used to return 200 with chunks_removed: 0, delete the Postgres row,
    leave the Qdrant points in place, and the deleted document went on answering
    queries. This is the assertion that catches that.
    """
    auth = {"Authorization": f"Bearer {admin_key}"}

    ingested = api.post(
        "/api/v1/ingest",
        params={"namespace": NAMESPACE},
        headers=auth,
        files={"file": ("doomed.md", CHUNK_TEXT, "text/markdown")},
    )
    assert ingested.status_code == 200, ingested.text
    doomed_id = ingested.json()["document_id"]

    found = api.post(
        "/api/v1/search",
        headers=auth,
        json={
            "query": "What is the Vremlin handshake?",
            "namespace": NAMESPACE,
            "top_k": 5,
        },
    ).json()
    assert found["total"] > 0, "precondition: the document must be searchable first"

    deleted = api.delete(
        f"/api/v1/documents/{doomed_id}",
        params={"namespace": NAMESPACE},
        headers=auth,
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["chunks_removed"] > 0

    after = api.post(
        "/api/v1/search",
        headers=auth,
        json={
            "query": "What is the Vremlin handshake?",
            "namespace": NAMESPACE,
            "top_k": 5,
        },
    ).json()

    returned_docs = {r["document_id"] for r in after["results"]}
    assert doomed_id not in returned_docs, (
        "a deleted document is still retrievable: its chunks survived the delete"
    )
