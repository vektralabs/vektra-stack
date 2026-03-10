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
            error_message=None,
            created_at="2026-03-02T00:00:00",
            completed_at=None,
        )
        assert resp.status == "running"
        assert resp.processed_documents == 3

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


class TestRunReindex:
    @pytest.mark.asyncio
    async def test_run_reindex_updates_progress(self):
        """Verify that run_reindex re-embeds chunks and updates job status."""
        from vektra_index.reindex import run_reindex

        job_id = uuid4()
        doc1_id = uuid4()
        doc2_id = uuid4()

        # Mock chunk rows returned by the chunk query
        mock_chunk1 = MagicMock(
            content="hello world",
            chunk_metadata={"document_id": str(doc1_id)},
            element_type="text",
            content_format="text",
            position=0,
            sparse_vector=None,
        )
        mock_chunk2 = MagicMock(
            content="goodbye world",
            chunk_metadata={"document_id": str(doc2_id)},
            element_type="text",
            content_format="text",
            position=0,
            sparse_vector=None,
        )

        mock_session = AsyncMock()
        execute_results = [
            MagicMock(),  # update status to running
            MagicMock(**{"scalar_one.return_value": 2}),  # count
            MagicMock(),  # update total_documents
            MagicMock(**{"all.return_value": [(doc1_id,), (doc2_id,)]}),  # doc query
            MagicMock(**{"all.return_value": [mock_chunk1]}),  # chunks for doc 1
            MagicMock(),  # pgvector store flush (doc 1)
            MagicMock(),  # update progress doc 1
            MagicMock(**{"all.return_value": [mock_chunk2]}),  # chunks for doc 2
            MagicMock(),  # pgvector store flush (doc 2)
            MagicMock(),  # update progress doc 2
            MagicMock(),  # update completed
        ]
        mock_session.execute = AsyncMock(side_effect=execute_results)
        mock_session.commit = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_factory = MagicMock(return_value=mock_session_ctx)

        # Mock embedding provider
        mock_embedding = AsyncMock()
        mock_embedding.embed_documents = AsyncMock(
            return_value=[[0.1] * 384]
        )
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

        # Verify embedding was called for both documents
        assert mock_embedding.embed_documents.call_count == 2
        assert mock_session.execute.call_count >= 4
        assert mock_session.commit.call_count >= 4

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

        with patch(
            "vektra_shared.db.get_session_factory",
            return_value=mock_factory,
        ):
            await run_reindex(
                job_id=job_id,
                namespace="default",
                source_version=1,
                target_version=2,
            )

        assert call_count == 2
