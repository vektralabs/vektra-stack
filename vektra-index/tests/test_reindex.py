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
        """Verify that run_reindex updates job status."""
        from vektra_index.reindex import run_reindex

        job_id = uuid4()

        mock_session = AsyncMock()
        execute_results = [
            MagicMock(),  # update status to running
            MagicMock(**{"scalar_one.return_value": 2}),  # count
            MagicMock(),  # update total_documents
            MagicMock(**{"all.return_value": [(uuid4(),), (uuid4(),)]}),  # doc query
            MagicMock(),  # update progress doc 1
            MagicMock(),  # update progress doc 2
            MagicMock(),  # update completed
        ]
        mock_session.execute = AsyncMock(side_effect=execute_results)
        mock_session.commit = AsyncMock()

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_factory = MagicMock(return_value=mock_session_ctx)

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
