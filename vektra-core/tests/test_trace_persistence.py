"""Unit tests for QueryTrace persistence in the API layer (BUG-013, DEBT-011)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from vektra_shared.types import ChunkRef, QueryTrace, StepTrace, trace_from_dict

# ---------------------------------------------------------------------------
# trace_from_dict round-trip
# ---------------------------------------------------------------------------


def _make_trace() -> QueryTrace:
    return QueryTrace(
        response_id=uuid4(),
        steps=[
            StepTrace(name="embed_query", duration_ms=12, metadata={"dim": 384}),
            StepTrace(name="vector_search", duration_ms=45, metadata={}),
        ],
        total_duration_ms=200,
        chunks_retrieved=[
            ChunkRef(chunk_id="c1", score=0.85),
            ChunkRef(chunk_id="c2", score=0.72),
        ],
        llm_model="gpt-4o",
        prompt_version="abc12345",
        created_at=datetime(2026, 3, 28, 10, 0, 0, tzinfo=UTC),
    )


def _trace_to_dict(trace: QueryTrace) -> dict:
    """Mirror of pipeline._trace_to_dict for testing."""
    return {
        "response_id": str(trace.response_id),
        "steps": [
            {"name": s.name, "duration_ms": s.duration_ms, "metadata": s.metadata}
            for s in trace.steps
        ],
        "total_duration_ms": trace.total_duration_ms,
        "chunks_retrieved": [
            {"chunk_id": c.chunk_id, "score": c.score} for c in trace.chunks_retrieved
        ],
        "llm_model": trace.llm_model,
        "prompt_version": trace.prompt_version,
        "created_at": trace.created_at.isoformat(),
    }


def test_trace_from_dict_roundtrip():
    """trace_from_dict should reconstruct a QueryTrace from _trace_to_dict output."""
    original = _make_trace()
    d = _trace_to_dict(original)
    restored = trace_from_dict(d)

    assert restored.response_id == original.response_id
    assert restored.total_duration_ms == original.total_duration_ms
    assert restored.llm_model == original.llm_model
    assert restored.prompt_version == original.prompt_version
    assert restored.created_at == original.created_at
    assert len(restored.steps) == 2
    assert restored.steps[0].name == "embed_query"
    assert restored.steps[0].duration_ms == 12
    assert restored.steps[0].metadata == {"dim": 384}
    assert len(restored.chunks_retrieved) == 2
    assert restored.chunks_retrieved[0].chunk_id == "c1"
    assert restored.chunks_retrieved[0].score == 0.85


def test_trace_from_dict_empty_metadata():
    """Steps with missing metadata key should default to empty dict."""
    d = {
        "response_id": str(uuid4()),
        "steps": [{"name": "test", "duration_ms": 1}],
        "total_duration_ms": 1,
        "chunks_retrieved": [],
        "llm_model": "test",
        "prompt_version": "abc",
        "created_at": "2026-03-28T10:00:00+00:00",
    }
    trace = trace_from_dict(d)
    assert trace.steps[0].metadata == {}


# ---------------------------------------------------------------------------
# Non-streaming trace persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_trace_called_when_enabled():
    """When store_traces_enabled=True, analytics_service.store_trace is called."""
    from vektra_core.api import query as query_handler

    mock_svc = AsyncMock()
    mock_session = AsyncMock()
    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    trace = _make_trace()
    response = MagicMock()
    response.response_id = trace.response_id
    response.answer = "test answer"
    response.sources = []
    response.conversation_id = None
    response.context_only = False
    response.no_relevant_context = False

    mock_pipeline = AsyncMock()
    mock_pipeline.execute = AsyncMock(return_value=(response, trace))

    app_state = MagicMock()
    app_state.registry = MagicMock()
    app_state.registry.get = MagicMock(
        side_effect=lambda cat, name: {
            ("query_pipeline", "default"): mock_pipeline,
            ("safeguard", "default"): MagicMock(
                pre_query=AsyncMock(return_value=MagicMock(allowed=True))
            ),
            ("conversation_store", "default"): MagicMock(),
        }.get((cat, name), MagicMock())
    )
    app_state.store_traces_enabled = True
    app_state.analytics_service = mock_svc
    app_state.db_session_factory = mock_factory
    app_state.grounding_mode_default = "strict"

    request = MagicMock()
    request.app.state = app_state
    request.headers = {}

    body = MagicMock()
    body.question = "test question"
    body.namespace = "default"
    body.conversation_id = None
    body.top_k = 5
    body.stream = False

    key_info = MagicMock()
    key_info.scopes = ["query"]
    key_info.key_id = uuid4()

    await query_handler(body, request, key_info)

    mock_svc.store_trace.assert_called_once()
    call_args = mock_svc.store_trace.call_args
    assert call_args[0][1] == trace  # second positional arg is the trace
    assert call_args[1]["namespace"] == "default"


@pytest.mark.asyncio
async def test_store_trace_not_called_when_disabled():
    """When store_traces_enabled=False, store_trace is not called."""
    from vektra_core.api import query as query_handler

    mock_svc = AsyncMock()
    trace = _make_trace()
    response = MagicMock()
    response.response_id = trace.response_id
    response.answer = "test"
    response.sources = []
    response.conversation_id = None
    response.context_only = False
    response.no_relevant_context = False

    mock_pipeline = AsyncMock()
    mock_pipeline.execute = AsyncMock(return_value=(response, trace))

    app_state = MagicMock()
    app_state.registry = MagicMock()
    app_state.registry.get = MagicMock(
        side_effect=lambda cat, name: {
            ("query_pipeline", "default"): mock_pipeline,
            ("safeguard", "default"): MagicMock(
                pre_query=AsyncMock(return_value=MagicMock(allowed=True))
            ),
            ("conversation_store", "default"): MagicMock(),
        }.get((cat, name), MagicMock())
    )
    app_state.store_traces_enabled = False
    app_state.analytics_service = mock_svc
    app_state.grounding_mode_default = "strict"
    app_state.db_session_factory = None

    request = MagicMock()
    request.app.state = app_state
    request.headers = {}

    body = MagicMock()
    body.question = "test"
    body.namespace = "default"
    body.conversation_id = None
    body.top_k = 5
    body.stream = False

    key_info = MagicMock()
    key_info.scopes = ["query"]
    key_info.key_id = uuid4()

    await query_handler(body, request, key_info)

    mock_svc.store_trace.assert_not_called()


@pytest.mark.asyncio
async def test_store_trace_failure_does_not_propagate():
    """DB failure in store_trace should not turn a successful query into a 500."""
    from vektra_core.api import query as query_handler

    mock_svc = AsyncMock()
    mock_svc.store_trace.side_effect = RuntimeError("DB connection lost")

    mock_session = AsyncMock()
    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    trace = _make_trace()
    response = MagicMock()
    response.response_id = trace.response_id
    response.answer = "test"
    response.sources = []
    response.conversation_id = None
    response.context_only = False
    response.no_relevant_context = False

    mock_pipeline = AsyncMock()
    mock_pipeline.execute = AsyncMock(return_value=(response, trace))

    app_state = MagicMock()
    app_state.registry = MagicMock()
    app_state.registry.get = MagicMock(
        side_effect=lambda cat, name: {
            ("query_pipeline", "default"): mock_pipeline,
            ("safeguard", "default"): MagicMock(
                pre_query=AsyncMock(return_value=MagicMock(allowed=True))
            ),
            ("conversation_store", "default"): MagicMock(),
        }.get((cat, name), MagicMock())
    )
    app_state.store_traces_enabled = True
    app_state.analytics_service = mock_svc
    app_state.db_session_factory = mock_factory
    app_state.grounding_mode_default = "strict"

    request = MagicMock()
    request.app.state = app_state
    request.headers = {}

    body = MagicMock()
    body.question = "test"
    body.namespace = "default"
    body.conversation_id = None
    body.top_k = 5
    body.stream = False

    key_info = MagicMock()
    key_info.scopes = ["query"]
    key_info.key_id = uuid4()

    # Should not raise despite store_trace failure
    result = await query_handler(body, request, key_info)
    assert result is not None
