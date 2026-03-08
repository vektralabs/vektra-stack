"""AnalyticsService: QueryTrace storage, querying, and metrics aggregation.

Provides persistent storage for QueryTrace data and exposes methods for
querying traces and computing aggregated metrics. All data is GDPR-safe
by design (ARCH-041, ADR-0017): no query text, response text, or chunk
content is stored.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vektra_analytics.models import QueryTraceOrm
from vektra_shared.types import ChunkRef, QueryTrace, StepTrace

log = structlog.get_logger(__name__)


class MetricsResponse(BaseModel):
    """Aggregated analytics metrics for a time window."""

    total_queries: int
    avg_latency_ms: float
    p95_latency_ms: float
    avg_retrieval_score: float
    queries_per_hour: float
    model_distribution: dict[str, int]
    period_start: datetime
    period_end: datetime


class AnalyticsService:
    """QueryTrace storage, retrieval, and metrics aggregation.

    Methods take an explicit AsyncSession parameter (one session per request).
    The service instance is stateless and safe to store as a singleton in
    app.state. Session lifecycle is managed by the API dependency layer.
    """

    async def store_trace(
        self, session: AsyncSession, trace: QueryTrace, namespace: str
    ) -> None:
        """Persist a QueryTrace to the database."""
        orm = QueryTraceOrm(
            response_id=trace.response_id,
            namespace_id=namespace,
            steps=[
                {"name": s.name, "duration_ms": s.duration_ms, "metadata": s.metadata}
                for s in trace.steps
            ],
            total_duration_ms=trace.total_duration_ms,
            chunks_retrieved=[
                {"chunk_id": c.chunk_id, "score": c.score}
                for c in trace.chunks_retrieved
            ],
            llm_model=trace.llm_model,
            prompt_version=trace.prompt_version,
            created_at=trace.created_at,
        )
        session.add(orm)
        await session.flush()
        log.info(
            "trace_stored",
            response_id=str(trace.response_id),
            namespace=namespace,
        )

    async def get_trace(
        self, session: AsyncSession, response_id: UUID
    ) -> QueryTrace | None:
        """Retrieve a single trace by response_id."""
        stmt = select(QueryTraceOrm).where(QueryTraceOrm.response_id == response_id)
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return _orm_to_trace(row)

    async def list_traces(
        self,
        session: AsyncSession,
        namespace: str | None = None,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        llm_model: str | None = None,
        min_duration_ms: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[QueryTrace]:
        """List traces with optional filters, ordered by created_at desc."""
        stmt = select(QueryTraceOrm).order_by(
            QueryTraceOrm.created_at.desc(),
            QueryTraceOrm.response_id.desc(),
        )

        if namespace is not None:
            stmt = stmt.where(QueryTraceOrm.namespace_id == namespace)
        if from_dt is not None:
            stmt = stmt.where(QueryTraceOrm.created_at >= from_dt)
        if to_dt is not None:
            stmt = stmt.where(QueryTraceOrm.created_at <= to_dt)
        if llm_model is not None:
            stmt = stmt.where(QueryTraceOrm.llm_model == llm_model)
        if min_duration_ms is not None:
            stmt = stmt.where(QueryTraceOrm.total_duration_ms >= min_duration_ms)

        stmt = stmt.limit(limit).offset(offset)
        result = await session.execute(stmt)
        rows = result.scalars().all()
        return [_orm_to_trace(row) for row in rows]

    async def get_metrics(
        self,
        session: AsyncSession,
        namespace: str | None = None,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
    ) -> MetricsResponse:
        """Compute aggregated metrics for the given time window."""

        # Base filter
        conditions = []
        if namespace is not None:
            conditions.append(QueryTraceOrm.namespace_id == namespace)
        if from_dt is not None:
            conditions.append(QueryTraceOrm.created_at >= from_dt)
        if to_dt is not None:
            conditions.append(QueryTraceOrm.created_at <= to_dt)

        # Aggregate query
        stmt = select(
            func.count().label("total"),
            func.avg(QueryTraceOrm.total_duration_ms).label("avg_latency"),
            func.min(QueryTraceOrm.created_at).label("first_at"),
            func.max(QueryTraceOrm.created_at).label("last_at"),
        ).where(*conditions)

        result = await session.execute(stmt)
        agg = result.one()

        total: int = agg.total or 0
        avg_latency: float = float(agg.avg_latency) if agg.avg_latency else 0.0
        first_at: datetime | None = agg.first_at
        last_at: datetime | None = agg.last_at

        # p95 latency via percentile_cont (task 7)
        p95: float = 0.0
        if total > 0:
            p95_stmt = select(
                func.percentile_cont(0.95)
                .within_group(QueryTraceOrm.total_duration_ms)
                .label("p95")
            ).where(*conditions)
            p95_result = await session.execute(p95_stmt)
            p95_val = p95_result.scalar()
            p95 = float(p95_val) if p95_val else 0.0

        # Avg retrieval score: mean of max chunk score per trace
        avg_retrieval_score = await self._compute_avg_retrieval_score(
            session, conditions
        )

        # Effective period (prefer explicit window, fall back to data range)
        period_start = from_dt or first_at
        period_end = to_dt or last_at

        # Queries per hour
        queries_per_hour: float = 0.0
        if total > 0 and period_start and period_end and period_end > period_start:
            span_hours = (period_end - period_start).total_seconds() / 3600.0
            queries_per_hour = total / span_hours

        # Model distribution
        model_stmt = (
            select(
                QueryTraceOrm.llm_model,
                func.count().label("cnt"),
            )
            .where(*conditions)
            .group_by(QueryTraceOrm.llm_model)
        )
        model_result = await session.execute(model_stmt)
        model_distribution = {row.llm_model: row.cnt for row in model_result.all()}

        now = datetime.now(first_at.tzinfo) if first_at else datetime.now(UTC)
        return MetricsResponse(
            total_queries=total,
            avg_latency_ms=avg_latency,
            p95_latency_ms=p95,
            avg_retrieval_score=avg_retrieval_score,
            queries_per_hour=round(queries_per_hour, 2),
            model_distribution=model_distribution,
            period_start=period_start or now,
            period_end=period_end or now,
        )

    async def _compute_avg_retrieval_score(
        self,
        session: AsyncSession,
        conditions: list,
    ) -> float:
        """Compute mean of max chunk score per trace using application logic.

        JSONB extraction for max-per-row is simpler in Python than in SQL.
        Loads all matching chunks_retrieved into memory; bounded by retention
        policy (VEKTRA_ANALYTICS_RETENTION_DAYS via infra-phase2 cleanup job).
        """
        stmt = select(QueryTraceOrm.chunks_retrieved).where(*conditions)
        result = await session.execute(stmt)
        max_scores: list[float] = []
        for (chunks_json,) in result.all():
            if not chunks_json:
                continue
            scores = [c.get("score", 0.0) for c in chunks_json if isinstance(c, dict)]
            if scores:
                max_scores.append(max(scores))
        if not max_scores:
            return 0.0
        return round(sum(max_scores) / len(max_scores), 4)

    async def delete_before(self, session: AsyncSession, cutoff: datetime) -> int:
        """Delete traces older than the cutoff date. Returns count deleted."""
        stmt = delete(QueryTraceOrm).where(QueryTraceOrm.created_at < cutoff)
        result = await session.execute(stmt)
        count = result.rowcount
        log.info("traces_deleted", count=count, cutoff=cutoff.isoformat())
        return count


def _orm_to_trace(row: QueryTraceOrm) -> QueryTrace:
    """Convert ORM row to QueryTrace dataclass."""
    steps = [
        StepTrace(
            name=s.get("name", ""),
            duration_ms=s.get("duration_ms", 0),
            metadata=s.get("metadata", {}),
        )
        for s in (row.steps or [])
        if isinstance(s, dict)
    ]
    chunks = [
        ChunkRef(
            chunk_id=c.get("chunk_id", ""),
            score=c.get("score", 0.0),
        )
        for c in (row.chunks_retrieved or [])
        if isinstance(c, dict)
    ]
    return QueryTrace(
        response_id=row.response_id,
        steps=steps,
        total_duration_ms=row.total_duration_ms,
        chunks_retrieved=chunks,
        llm_model=row.llm_model,
        prompt_version=row.prompt_version,
        created_at=row.created_at,
    )
