"""Unit tests for the IngestPipeline deduplication and ingestion logic.

Uses mocked SQLAlchemy sessions, EmbeddingProvider, and VectorStoreProvider
so tests run without a database or ML model.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from vektra_shared.types import DocumentChunk, ElementType

# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _make_registry(*, embedding=None, vector_store=None, events=None):
    """Build a minimal mock ProviderRegistry."""
    mock_reg = MagicMock()

    default_embedding = AsyncMock()
    default_embedding.embed_documents = AsyncMock(return_value=[[0.1] * 384])

    default_vs = AsyncMock()
    default_vs.store = AsyncMock(return_value=["chunk-id-1"])

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


def _make_session():
    """Build a minimal AsyncMock session."""
    session = AsyncMock()

    # execute() should return a result object with scalar_one_or_none() = None
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_result)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    # The ORM object that gets added needs an id after flush
    # We simulate this by patching flush to assign a UUID
    doc_id = uuid4()

    async def _flush_side_effect():
        pass  # flush is a no-op; we'll set id externally

    session.flush.side_effect = _flush_side_effect
    return session, doc_id


# ---------------------------------------------------------------------------
# Deduplication: exact match
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_exact_match_returns_exists():
    """Same content_hash + same filename → IngestResult(status='exists')."""
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
        file_content=b"fake pdf content",
        filename="doc.pdf",
        namespace="default",
        session=session,
        registry=registry,
    )

    assert result.status == "exists"
    assert result.document_id == existing_doc_id
    # Should not have called embed or store
    registry.get.assert_not_called()


@pytest.mark.asyncio
async def test_dedup_alias_different_filename():
    """Same content_hash + different filename → IngestResult(status='alias')."""
    from vektra_ingest.pipeline import run_ingest

    existing_doc_id = uuid4()
    existing = MagicMock()
    existing.id = existing_doc_id
    existing.filename = "original.pdf"
    existing.filename_aliases = []

    call_count = 0

    async def _execute(stmt, *args, **kwargs):
        nonlocal call_count
        mock_result = MagicMock()
        if call_count == 0:
            # Step 0: auto-create namespace (pg upsert) -> no-op
            pass
        elif call_count == 1:
            # First query: by content_hash → finds existing
            mock_result.scalar_one_or_none.return_value = existing
        else:
            mock_result.scalar_one_or_none.return_value = None
        call_count += 1
        return mock_result

    session = AsyncMock()
    session.execute = _execute
    session.commit = AsyncMock()

    registry, _, _ = _make_registry()

    result = await run_ingest(
        file_content=b"fake pdf content",
        filename="alias.pdf",  # different filename
        namespace="default",
        session=session,
        registry=registry,
    )

    assert result.status == "alias"
    assert result.document_id == existing_doc_id
    assert result.alias_count == 1


# ---------------------------------------------------------------------------
# Document versioning (Phase 2): same filename, different content → new version
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filename_match_creates_new_version():
    """Different content_hash + same filename → version increment (Phase 2)."""
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
            # Step 0: auto-create namespace (pg upsert) -> no-op
            pass
        elif call_count == 1:
            # content_hash query → no match (different content)
            mock_result.scalar_one_or_none.return_value = None
        elif call_count == 2:
            # filename query → finds existing doc
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
    mock_vs.delete = AsyncMock(return_value=3)

    async def _fake_extract(req):
        async def _gen():
            yield DocumentChunk(text="new chunk content", element_type=ElementType.TEXT)

        return _gen()

    async def _fake_chunk(elements):
        async def _gen():
            async for e in elements:
                yield e

        return _gen()

    mock_extractor = MagicMock()
    mock_extractor.extract = _fake_extract

    with patch(
        "vektra_ingest.pipeline.detect_content_type", return_value="application/pdf"
    ):
        with patch(
            "vektra_ingest.pipeline._get_extractor", return_value=mock_extractor
        ):
            with patch("vektra_ingest.pipeline.FixedSizeChunking") as mock_cc:
                mc = MagicMock()
                mc.chunk = _fake_chunk
                mock_cc.return_value = mc

                result = await run_ingest(
                    file_content=b"completely different content",
                    filename="doc.pdf",
                    namespace="default",
                    session=session,
                    registry=registry,
                )

    assert result.status == "new"
    assert result.version == 2
    assert result.supersedes_id == old_doc_id
    # Old chunks were deleted
    mock_vs.delete.assert_awaited_once_with("default", [str(old_doc_id)])


# ---------------------------------------------------------------------------
# Unsupported content type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_content_type_raises_ingest_error():
    """Content type with no extractor → IngestError(ERR-INGEST-001)."""
    from vektra_ingest.exceptions import IngestError
    from vektra_ingest.pipeline import run_ingest

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_result)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    registry, _, _ = _make_registry()

    # Mock detect_content_type to return an unsupported type
    with patch(
        "vektra_ingest.pipeline.detect_content_type",
        return_value="image/jpeg",
    ):
        with patch("vektra_ingest.pipeline._cleanup_document", new_callable=AsyncMock):
            with pytest.raises(IngestError) as exc_info:
                await run_ingest(
                    file_content=b"fake image",
                    filename="photo.jpg",
                    namespace="default",
                    session=session,
                    registry=registry,
                )

    assert exc_info.value.error_code == "ERR-INGEST-001"
    assert "image/jpeg" in exc_info.value.message


# ---------------------------------------------------------------------------
# Successful ingestion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_ingest_returns_new():
    """Full ingest with mocked providers returns IngestResult(status='new')."""
    from vektra_ingest.pipeline import run_ingest

    doc_id = uuid4()

    session = AsyncMock()
    call_count = 0

    async def _execute(stmt, *args, **kwargs):
        nonlocal call_count
        mock_result = MagicMock()
        # Both dedup queries return no match
        mock_result.scalar_one_or_none.return_value = None
        call_count += 1
        return mock_result

    # Capture the added ORM instance; set its id in flush side_effect
    added_docs: list = []

    def _session_add(obj):
        added_docs.append(obj)

    async def _flush_side_effect():
        for obj in added_docs:
            obj.id = doc_id

    session.execute = _execute
    session.add = _session_add
    session.flush = AsyncMock(side_effect=_flush_side_effect)
    session.commit = AsyncMock()

    registry, mock_embedding, mock_vs = _make_registry()
    mock_embedding.embed_documents = AsyncMock(return_value=[[0.1] * 384] * 3)
    mock_vs.store = AsyncMock(return_value=["id1", "id2", "id3"])

    # Mock the whole extraction + chunking chain
    async def _fake_extract(req):
        async def _gen():
            yield DocumentChunk(
                text="chunk one content here", element_type=ElementType.TEXT
            )
            yield DocumentChunk(
                text="chunk two content here", element_type=ElementType.TEXT
            )
            yield DocumentChunk(
                text="chunk three content here", element_type=ElementType.TEXT
            )

        return _gen()

    async def _fake_chunk(elements):
        async def _gen():
            async for e in elements:
                yield e

        return _gen()

    # Patch extractor registry and detect_content_type
    mock_extractor = MagicMock()
    mock_extractor.extract = _fake_extract
    mock_extractor.supported_types.return_value = {"application/pdf"}

    with patch(
        "vektra_ingest.pipeline.detect_content_type", return_value="application/pdf"
    ):
        with patch(
            "vektra_ingest.pipeline._get_extractor", return_value=mock_extractor
        ):
            with patch(
                "vektra_ingest.pipeline.FixedSizeChunking"
            ) as mock_chunker_class:
                mock_chunker = MagicMock()
                mock_chunker.chunk = _fake_chunk
                mock_chunker_class.return_value = mock_chunker

                result = await run_ingest(
                    file_content=b"%PDF-1.4 fake pdf",
                    filename="test.pdf",
                    namespace="default",
                    session=session,
                    registry=registry,
                )

    assert result.status == "new"
    assert result.document_id == doc_id
    assert result.chunk_count == 3
    mock_vs.store.assert_called_once()
    mock_embedding.embed_documents.assert_called_once()


# ---------------------------------------------------------------------------
# Storage failure cleanup (BR-003)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_storage_failure_triggers_cleanup():
    """VectorStoreProvider.store() failure triggers _cleanup_document (BR-003)."""
    from vektra_ingest.exceptions import IngestError
    from vektra_ingest.pipeline import run_ingest

    doc_id = uuid4()

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_result)

    added_docs: list = []

    def _session_add(obj):
        added_docs.append(obj)

    async def _flush_side_effect():
        for obj in added_docs:
            obj.id = doc_id

    session.add = _session_add
    session.flush = AsyncMock(side_effect=_flush_side_effect)
    session.commit = AsyncMock()

    # Vector store fails
    mock_vs = AsyncMock()
    mock_vs.store = AsyncMock(side_effect=RuntimeError("pgvector connection lost"))

    registry, mock_embedding, _ = _make_registry(vector_store=mock_vs)
    mock_embedding.embed_documents = AsyncMock(return_value=[[0.1] * 384])

    async def _fake_extract(req):
        async def _gen():
            yield DocumentChunk(text="some text", element_type=ElementType.TEXT)

        return _gen()

    async def _fake_chunk(elements):
        async def _gen():
            async for e in elements:
                yield e

        return _gen()

    mock_extractor = MagicMock()
    mock_extractor.extract = _fake_extract

    with patch(
        "vektra_ingest.pipeline.detect_content_type", return_value="application/pdf"
    ):
        with patch(
            "vektra_ingest.pipeline._get_extractor", return_value=mock_extractor
        ):
            with patch("vektra_ingest.pipeline.FixedSizeChunking") as mock_cc:
                mc = MagicMock()
                mc.chunk = _fake_chunk
                mock_cc.return_value = mc

                with patch(
                    "vektra_ingest.pipeline._cleanup_document",
                    new_callable=AsyncMock,
                ) as mock_cleanup:
                    with pytest.raises(IngestError) as exc_info:
                        await run_ingest(
                            file_content=b"%PDF-1.4 fake",
                            filename="test.pdf",
                            namespace="default",
                            session=session,
                            registry=registry,
                        )

    assert exc_info.value.error_code == "ERR-INGEST-004"
    mock_cleanup.assert_called_once_with(doc_id)


# ---------------------------------------------------------------------------
# Phase tracking callback (DEBT-006)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_phase_callback_called_in_sequence():
    """on_phase callback is called with extracting, chunking, embedding in order."""
    from vektra_ingest.pipeline import run_ingest

    doc_id = uuid4()

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_result)

    added_docs: list = []

    def _session_add(obj):
        added_docs.append(obj)

    async def _flush_side_effect():
        for obj in added_docs:
            obj.id = doc_id

    session.add = _session_add
    session.flush = AsyncMock(side_effect=_flush_side_effect)
    session.commit = AsyncMock()

    registry, mock_embedding, mock_vs = _make_registry()
    mock_embedding.embed_documents = AsyncMock(return_value=[[0.1] * 384])
    mock_vs.store = AsyncMock(return_value=["chunk-1"])

    async def _fake_extract(req):
        async def _gen():
            yield DocumentChunk(text="some text", element_type=ElementType.TEXT)

        return _gen()

    async def _fake_chunk(elements):
        async def _gen():
            async for e in elements:
                yield e

        return _gen()

    mock_extractor = MagicMock()
    mock_extractor.extract = _fake_extract

    phases: list[tuple[str, int | None]] = []

    async def _on_phase(phase: str, percentage: int | None) -> None:
        phases.append((phase, percentage))

    with patch(
        "vektra_ingest.pipeline.detect_content_type", return_value="application/pdf"
    ):
        with patch(
            "vektra_ingest.pipeline._get_extractor", return_value=mock_extractor
        ):
            with patch("vektra_ingest.pipeline.FixedSizeChunking") as mock_cc:
                mc = MagicMock()
                mc.chunk = _fake_chunk
                mock_cc.return_value = mc

                await run_ingest(
                    file_content=b"%PDF-1.4 fake",
                    filename="test.pdf",
                    namespace="default",
                    session=session,
                    registry=registry,
                    on_phase=_on_phase,
                )

    assert phases == [("extracting", None), ("chunking", None), ("embedding", 50)]


# ---------------------------------------------------------------------------
# Event emission (T27-T28)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_document_indexed_event_emitted():
    """document.indexed event is emitted on successful ingest."""
    from vektra_ingest.pipeline import run_ingest

    doc_id = uuid4()

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_result)

    added_docs: list = []

    def _session_add(obj):
        added_docs.append(obj)

    async def _flush_side_effect():
        for obj in added_docs:
            obj.id = doc_id

    session.add = _session_add
    session.flush = AsyncMock(side_effect=_flush_side_effect)
    session.commit = AsyncMock()

    mock_events = AsyncMock()
    mock_events.emit = AsyncMock()

    registry, mock_embedding, mock_vs = _make_registry(events=mock_events)
    mock_embedding.embed_documents = AsyncMock(return_value=[[0.1] * 384])
    mock_vs.store = AsyncMock(return_value=["chunk-1"])

    async def _fake_extract(req):
        async def _gen():
            yield DocumentChunk(text="some text", element_type=ElementType.TEXT)

        return _gen()

    async def _fake_chunk(elements):
        async def _gen():
            async for e in elements:
                yield e

        return _gen()

    mock_extractor = MagicMock()
    mock_extractor.extract = _fake_extract

    with patch(
        "vektra_ingest.pipeline.detect_content_type", return_value="application/pdf"
    ):
        with patch(
            "vektra_ingest.pipeline._get_extractor", return_value=mock_extractor
        ):
            with patch("vektra_ingest.pipeline.FixedSizeChunking") as mock_cc:
                mc = MagicMock()
                mc.chunk = _fake_chunk
                mock_cc.return_value = mc

                result = await run_ingest(
                    file_content=b"%PDF-1.4 fake",
                    filename="test.pdf",
                    namespace="default",
                    session=session,
                    registry=registry,
                )

    assert result.status == "new"

    # Check document.indexed event was emitted
    emit_calls = [
        c for c in mock_events.emit.call_args_list if c.args[0] == "document.indexed"
    ]
    assert len(emit_calls) == 1
    payload = emit_calls[0].args[1]
    assert payload["document_id"] == str(doc_id)
    assert payload["namespace"] == "default"
    assert payload["chunk_count"] == 1


@pytest.mark.asyncio
async def test_storage_failure_raises_ingest_error():
    """Storage failure wraps exception as IngestError."""
    from vektra_ingest.exceptions import IngestError
    from vektra_ingest.pipeline import run_ingest

    doc_id = uuid4()

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_result)

    added_docs: list = []

    def _session_add(obj):
        added_docs.append(obj)

    async def _flush_side_effect():
        for obj in added_docs:
            obj.id = doc_id

    session.add = _session_add
    session.flush = AsyncMock(side_effect=_flush_side_effect)
    session.commit = AsyncMock()

    mock_events = AsyncMock()
    mock_events.emit = AsyncMock()

    mock_vs = AsyncMock()
    mock_vs.store = AsyncMock(side_effect=RuntimeError("storage failed"))

    registry, mock_embedding, _ = _make_registry(
        vector_store=mock_vs, events=mock_events
    )
    mock_embedding.embed_documents = AsyncMock(return_value=[[0.1] * 384])

    async def _fake_extract(req):
        async def _gen():
            yield DocumentChunk(text="some text", element_type=ElementType.TEXT)

        return _gen()

    async def _fake_chunk(elements):
        async def _gen():
            async for e in elements:
                yield e

        return _gen()

    mock_extractor = MagicMock()
    mock_extractor.extract = _fake_extract

    with patch(
        "vektra_ingest.pipeline.detect_content_type", return_value="application/pdf"
    ):
        with patch(
            "vektra_ingest.pipeline._get_extractor", return_value=mock_extractor
        ):
            with patch("vektra_ingest.pipeline.FixedSizeChunking") as mock_cc:
                mc = MagicMock()
                mc.chunk = _fake_chunk
                mock_cc.return_value = mc

                with patch(
                    "vektra_ingest.pipeline._cleanup_document", new_callable=AsyncMock
                ):
                    with pytest.raises(IngestError):
                        await run_ingest(
                            file_content=b"%PDF-1.4 fake",
                            filename="test.pdf",
                            namespace="default",
                            session=session,
                            registry=registry,
                        )
