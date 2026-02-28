"""Unit tests for arq task job status transitions (ADR-0006, NFR-005)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from vektra_ingest.pipeline import IngestResult

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_registry():
    reg = MagicMock()
    reg.get.side_effect = lambda *a, **kw: AsyncMock()
    return reg


# ---------------------------------------------------------------------------
# Job status transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_marks_job_processing_then_indexed():
    """Successful task: job transitions pending → processing → indexed."""
    from vektra_ingest.jobs import ingest_document_task

    job_id = str(uuid4())
    registry = _make_registry()
    ctx = {"registry": registry}

    indexed_result = IngestResult(
        status="indexed",
        document_id=uuid4(),
        chunk_count=10,
    )

    # Track _update_job calls
    update_calls: list[dict] = []

    async def _mock_update(jid, **kwargs):
        update_calls.append({"job_id": jid, **kwargs})

    with patch("vektra_ingest.jobs._update_job", side_effect=_mock_update):
        with patch("vektra_ingest.jobs.run_ingest", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = indexed_result

            with patch("vektra_shared.db._session_factory") as mock_factory:
                session = AsyncMock()
                mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
                mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

                # Make _session_factory callable (it's an async_sessionmaker)
                class FakeFactory:
                    def __call__(self):
                        return self

                    async def __aenter__(self):
                        return session

                    async def __aexit__(self, *a):
                        pass

                import vektra_shared.db as db_mod

                original = db_mod._session_factory
                db_mod._session_factory = FakeFactory()
                try:
                    await ingest_document_task(
                        ctx, job_id, "default", "test.pdf", b"fake content"
                    )
                finally:
                    db_mod._session_factory = original

    assert len(update_calls) == 2
    assert update_calls[0]["status"] == "processing"
    assert update_calls[0]["phase"] == "extracting"
    assert update_calls[1]["status"] == "indexed"
    assert update_calls[1]["chunk_count"] == 10


@pytest.mark.asyncio
async def test_task_marks_job_failed_on_ingest_error():
    """IngestError → job status 'failed' with error_code."""
    from vektra_ingest.exceptions import IngestError
    from vektra_ingest.jobs import ingest_document_task

    job_id = str(uuid4())
    ctx = {"registry": _make_registry()}

    update_calls: list[dict] = []

    async def _mock_update(jid, **kwargs):
        update_calls.append({"job_id": jid, **kwargs})

    with patch("vektra_ingest.jobs._update_job", side_effect=_mock_update):
        with patch("vektra_ingest.jobs.run_ingest", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = IngestError(
                error_code="ERR-INGEST-003",
                message="Scanned PDF",
            )

            class FakeFactory:
                def __call__(self):
                    return self

                async def __aenter__(self):
                    return AsyncMock()

                async def __aexit__(self, *a):
                    pass

            import vektra_shared.db as db_mod

            original = db_mod._session_factory
            db_mod._session_factory = FakeFactory()
            try:
                await ingest_document_task(
                    ctx, job_id, "default", "scanned.pdf", b"fake"
                )
            finally:
                db_mod._session_factory = original

    assert len(update_calls) == 2
    assert update_calls[1]["status"] == "failed"
    assert update_calls[1]["error_code"] == "ERR-INGEST-003"


@pytest.mark.asyncio
async def test_task_marks_job_failed_on_conflict():
    """IngestConflictError → job status 'failed' with ERR-INGEST-001."""
    from vektra_ingest.exceptions import IngestConflictError
    from vektra_ingest.jobs import ingest_document_task

    job_id = str(uuid4())
    ctx = {"registry": _make_registry()}

    update_calls: list[dict] = []

    async def _mock_update(jid, **kwargs):
        update_calls.append({"job_id": jid, **kwargs})

    with patch("vektra_ingest.jobs._update_job", side_effect=_mock_update):
        with patch("vektra_ingest.jobs.run_ingest", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = IngestConflictError(
                filename="doc.pdf", namespace="default"
            )

            class FakeFactory:
                def __call__(self):
                    return self

                async def __aenter__(self):
                    return AsyncMock()

                async def __aexit__(self, *a):
                    pass

            import vektra_shared.db as db_mod

            original = db_mod._session_factory
            db_mod._session_factory = FakeFactory()
            try:
                await ingest_document_task(
                    ctx, job_id, "default", "doc.pdf", b"content"
                )
            finally:
                db_mod._session_factory = original

    failed_calls = [c for c in update_calls if c.get("status") == "failed"]
    assert len(failed_calls) == 1
    assert failed_calls[0]["error_code"] == "ERR-INGEST-001"


@pytest.mark.asyncio
async def test_task_no_session_factory_marks_failed():
    """When _session_factory is None, task marks job as failed without crashing."""
    from vektra_ingest.jobs import ingest_document_task

    job_id = str(uuid4())
    ctx = {"registry": _make_registry()}

    update_calls: list[dict] = []

    async def _mock_update(jid, **kwargs):
        update_calls.append({"job_id": jid, **kwargs})

    with patch("vektra_ingest.jobs._update_job", side_effect=_mock_update):
        import vektra_shared.db as db_mod

        original = db_mod._session_factory
        db_mod._session_factory = None
        try:
            await ingest_document_task(ctx, job_id, "default", "test.pdf", b"content")
        finally:
            db_mod._session_factory = original

    # Should mark processing first, then fail
    statuses = [c.get("status") for c in update_calls]
    assert "failed" in statuses
