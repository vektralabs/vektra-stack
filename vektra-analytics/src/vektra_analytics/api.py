"""FastAPI router for analytics endpoints.

All endpoints require admin scope. Analytics data is operational,
not user-facing (ARCH-041, ADR-0017).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from vektra_analytics.service import AnalyticsService
from vektra_shared.auth import ApiKeyInfo, require_scope
from vektra_shared.types import QueryTrace

router = APIRouter(prefix="/api/v1", tags=["analytics"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class TraceResponse(BaseModel):
    """Single QueryTrace serialized for the API."""

    response_id: UUID
    steps: list[dict]
    total_duration_ms: int
    chunks_retrieved: list[dict]
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


def _get_service(request: Request):
    """Retrieve AnalyticsService from app state."""
    svc = getattr(request.app.state, "analytics_service", None)
    if svc is None or not isinstance(svc, AnalyticsService):
        raise HTTPException(status_code=503, detail="Analytics service not available")
    return svc


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
    service=Depends(_get_service),
) -> TraceListResponse:
    """List traces with filters (namespace, time range, model, duration, pagination)."""
    traces = await service.list_traces(
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
    service=Depends(_get_service),
) -> TraceResponse:
    """Get a single trace by response_id."""
    trace = await service.get_trace(response_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    return _trace_to_response(trace)


@router.get("/metrics")
async def get_metrics(
    namespace: str | None = Query(None),
    from_dt: datetime | None = Query(None, alias="from"),
    to_dt: datetime | None = Query(None, alias="to"),
    _key: ApiKeyInfo = Depends(require_scope("admin")),
    service=Depends(_get_service),
):
    """Aggregated metrics: avg/p95 latency, retrieval quality, throughput, model distribution."""
    return await service.get_metrics(
        namespace=namespace,
        from_dt=from_dt,
        to_dt=to_dt,
    )
