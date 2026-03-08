"""Unit tests for AnalyticsService: store, retrieve, list, metrics, delete."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from vektra_shared.types import ChunkRef, QueryTrace, StepTrace

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_trace(
    *,
    response_id: UUID | None = None,
    duration_ms: int = 250,
    llm_model: str = "gpt-4",
    prompt_version: str = "abc12345",
    chunks: list[ChunkRef] | None = None,
    created_at: datetime | None = None,
) -> QueryTrace:
    return QueryTrace(
        response_id=response_id or uuid4(),
        steps=[
            StepTrace(name="embed_query", duration_ms=10),
            StepTrace(name="vector_search", duration_ms=80, metadata={"retrieved": 5}),
            StepTrace(name="llm_call", duration_ms=150, metadata={"model": llm_model}),
        ],
        total_duration_ms=duration_ms,
        chunks_retrieved=chunks
        or [
            ChunkRef(chunk_id="chunk-1", score=0.95),
            ChunkRef(chunk_id="chunk-2", score=0.82),
        ],
        llm_model=llm_model,
        prompt_version=prompt_version,
        created_at=created_at or datetime.now(UTC),
    )


def _make_orm(
    *,
    response_id: UUID | None = None,
    namespace_id: str = "default",
    duration_ms: int = 250,
    llm_model: str = "gpt-4",
    prompt_version: str = "abc12345",
    chunks: list[dict] | None = None,
    steps: list[dict] | None = None,
    created_at: datetime | None = None,
) -> MagicMock:
    """Create a mock QueryTraceOrm row."""
    orm = MagicMock()
    orm.id = uuid4()
    orm.response_id = response_id or uuid4()
    orm.namespace_id = namespace_id
    orm.total_duration_ms = duration_ms
    orm.llm_model = llm_model
    orm.prompt_version = prompt_version
    orm.chunks_retrieved = chunks or [
        {"chunk_id": "chunk-1", "score": 0.95},
        {"chunk_id": "chunk-2", "score": 0.82},
    ]
    orm.steps = steps or [
        {"name": "embed_query", "duration_ms": 10, "metadata": {}},
        {"name": "vector_search", "duration_ms": 80, "metadata": {"retrieved": 5}},
        {"name": "llm_call", "duration_ms": 150, "metadata": {"model": llm_model}},
    ]
    orm.created_at = created_at or datetime.now(UTC)
    return orm


# ---------------------------------------------------------------------------
# store_trace
# ---------------------------------------------------------------------------


class TestStoreTrace:
    async def test_store_trace_adds_to_session(self):
        session = AsyncMock()
        session.flush = AsyncMock()

        from vektra_analytics.service import AnalyticsService

        svc = AnalyticsService()
        trace = _make_trace()

        await svc.store_trace(session, trace, namespace="test-ns")

        session.add.assert_called_once()
        session.flush.assert_awaited_once()

        orm = session.add.call_args[0][0]
        assert orm.response_id == trace.response_id
        assert orm.namespace_id == "test-ns"
        assert orm.total_duration_ms == trace.total_duration_ms
        assert orm.llm_model == trace.llm_model
        assert len(orm.steps) == 3
        assert len(orm.chunks_retrieved) == 2

    async def test_store_trace_requires_namespace(self):
        """namespace has no default - callers must provide it explicitly."""
        import inspect

        from vektra_analytics.service import AnalyticsService

        sig = inspect.signature(AnalyticsService.store_trace)
        param = sig.parameters["namespace"]
        assert param.default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# get_trace
# ---------------------------------------------------------------------------


class TestGetTrace:
    async def test_get_trace_found(self):
        rid = uuid4()
        orm_row = _make_orm(response_id=rid)

        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = orm_row
        session.execute = AsyncMock(return_value=result_mock)

        from vektra_analytics.service import AnalyticsService

        svc = AnalyticsService()
        trace = await svc.get_trace(session, rid)

        assert trace is not None
        assert trace.response_id == rid
        assert trace.llm_model == "gpt-4"
        assert len(trace.steps) == 3
        assert len(trace.chunks_retrieved) == 2
        assert trace.chunks_retrieved[0].chunk_id == "chunk-1"
        assert trace.chunks_retrieved[0].score == 0.95

    async def test_get_trace_not_found(self):
        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result_mock)

        from vektra_analytics.service import AnalyticsService

        svc = AnalyticsService()
        trace = await svc.get_trace(session, uuid4())

        assert trace is None


# ---------------------------------------------------------------------------
# list_traces
# ---------------------------------------------------------------------------


class TestListTraces:
    async def test_list_traces_returns_results(self):
        orm1 = _make_orm(duration_ms=100)
        orm2 = _make_orm(duration_ms=200)

        session = AsyncMock()
        result_mock = MagicMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [orm1, orm2]
        result_mock.scalars.return_value = scalars_mock
        session.execute = AsyncMock(return_value=result_mock)

        from vektra_analytics.service import AnalyticsService

        svc = AnalyticsService()
        traces = await svc.list_traces(session, limit=10)

        assert len(traces) == 2
        assert traces[0].total_duration_ms == 100
        assert traces[1].total_duration_ms == 200

    async def test_list_traces_empty(self):
        session = AsyncMock()
        result_mock = MagicMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = []
        result_mock.scalars.return_value = scalars_mock
        session.execute = AsyncMock(return_value=result_mock)

        from vektra_analytics.service import AnalyticsService

        svc = AnalyticsService()
        traces = await svc.list_traces(session)

        assert traces == []


# ---------------------------------------------------------------------------
# get_metrics
# ---------------------------------------------------------------------------


class TestGetMetrics:
    async def test_get_metrics_with_data(self):
        now = datetime.now(UTC)

        # Mock aggregate query result
        agg_row = MagicMock()
        agg_row.total = 10
        agg_row.avg_latency = 250.5
        agg_row.first_at = now - timedelta(hours=2)
        agg_row.last_at = now

        # Mock p95 result
        p95_scalar = 480.0

        # Mock chunks query for avg retrieval score
        chunks_rows = [
            ([{"chunk_id": "c1", "score": 0.95}, {"chunk_id": "c2", "score": 0.8}],),
            ([{"chunk_id": "c3", "score": 0.7}],),
            ([],),
        ]

        # Mock model distribution
        model_row1 = MagicMock()
        model_row1.llm_model = "gpt-4"
        model_row1.cnt = 7
        model_row2 = MagicMock()
        model_row2.llm_model = "claude-3"
        model_row2.cnt = 3

        call_count = 0

        async def mock_execute(stmt):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:  # aggregate
                result.one.return_value = agg_row
                return result
            elif call_count == 2:  # p95
                result.scalar.return_value = p95_scalar
                return result
            elif call_count == 3:  # chunks for avg retrieval score
                result.all.return_value = chunks_rows
                return result
            else:  # model distribution
                result.all.return_value = [model_row1, model_row2]
                return result

        session = AsyncMock()
        session.execute = mock_execute

        from vektra_analytics.service import AnalyticsService

        svc = AnalyticsService()
        metrics = await svc.get_metrics(session)

        assert metrics.total_queries == 10
        assert metrics.avg_latency_ms == 250.5
        assert metrics.p95_latency_ms == 480.0
        # avg retrieval score: max(0.95, 0.8)=0.95, max(0.7)=0.7, empty skipped -> (0.95+0.7)/2 = 0.825
        assert metrics.avg_retrieval_score == 0.825
        assert metrics.queries_per_hour == 5.0  # 10 queries / 2 hours
        assert metrics.model_distribution == {"gpt-4": 7, "claude-3": 3}

    async def test_get_metrics_empty(self):
        agg_row = MagicMock()
        agg_row.total = 0
        agg_row.avg_latency = None
        agg_row.first_at = None
        agg_row.last_at = None

        call_count = 0

        async def mock_execute(stmt):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:  # aggregate
                result.one.return_value = agg_row
                return result
            elif call_count == 2:  # chunks for avg retrieval score
                result.all.return_value = []
                return result
            else:  # model distribution
                result.all.return_value = []
                return result

        session = AsyncMock()
        session.execute = mock_execute

        from vektra_analytics.service import AnalyticsService

        svc = AnalyticsService()
        metrics = await svc.get_metrics(session)

        assert metrics.total_queries == 0
        assert metrics.avg_latency_ms == 0.0
        assert metrics.p95_latency_ms == 0.0
        assert metrics.avg_retrieval_score == 0.0
        assert metrics.queries_per_hour == 0.0
        assert metrics.model_distribution == {}


# ---------------------------------------------------------------------------
# delete_before
# ---------------------------------------------------------------------------


class TestDeleteBefore:
    async def test_delete_before_returns_count(self):
        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.rowcount = 3
        session.execute = AsyncMock(return_value=result_mock)

        from vektra_analytics.service import AnalyticsService

        svc = AnalyticsService()
        cutoff = datetime.now(UTC) - timedelta(days=30)
        count = await svc.delete_before(session, cutoff)

        assert count == 3
        session.execute.assert_awaited_once()

    async def test_delete_before_none_deleted(self):
        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.rowcount = 0
        session.execute = AsyncMock(return_value=result_mock)

        from vektra_analytics.service import AnalyticsService

        svc = AnalyticsService()
        cutoff = datetime.now(UTC) - timedelta(days=30)
        count = await svc.delete_before(session, cutoff)

        assert count == 0


# ---------------------------------------------------------------------------
# _orm_to_trace
# ---------------------------------------------------------------------------


class TestOrmToTrace:
    def test_converts_orm_to_trace(self):
        from vektra_analytics.service import _orm_to_trace

        orm = _make_orm(duration_ms=300, llm_model="claude-3")
        trace = _orm_to_trace(orm)

        assert trace.total_duration_ms == 300
        assert trace.llm_model == "claude-3"
        assert len(trace.steps) == 3
        assert trace.steps[0].name == "embed_query"
        assert len(trace.chunks_retrieved) == 2

    def test_handles_empty_steps_and_chunks(self):
        from vektra_analytics.service import _orm_to_trace

        orm = _make_orm()
        orm.steps = []
        orm.chunks_retrieved = []
        trace = _orm_to_trace(orm)

        assert trace.steps == []
        assert trace.chunks_retrieved == []

    def test_handles_none_steps_and_chunks(self):
        from vektra_analytics.service import _orm_to_trace

        orm = _make_orm(steps=None, chunks=None)
        # steps and chunks might be None from DB
        orm.steps = None
        orm.chunks_retrieved = None
        trace = _orm_to_trace(orm)

        assert trace.steps == []
        assert trace.chunks_retrieved == []
