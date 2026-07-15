"""Unit tests for reindex API (T15-T16)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


class TestReindexModels:
    """Test request/response model validation."""

    def test_reindex_status_response_model(self):
        from vektra_index.reindex import ReindexStatusResponse

        resp = ReindexStatusResponse(
            job_id="abc",
            status="running",
            namespace="default",
            source_index_version=1,
            target_index_version=2,
            total_documents=10,
            processed_documents=3,
            chunks_reindexed=42,
            error_message=None,
            created_at="2026-03-02T00:00:00",
            completed_at=None,
        )
        assert resp.status == "running"
        assert resp.processed_documents == 3
        assert resp.chunks_reindexed == 42

    def test_reindex_response_model(self):
        from vektra_index.reindex import ReindexResponse

        resp = ReindexResponse(
            job_id="abc",
            status="pending",
            namespace="default",
            target_index_version=2,
        )
        assert resp.status == "pending"

    def test_reindex_request_validation(self):
        from vektra_index.reindex import ReindexRequest

        req = ReindexRequest(namespace="test", target_index_version=3)
        assert req.namespace == "test"
        assert req.target_index_version == 3

    def test_reindex_request_default_namespace(self):
        from vektra_index.reindex import ReindexRequest

        req = ReindexRequest(target_index_version=2)
        assert req.namespace == "default"


def _make_reindex_env(doc_ids, chunks_by_doc, total=None):
    """Wire the session + registry run_reindex needs.

    Chunks come from the vector store, not from SQL: the only rows the session
    still serves are the job record and the document list (ADR-0026).
    """
    from vektra_shared.types import StoredChunk

    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            MagicMock(),  # update status to running
            MagicMock(**{"scalar_one.return_value": len(doc_ids)}),  # count
            MagicMock(),  # update total_documents
            MagicMock(**{"all.return_value": [(d,) for d in doc_ids]}),  # doc query
            # progress update per document, then the terminal update
            *[MagicMock() for _ in range(len(doc_ids) + 1)],
        ]
    )
    session.commit = AsyncMock()

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)

    embedding = AsyncMock()
    embedding.embed_documents = AsyncMock(
        side_effect=lambda texts: [[0.1] * 384 for _ in texts]
    )

    vector_store = AsyncMock()
    vector_store.list_chunks = AsyncMock(
        side_effect=lambda ns, doc_id: [
            StoredChunk(**c) for c in chunks_by_doc.get(doc_id, [])
        ]
    )
    vector_store.store = AsyncMock(
        side_effect=lambda ns, chunks, **kw: [c.chunk_id for c in chunks]
    )

    registry = MagicMock()
    registry.get = MagicMock(
        side_effect=lambda category, name: {
            "embedding": embedding,
            "vector_store": vector_store,
        }[category]
    )

    return session, factory, embedding, vector_store, registry


class TestRunReindex:
    @pytest.mark.asyncio
    async def test_run_reindex_reads_and_writes_through_the_active_store(self):
        """Reindex goes through the registry's vector store, not Postgres.

        Reading chunks from Postgres and writing them back through a hardcoded
        PgvectorProvider is what made reindex a silent no-op in Qdrant mode
        (BUG-023): it read an empty table and reported "completed".
        """
        from vektra_index.reindex import run_reindex

        doc1_id, doc2_id = uuid4(), uuid4()
        _session, factory, embedding, vector_store, registry = _make_reindex_env(
            [doc1_id, doc2_id],
            {
                doc1_id: [{"chunk_id": str(uuid4()), "text": "hello", "position": 0}],
                doc2_id: [{"chunk_id": str(uuid4()), "text": "goodbye", "position": 0}],
            },
        )

        with patch("vektra_shared.db.get_session_factory", return_value=factory):
            await run_reindex(
                job_id=uuid4(),
                namespace="default",
                source_version=1,
                target_version=2,
                registry=registry,
            )

        assert embedding.embed_documents.call_count == 2
        assert vector_store.list_chunks.await_count == 2
        assert vector_store.store.await_count == 2
        # Every write lands on the target version, alongside the live one
        for call in vector_store.store.await_args_list:
            assert call.kwargs["index_version"] == 2

    @pytest.mark.asyncio
    async def test_run_reindex_fails_when_it_stores_nothing(self):
        """A reindex over a non-empty namespace that wrote nothing has failed.

        Reporting "completed" here is exactly what BUG-023 was: the operator
        could not tell a broken reindex from a real one.
        """
        from vektra_index.reindex import run_reindex

        doc_id = uuid4()
        session, factory, _embedding, vector_store, registry = _make_reindex_env(
            [doc_id], {doc_id: []}
        )

        with patch("vektra_shared.db.get_session_factory", return_value=factory):
            await run_reindex(
                job_id=uuid4(),
                namespace="default",
                source_version=1,
                target_version=2,
                registry=registry,
            )

        vector_store.store.assert_not_awaited()
        # The terminal write marks the job failed, not completed
        final_values = session.execute.await_args.args[0].compile().params
        assert final_values["status"] == "failed"
        assert "stored no chunks" in final_values["error_message"]

    @pytest.mark.asyncio
    async def test_run_reindex_remaps_parent_links(self):
        """Chunk ids change with the version, so parent links must follow.

        Otherwise FEAT-017 parent expansion in the reindexed version would point
        at the source version's chunks.
        """
        from vektra_index.reindex import run_reindex

        doc_id = uuid4()
        parent_id, child_id = str(uuid4()), str(uuid4())
        _session, factory, _embedding, vector_store, registry = _make_reindex_env(
            [doc_id],
            {
                doc_id: [
                    {"chunk_id": parent_id, "text": "parent", "position": 0},
                    {
                        "chunk_id": child_id,
                        "text": "child",
                        "position": 1,
                        "parent_id": parent_id,
                    },
                ]
            },
        )

        with patch("vektra_shared.db.get_session_factory", return_value=factory):
            await run_reindex(
                job_id=uuid4(),
                namespace="default",
                source_version=1,
                target_version=2,
                registry=registry,
            )

        stored = vector_store.store.await_args.args[1]
        new_parent, new_child = stored[0], stored[1]
        # Ids are rewritten for the target version, and the child follows
        assert new_parent.chunk_id != parent_id
        assert new_child.parent_id == new_parent.chunk_id

    @pytest.mark.asyncio
    async def test_run_reindex_refuses_stale_source_version(self):
        """list_chunks() reads the store's active version, so reindexing from a
        different source version would silently rewrite the wrong chunks."""
        from vektra_index.reindex import run_reindex

        doc_id = uuid4()
        _session, factory, _embedding, vector_store, registry = _make_reindex_env(
            [doc_id], {doc_id: [{"chunk_id": str(uuid4()), "text": "x", "position": 0}]}
        )

        with patch("vektra_shared.db.get_session_factory", return_value=factory):
            await run_reindex(
                job_id=uuid4(),
                namespace="default",
                source_version=7,  # the store reads version 1
                target_version=8,
                registry=registry,
            )

        vector_store.list_chunks.assert_not_awaited()
        vector_store.store.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_reindex_marks_failed_on_error(self):
        """Verify that run_reindex marks job as failed on exception."""
        from vektra_index.reindex import run_reindex

        job_id = uuid4()
        call_count = 0

        def make_session_ctx():
            nonlocal call_count
            call_count += 1
            ctx = AsyncMock()
            if call_count == 1:
                failing_session = AsyncMock()
                failing_session.execute = AsyncMock(side_effect=Exception("DB gone"))
                failing_session.commit = AsyncMock()
                ctx.__aenter__ = AsyncMock(return_value=failing_session)
            else:
                error_session = AsyncMock()
                error_session.execute = AsyncMock()
                error_session.commit = AsyncMock()
                ctx.__aenter__ = AsyncMock(return_value=error_session)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        mock_factory = MagicMock(side_effect=make_session_ctx)

        mock_embedding = AsyncMock()
        mock_embedding.embed_documents = AsyncMock(return_value=[[0.1] * 384])
        mock_registry = MagicMock()
        mock_registry.get = MagicMock(return_value=mock_embedding)

        with patch(
            "vektra_shared.db.get_session_factory",
            return_value=mock_factory,
        ):
            await run_reindex(
                job_id=job_id,
                namespace="default",
                source_version=1,
                target_version=2,
                registry=mock_registry,
            )

        assert call_count == 2


class TestReindexErrorEnvelope:
    """DEBT-033: the reindex endpoints now raise the REQ-010 envelope."""

    def test_target_conflict_is_400_with_index_003(self):
        from vektra_index.reindex import _reindex_target_conflict

        exc = _reindex_target_conflict(2, 2)
        assert exc.status_code == 400
        assert exc.detail["error"]["code"] == "ERR-INDEX-003"
        assert exc.detail["error"]["details"]["active_index_version"] == 2

    def test_job_not_found_is_404_with_index_004(self):
        from vektra_index.reindex import _reindex_job_not_found

        job_id = uuid4()
        exc = _reindex_job_not_found(job_id)
        assert exc.status_code == 404
        assert exc.detail["error"]["code"] == "ERR-INDEX-004"
        assert exc.detail["error"]["details"]["job_id"] == str(job_id)
