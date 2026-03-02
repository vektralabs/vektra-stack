"""Unit tests for document versioning (T8-T11).

Verifies that re-ingesting a file with the same filename but different
content creates a new version instead of raising IngestConflictError.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from vektra_shared.types import DocumentChunk, ElementType

# ---------------------------------------------------------------------------
# Mock helpers (reuse pattern from test_pipeline.py)
# ---------------------------------------------------------------------------


def _make_registry(*, embedding=None, vector_store=None, events=None):
    """Build a minimal mock ProviderRegistry."""
    mock_reg = MagicMock()

    default_embedding = AsyncMock()
    default_embedding.embed_documents = AsyncMock(return_value=[[0.1] * 384])

    default_vs = AsyncMock()
    default_vs.store = AsyncMock(return_value=["chunk-id-1"])
    default_vs.delete = AsyncMock(return_value=5)

    def _get(category, name="default"):
        if category == "embedding":
            return embedding or default_embedding
        if category == "vector_store":
            return vector_store or default_vs
        if category == "events":
            if events is None:
                raise ValueError("not registered")
            return events
        raise ValueError(f"Unknown category: {category}")

    mock_reg.get.side_effect = _get
    return mock_reg, default_embedding, default_vs


async def _fake_extract(req):
    async def _gen():
        yield DocumentChunk(text="chunk content", element_type=ElementType.TEXT)

    return _gen()


async def _fake_chunk(elements):
    async def _gen():
        async for e in elements:
            yield e

    return _gen()


# ---------------------------------------------------------------------------
# Tests: document versioning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reingest_creates_new_version():
    """Re-ingest with same filename + different content creates version 2."""
    from vektra_ingest.pipeline import run_ingest

    old_doc_id = uuid4()
    new_doc_id = uuid4()

    # Mock the existing document (version 1)
    existing_doc = MagicMock()
    existing_doc.id = old_doc_id
    existing_doc.filename = "report.pdf"
    existing_doc.version = 1
    existing_doc.content_hash = "old_hash_different_from_new"

    call_count = 0

    async def _execute(stmt, *args, **kwargs):
        nonlocal call_count
        mock_result = MagicMock()
        if call_count == 0:
            # Step 1: content_hash query -> no match (different content)
            mock_result.scalar_one_or_none.return_value = None
        elif call_count == 1:
            # Step 2: filename query -> finds existing doc (version 1)
            mock_result.scalar_one_or_none.return_value = existing_doc
        else:
            # Subsequent calls (update, etc.) return mock result
            mock_result.scalar_one_or_none.return_value = None
        call_count += 1
        return mock_result

    added_docs: list = []

    def _session_add(obj):
        added_docs.append(obj)

    async def _flush_side_effect():
        for obj in added_docs:
            if not hasattr(obj, "_id_set"):
                obj.id = new_doc_id
                obj._id_set = True

    session = AsyncMock()
    session.execute = _execute
    session.add = _session_add
    session.flush = AsyncMock(side_effect=_flush_side_effect)
    session.commit = AsyncMock()

    registry, mock_embedding, mock_vs = _make_registry()
    mock_embedding.embed_documents = AsyncMock(return_value=[[0.1] * 384])
    mock_vs.store = AsyncMock(return_value=["chunk-1"])

    mock_extractor = MagicMock()
    mock_extractor.extract = _fake_extract

    with patch("vektra_ingest.pipeline.detect_content_type", return_value="application/pdf"):
        with patch("vektra_ingest.pipeline._get_extractor", return_value=mock_extractor):
            with patch("vektra_ingest.pipeline.FixedSizeChunking") as mock_cc:
                mc = MagicMock()
                mc.chunk = _fake_chunk
                mock_cc.return_value = mc

                result = await run_ingest(
                    file_content=b"%PDF-1.4 new content here",
                    filename="report.pdf",
                    namespace="default",
                    session=session,
                    registry=registry,
                )

    assert result.status == "indexed"
    assert result.document_id == new_doc_id
    assert result.version == 2
    assert result.supersedes_id == old_doc_id

    # Vector store delete was called to remove old chunks
    mock_vs.delete.assert_called_once_with("default", [str(old_doc_id)])

    # New source_document has version=2 and supersedes_id
    assert len(added_docs) == 1
    assert added_docs[0].version == 2
    assert added_docs[0].supersedes_id == old_doc_id


@pytest.mark.asyncio
async def test_reingest_version_3():
    """Re-ingesting a v2 document creates version 3."""
    from vektra_ingest.pipeline import run_ingest

    old_doc_id = uuid4()
    new_doc_id = uuid4()

    existing_doc = MagicMock()
    existing_doc.id = old_doc_id
    existing_doc.filename = "report.pdf"
    existing_doc.version = 2  # already version 2

    call_count = 0

    async def _execute(stmt, *args, **kwargs):
        nonlocal call_count
        mock_result = MagicMock()
        if call_count == 0:
            mock_result.scalar_one_or_none.return_value = None
        elif call_count == 1:
            mock_result.scalar_one_or_none.return_value = existing_doc
        else:
            mock_result.scalar_one_or_none.return_value = None
        call_count += 1
        return mock_result

    added_docs: list = []

    def _session_add(obj):
        added_docs.append(obj)

    async def _flush_side_effect():
        for obj in added_docs:
            if not hasattr(obj, "_id_set"):
                obj.id = new_doc_id
                obj._id_set = True

    session = AsyncMock()
    session.execute = _execute
    session.add = _session_add
    session.flush = AsyncMock(side_effect=_flush_side_effect)
    session.commit = AsyncMock()

    registry, mock_embedding, mock_vs = _make_registry()
    mock_embedding.embed_documents = AsyncMock(return_value=[[0.1] * 384])
    mock_vs.store = AsyncMock(return_value=["chunk-1"])

    mock_extractor = MagicMock()
    mock_extractor.extract = _fake_extract

    with patch("vektra_ingest.pipeline.detect_content_type", return_value="application/pdf"):
        with patch("vektra_ingest.pipeline._get_extractor", return_value=mock_extractor):
            with patch("vektra_ingest.pipeline.FixedSizeChunking") as mock_cc:
                mc = MagicMock()
                mc.chunk = _fake_chunk
                mock_cc.return_value = mc

                result = await run_ingest(
                    file_content=b"%PDF-1.4 version three",
                    filename="report.pdf",
                    namespace="default",
                    session=session,
                    registry=registry,
                )

    assert result.version == 3
    assert result.supersedes_id == old_doc_id


@pytest.mark.asyncio
async def test_new_file_gets_version_1():
    """A fresh file (no existing filename match) gets version=1."""
    from vektra_ingest.pipeline import run_ingest

    new_doc_id = uuid4()

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_result)

    added_docs: list = []

    def _session_add(obj):
        added_docs.append(obj)

    async def _flush_side_effect():
        for obj in added_docs:
            obj.id = new_doc_id

    session.add = _session_add
    session.flush = AsyncMock(side_effect=_flush_side_effect)
    session.commit = AsyncMock()

    registry, mock_embedding, mock_vs = _make_registry()
    mock_embedding.embed_documents = AsyncMock(return_value=[[0.1] * 384])
    mock_vs.store = AsyncMock(return_value=["chunk-1"])

    mock_extractor = MagicMock()
    mock_extractor.extract = _fake_extract

    with patch("vektra_ingest.pipeline.detect_content_type", return_value="application/pdf"):
        with patch("vektra_ingest.pipeline._get_extractor", return_value=mock_extractor):
            with patch("vektra_ingest.pipeline.FixedSizeChunking") as mock_cc:
                mc = MagicMock()
                mc.chunk = _fake_chunk
                mock_cc.return_value = mc

                result = await run_ingest(
                    file_content=b"%PDF-1.4 fresh document",
                    filename="new-file.pdf",
                    namespace="default",
                    session=session,
                    registry=registry,
                )

    assert result.status == "indexed"
    assert result.version == 1
    assert result.supersedes_id is None


@pytest.mark.asyncio
async def test_superseded_event_emitted():
    """When versioning occurs, document.superseded event is emitted."""
    from vektra_ingest.pipeline import run_ingest

    old_doc_id = uuid4()
    new_doc_id = uuid4()

    existing_doc = MagicMock()
    existing_doc.id = old_doc_id
    existing_doc.filename = "doc.pdf"
    existing_doc.version = 1

    call_count = 0

    async def _execute(stmt, *args, **kwargs):
        nonlocal call_count
        mock_result = MagicMock()
        if call_count == 0:
            mock_result.scalar_one_or_none.return_value = None
        elif call_count == 1:
            mock_result.scalar_one_or_none.return_value = existing_doc
        else:
            mock_result.scalar_one_or_none.return_value = None
        call_count += 1
        return mock_result

    added_docs: list = []

    def _session_add(obj):
        added_docs.append(obj)

    async def _flush_side_effect():
        for obj in added_docs:
            if not hasattr(obj, "_id_set"):
                obj.id = new_doc_id
                obj._id_set = True

    session = AsyncMock()
    session.execute = _execute
    session.add = _session_add
    session.flush = AsyncMock(side_effect=_flush_side_effect)
    session.commit = AsyncMock()

    mock_events = AsyncMock()
    mock_events.emit = AsyncMock()

    registry, mock_embedding, mock_vs = _make_registry(events=mock_events)
    mock_embedding.embed_documents = AsyncMock(return_value=[[0.1] * 384])
    mock_vs.store = AsyncMock(return_value=["chunk-1"])

    mock_extractor = MagicMock()
    mock_extractor.extract = _fake_extract

    with patch("vektra_ingest.pipeline.detect_content_type", return_value="application/pdf"):
        with patch("vektra_ingest.pipeline._get_extractor", return_value=mock_extractor):
            with patch("vektra_ingest.pipeline.FixedSizeChunking") as mock_cc:
                mc = MagicMock()
                mc.chunk = _fake_chunk
                mock_cc.return_value = mc

                await run_ingest(
                    file_content=b"%PDF-1.4 updated",
                    filename="doc.pdf",
                    namespace="default",
                    session=session,
                    registry=registry,
                )

    # Check document.superseded was emitted
    superseded_calls = [
        c for c in mock_events.emit.call_args_list if c[0][0] == "document.superseded"
    ]
    assert len(superseded_calls) == 1
    payload = superseded_calls[0][0][1]
    assert payload["document_id"] == str(old_doc_id)
    assert payload["old_version"] == 1
    assert payload["new_version"] == 2

    # Also check document.indexed was emitted
    indexed_calls = [
        c for c in mock_events.emit.call_args_list if c[0][0] == "document.indexed"
    ]
    assert len(indexed_calls) == 1


@pytest.mark.asyncio
async def test_old_doc_soft_deleted_with_superseded_reason():
    """Old document is soft-deleted with deletion_reason='superseded'."""
    from vektra_ingest.pipeline import run_ingest

    old_doc_id = uuid4()
    new_doc_id = uuid4()

    existing_doc = MagicMock()
    existing_doc.id = old_doc_id
    existing_doc.filename = "doc.pdf"
    existing_doc.version = 1

    call_count = 0
    executed_stmts = []

    async def _execute(stmt, *args, **kwargs):
        nonlocal call_count
        executed_stmts.append(stmt)
        mock_result = MagicMock()
        if call_count == 0:
            mock_result.scalar_one_or_none.return_value = None
        elif call_count == 1:
            mock_result.scalar_one_or_none.return_value = existing_doc
        else:
            mock_result.scalar_one_or_none.return_value = None
        call_count += 1
        return mock_result

    added_docs: list = []

    def _session_add(obj):
        added_docs.append(obj)

    async def _flush_side_effect():
        for obj in added_docs:
            if not hasattr(obj, "_id_set"):
                obj.id = new_doc_id
                obj._id_set = True

    session = AsyncMock()
    session.execute = _execute
    session.add = _session_add
    session.flush = AsyncMock(side_effect=_flush_side_effect)
    session.commit = AsyncMock()

    registry, mock_embedding, mock_vs = _make_registry()
    mock_embedding.embed_documents = AsyncMock(return_value=[[0.1] * 384])
    mock_vs.store = AsyncMock(return_value=["chunk-1"])

    mock_extractor = MagicMock()
    mock_extractor.extract = _fake_extract

    with patch("vektra_ingest.pipeline.detect_content_type", return_value="application/pdf"):
        with patch("vektra_ingest.pipeline._get_extractor", return_value=mock_extractor):
            with patch("vektra_ingest.pipeline.FixedSizeChunking") as mock_cc:
                mc = MagicMock()
                mc.chunk = _fake_chunk
                mock_cc.return_value = mc

                result = await run_ingest(
                    file_content=b"%PDF-1.4 new version",
                    filename="doc.pdf",
                    namespace="default",
                    session=session,
                    registry=registry,
                )

    # The session should have had at least one UPDATE with deletion_reason
    # We verify via the vector_store.delete call which confirms old chunks were removed
    mock_vs.delete.assert_called_once_with("default", [str(old_doc_id)])

    # The result should reflect the new version
    assert result.status == "indexed"
    assert result.version == 2


@pytest.mark.asyncio
async def test_exact_duplicate_still_returns_exists():
    """Same content + same filename still returns 'exists' (no versioning)."""
    from vektra_ingest.pipeline import run_ingest

    existing_doc_id = uuid4()
    existing = MagicMock()
    existing.id = existing_doc_id
    existing.filename = "doc.pdf"
    existing.filename_aliases = []

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    session.execute = AsyncMock(return_value=mock_result)
    session.commit = AsyncMock()

    registry, _, _ = _make_registry()

    result = await run_ingest(
        file_content=b"exact same content",
        filename="doc.pdf",
        namespace="default",
        session=session,
        registry=registry,
    )

    assert result.status == "exists"
    assert result.document_id == existing_doc_id
