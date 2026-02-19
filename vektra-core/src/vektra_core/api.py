"""vektra-core FastAPI router (REQ-003, REQ-013, REQ-042, REQ-053).

Routes:
  POST /api/v1/query    - RAG query (JSON or SSE streaming)
  GET  /api/v1/providers - List registered LLM providers with health status

Auth: `query` or `admin` scope required for both endpoints.
SSE streaming: set Accept: text/event-stream header or body.stream=true.
"""
from __future__ import annotations

import json
from typing import Any, AsyncGenerator
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from vektra_shared.auth import ApiKeyInfo
from vektra_shared.errors import auth_insufficient_scope, auth_invalid_token, http_status_for
from vektra_shared.types import QueryChunk, QueryRequest, SafeguardContext

log = structlog.get_logger(__name__)

router = APIRouter()
_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Auth dependency: query OR admin scope
# ---------------------------------------------------------------------------


async def _require_query_scope(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> ApiKeyInfo:
    """Dependency: accepts keys with 'query' or 'admin' scope."""
    if credentials is None:
        err = auth_invalid_token()
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())

    token = credentials.credentials
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        raise HTTPException(status_code=500, detail="ProviderRegistry not initialized")

    try:
        key_store = registry.get("key_store", "default")
    except ValueError:
        raise HTTPException(status_code=500, detail="Key store not configured")

    info = await key_store.lookup_by_token(token)
    if info is None:
        err = auth_invalid_token()
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())

    if not (info.has_scope("query") or info.has_scope("admin")):
        err = auth_insufficient_scope("query")
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())

    request.state.key_id = info.key_id
    return info


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class QueryBody(BaseModel):
    question: str
    conversation_id: UUID | None = None
    namespace: str = "default"
    top_k: int = 5
    stream: bool = False


class SourceRefBody(BaseModel):
    doc_id: UUID
    chunk_id: str
    score: float
    snippet: str
    citation_id: UUID
    document_version: int = 1


class QueryResponseBody(BaseModel):
    response_id: UUID
    answer: str | None
    sources: list[SourceRefBody]
    conversation_id: UUID | None
    context_only: bool = False
    no_relevant_context: bool = False


class ProviderInfo(BaseModel):
    name: str
    model: str
    status: str
    message: str | None = None


# ---------------------------------------------------------------------------
# SSE streaming helper
# ---------------------------------------------------------------------------


async def _sse_generator(
    stream: AsyncGenerator[QueryChunk, None],
) -> AsyncGenerator[str, None]:
    """Format QueryChunk events as SSE lines."""
    async for chunk in stream:
        if chunk.type == "token":
            # Plain text token: data field is the token string
            yield f"data: {chunk.data}\n\n"
        elif chunk.type in ("sources", "error"):
            payload = json.dumps({"type": chunk.type, "data": chunk.data})
            yield f"data: {payload}\n\n"
        elif chunk.type == "done":
            yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/api/v1/query", response_model=None)
async def query(
    body: QueryBody,
    request: Request,
    _key: ApiKeyInfo = Depends(_require_query_scope),
) -> Any:
    """Run a RAG query.

    Returns JSON QueryResponse by default.
    Returns SSE stream (text/event-stream) when Accept header contains
    'text/event-stream' or body.stream=true.
    """
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        raise HTTPException(status_code=500, detail="ProviderRegistry not initialized")

    try:
        pipeline = registry.get("query_pipeline", "default")
    except ValueError:
        raise HTTPException(status_code=503, detail="Query pipeline not configured")

    # Safeguard pre_query (input validation trust boundary, REQ-044)
    try:
        safeguard = registry.get("safeguard", "default")
        sg_ctx = SafeguardContext(
            namespace=body.namespace,
            conversation_id=body.conversation_id,
            key_scope=_key.scopes[0] if _key.scopes else "query",
        )
        sg_result = await safeguard.pre_query(body.question, sg_ctx)
        if not sg_result.allowed:
            raise HTTPException(
                status_code=400,
                detail={"error": {"code": "ERR-SAFEGUARD-001", "message": sg_result.reason or "Query blocked"}},
            )
    except ValueError:
        pass  # safeguard not registered (optional in tests/dev)

    # Determine streaming mode
    accept = request.headers.get("accept", "")
    use_stream = body.stream or "text/event-stream" in accept

    query_req = QueryRequest(
        question=body.question,
        namespace=body.namespace,
        conversation_id=body.conversation_id,
        top_k=body.top_k,
        stream=use_stream,
    )

    if use_stream:
        stream_iter = await pipeline.execute_stream(query_req)
        return StreamingResponse(
            _sse_generator(stream_iter),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Non-streaming: full response
    response, trace = await pipeline.execute(query_req)

    log.info(
        "query_trace",
        response_id=str(trace.response_id),
        total_duration_ms=trace.total_duration_ms,
        llm_model=trace.llm_model,
        prompt_version=trace.prompt_version,
        chunks_retrieved=len(trace.chunks_retrieved),
        steps=[{"name": s.name, "duration_ms": s.duration_ms} for s in trace.steps],
    )

    return QueryResponseBody(
        response_id=response.response_id,
        answer=response.answer,
        sources=[
            SourceRefBody(
                doc_id=s.doc_id,
                chunk_id=s.chunk_id,
                score=s.score,
                snippet=s.snippet,
                citation_id=s.citation_id,
                document_version=s.document_version,
            )
            for s in response.sources
        ],
        conversation_id=response.conversation_id,
        context_only=response.context_only,
        no_relevant_context=response.no_relevant_context,
    )


@router.get("/api/v1/providers", response_model=list[ProviderInfo])
async def list_providers(
    request: Request,
    _key: ApiKeyInfo = Depends(_require_query_scope),
) -> list[ProviderInfo]:
    """List registered LLM providers with current health status."""
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return []

    providers: list[ProviderInfo] = []
    for name in registry.list("llm"):
        llm = registry.get("llm", name)
        model = getattr(llm, "model_name", name)
        health = await llm.health_check()
        providers.append(
            ProviderInfo(
                name=name,
                model=model,
                status=health.status,
                message=health.message,
            )
        )
    return providers
