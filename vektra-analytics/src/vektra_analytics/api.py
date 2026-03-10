"""FastAPI router for analytics endpoints.

All endpoints require admin scope. Analytics data is operational,
not user-facing (ARCH-041, ADR-0017).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from vektra_analytics.service import AnalyticsService, MetricsResponse
from vektra_shared.auth import ApiKeyInfo, require_scope
from vektra_shared.errors import (
    ERR_ANALYTICS_001,
    ERR_ANALYTICS_002,
    ErrorCategory,
    ErrorResponse,
    http_status_for,
)
from vektra_shared.types import QueryTrace

router = APIRouter(prefix="/api/v1", tags=["analytics"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class TraceResponse(BaseModel):
    """Single QueryTrace serialized for the API."""

    response_id: UUID
    steps: list[dict[str, Any]]
    total_duration_ms: int
    chunks_retrieved: list[dict[str, Any]]
    llm_model: str
    prompt_version: str
    created_at: datetime


class TraceListResponse(BaseModel):
    """Paginated list of traces."""

    items: list[TraceResponse]
    count: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trace_to_response(t: QueryTrace) -> TraceResponse:
    return TraceResponse(
        response_id=t.response_id,
        steps=[
            {"name": s.name, "duration_ms": s.duration_ms, "metadata": s.metadata}
            for s in t.steps
        ],
        total_duration_ms=t.total_duration_ms,
        chunks_retrieved=[
            {"chunk_id": c.chunk_id, "score": c.score} for c in t.chunks_retrieved
        ],
        llm_model=t.llm_model,
        prompt_version=t.prompt_version,
        created_at=t.created_at,
    )


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _get_service(request: Request) -> AnalyticsService:
    """Retrieve AnalyticsService from app state."""
    svc: AnalyticsService | None = getattr(request.app.state, "analytics_service", None)
    if svc is None or not isinstance(svc, AnalyticsService):
        err = ErrorResponse(
            category=ErrorCategory.TRANSIENT,
            code=ERR_ANALYTICS_001,
            message="Analytics service is not available.",
            remediation="The service may be starting up. Try again shortly.",
        )
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())
    return svc


async def _get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """Yield a request-scoped database session from app state factory.

    The session factory is set by the application bootstrap (infra-phase2).
    """
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        err = ErrorResponse(
            category=ErrorCategory.TRANSIENT,
            code=ERR_ANALYTICS_001,
            message="Analytics database session is not configured.",
            remediation="The service may be starting up. Try again shortly.",
        )
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())
    async with factory() as session:
        yield session


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/traces", response_model=TraceListResponse)
async def list_traces(
    namespace: str | None = Query(None),
    from_dt: datetime | None = Query(None, alias="from"),
    to_dt: datetime | None = Query(None, alias="to"),
    llm_model: str | None = Query(None, alias="model"),
    min_duration_ms: int | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _key: ApiKeyInfo = Depends(require_scope("admin")),
    service: AnalyticsService = Depends(_get_service),
    session: AsyncSession = Depends(_get_session),
) -> TraceListResponse:
    """List traces with filters (namespace, time range, model, duration, pagination)."""
    traces = await service.list_traces(
        session,
        namespace=namespace,
        from_dt=from_dt,
        to_dt=to_dt,
        llm_model=llm_model,
        min_duration_ms=min_duration_ms,
        limit=limit,
        offset=offset,
    )
    items = [_trace_to_response(t) for t in traces]
    return TraceListResponse(items=items, count=len(items))


@router.get("/traces/{response_id}", response_model=TraceResponse)
async def get_trace(
    response_id: UUID,
    _key: ApiKeyInfo = Depends(require_scope("admin")),
    service: AnalyticsService = Depends(_get_service),
    session: AsyncSession = Depends(_get_session),
) -> TraceResponse:
    """Get a single trace by response_id."""
    trace = await service.get_trace(session, response_id)
    if trace is None:
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code=ERR_ANALYTICS_002,
            message=f"Trace with response_id '{response_id}' not found.",
            remediation="Verify the response_id is correct and the trace has been stored.",
        )
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())
    return _trace_to_response(trace)


@router.get("/metrics", response_model=MetricsResponse)
async def get_metrics(
    namespace: str | None = Query(None),
    from_dt: datetime | None = Query(None, alias="from"),
    to_dt: datetime | None = Query(None, alias="to"),
    _key: ApiKeyInfo = Depends(require_scope("admin")),
    service: AnalyticsService = Depends(_get_service),
    session: AsyncSession = Depends(_get_session),
) -> MetricsResponse:
    """Aggregated metrics: avg/p95 latency, retrieval quality, throughput, model distribution."""
    return await service.get_metrics(
        session,
        namespace=namespace,
        from_dt=from_dt,
        to_dt=to_dt,
    )
