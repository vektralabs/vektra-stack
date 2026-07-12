"""vektra-core FastAPI router (REQ-003, REQ-013, REQ-042, REQ-049, REQ-053, REQ-055).

Routes:
  POST /api/v1/query                        - RAG query (JSON or SSE streaming)
  GET  /api/v1/providers                    - List registered LLM providers
  GET  /api/v1/conversations/{id}           - Conversation metadata (no content)
  DELETE /api/v1/conversations/{id}         - Soft-delete a conversation
  POST /api/v1/feedback/{response_id}       - Response-level feedback
  POST /api/v1/feedback/citation/{citation_id} - Citation-level feedback

Auth: `query` or `admin` scope required for all endpoints.
SSE streaming: set Accept: text/event-stream header or body.stream=true.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from vektra_core.conversation import PersistentConversationStore
from vektra_core.models import FeedbackOrm
from vektra_shared.auth import ApiKeyInfo, require_scope
from vektra_shared.db import get_session
from vektra_shared.errors import (
    ERR_QUERY_002,
    ERR_QUERY_003,
    ErrorCategory,
    ErrorResponse,
    http_status_for,
)
from vektra_shared.namespace import resolve_citations_enabled, resolve_grounding_mode
from vektra_shared.types import (
    QueryChunk,
    QueryRequest,
    SafeguardContext,
    trace_from_dict,
)

log = structlog.get_logger(__name__)

_MAX_QUERY_CHARS = 10_000

router = APIRouter()

# Auth: require_scope("query") accepts query and admin keys (ARCH-059
# admin-as-superscope), and includes rate limiting integration.


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
    document_name: str | None = None  # FEAT-012
    title: str | None = None  # FEAT-021: set when the namespace cites sources


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


class ConversationMetadata(BaseModel):
    id: UUID
    namespace_id: str
    created_at: datetime
    updated_at: datetime
    turn_count: int
    title: str | None = None


class FeedbackBody(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    comment: str | None = None
    namespace: str = "default"


class CitationFeedbackBody(BaseModel):
    response_id: UUID
    rating: int = Field(..., ge=1, le=5)
    comment: str | None = None
    namespace: str = "default"


class FeedbackCreated(BaseModel):
    id: UUID


# ---------------------------------------------------------------------------
# SSE streaming helper
# ---------------------------------------------------------------------------


async def _sse_generator(
    stream: AsyncGenerator[QueryChunk, None],
    request: Request,
    *,
    analytics_service: Any | None = None,
    db_session_factory: Any | None = None,
    namespace: str = "default",
    store_traces: bool = False,
) -> AsyncGenerator[str, None]:
    """Format QueryChunk events as SSE lines.

    Polls request.is_disconnected() between token yields to detect
    client disconnect and close the LLM stream iterator (DEBT-005).
    """
    try:
        async for chunk in stream:
            if await request.is_disconnected():
                log.info("client_disconnected_during_stream")
                break

            if chunk.type == "token":
                yield f"data: {chunk.data}\n\n"
            elif chunk.type in ("sources", "error", "trace"):
                payload = json.dumps({"type": chunk.type, "data": chunk.data})
                yield f"data: {payload}\n\n"
                # Persist trace (best-effort, BUG-013)
                if (
                    chunk.type == "trace"
                    and store_traces
                    and analytics_service
                    and db_session_factory
                ):
                    try:
                        trace_obj = trace_from_dict(chunk.data)  # type: ignore[arg-type]
                        async with db_session_factory() as sess:
                            await analytics_service.store_trace(
                                sess, trace_obj, namespace=namespace
                            )
                            await sess.commit()
                    except Exception:
                        log.warning("stream_trace_store_failed", exc_info=True)
            elif chunk.type == "done":
                yield "data: [DONE]\n\n"
    finally:
        # Close the underlying async generator to release LLM resources
        await stream.aclose()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/api/v1/query", response_model=None)
async def query(
    body: QueryBody,
    request: Request,
    _key: ApiKeyInfo = Depends(require_scope("query")),
) -> Any:
    """Run a RAG query.

    Returns JSON QueryResponse by default.
    Returns SSE stream (text/event-stream) when Accept header contains
    'text/event-stream' or body.stream=true.
    """
    # Query length validation (ERR-QUERY-003)
    if len(body.question) > _MAX_QUERY_CHARS:
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code=ERR_QUERY_003,
            message=f"Query length {len(body.question)} characters exceeds the maximum of {_MAX_QUERY_CHARS}.",
            remediation="Shorten your query or split it into multiple smaller queries.",
        )
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())

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
                detail={
                    "error": {
                        "code": "ERR-SAFEGUARD-001",
                        "message": sg_result.reason or "Query blocked",
                    }
                },
            )
    except ValueError:
        pass  # safeguard not registered (optional in tests/dev)

    # Determine streaming mode
    accept = request.headers.get("accept", "")
    use_stream = body.stream or "text/event-stream" in accept

    # Ensure conversation row exists for persistent multi-turn (BUG-014).
    # The API layer creates the row because it has namespace_id and key_id,
    # which the pipeline does not (and should not) receive.
    conversation_id = body.conversation_id
    try:
        conv_store = registry.get("conversation_store", "default")
        if isinstance(conv_store, PersistentConversationStore):
            if conversation_id is None:
                conversation_id = await conv_store.create_conversation(
                    namespace_id=body.namespace,
                    key_id=_key.key_id,
                )
            else:
                # Client-provided ID: create row if it doesn't exist yet.
                await conv_store.ensure_conversation(
                    conversation_id=conversation_id,
                    namespace_id=body.namespace,
                    key_id=_key.key_id,
                )
    except ValueError:
        pass  # conversation store not registered (optional)
    except Exception as exc:
        log.warning("conversation_create_failed", error=str(exc))

    # Resolve grounding mode: namespace config > env var > default (FEAT-020)
    db_factory = getattr(request.app.state, "db_session_factory", None)
    _default_mode = getattr(request.app.state, "grounding_mode_default", "strict")
    if db_factory:
        grounding_mode = await resolve_grounding_mode(
            body.namespace, db_factory, default_mode=_default_mode
        )
    else:
        grounding_mode = _default_mode

    # Resolve citations: namespace config > default false (FEAT-021)
    citations_enabled = False
    if db_factory:
        citations_enabled = await resolve_citations_enabled(body.namespace, db_factory)

    query_req = QueryRequest(
        question=body.question,
        namespace=body.namespace,
        conversation_id=conversation_id,
        top_k=body.top_k,
        stream=use_stream,
        grounding_mode=grounding_mode,
        citations_enabled=citations_enabled,
    )

    if use_stream:
        stream_iter = await pipeline.execute_stream(query_req)
        return StreamingResponse(
            _sse_generator(
                stream_iter,
                request,
                analytics_service=getattr(request.app.state, "analytics_service", None),
                db_session_factory=getattr(
                    request.app.state, "db_session_factory", None
                ),
                namespace=body.namespace,
                store_traces=getattr(request.app.state, "store_traces_enabled", False)
                is True,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Non-streaming: full response
    try:
        response, trace = await pipeline.execute(query_req)
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("query_pipeline_failed", error=str(exc), exc_info=True)
        err = ErrorResponse(
            category=ErrorCategory.UPSTREAM,
            code=ERR_QUERY_002,
            message="LLM is unavailable. The query could not be completed.",
            remediation=(
                "Check that the LLM provider is running and accessible. "
                "Verify VEKTRA_LLM_PROVIDER is configured correctly. "
                "Retry the query in a few seconds."
            ),
        )
        raise HTTPException(
            status_code=http_status_for(err), detail=err.to_envelope()
        ) from exc

    log.info(
        "query_trace",
        response_id=str(trace.response_id),
        total_duration_ms=trace.total_duration_ms,
        llm_model=trace.llm_model,
        prompt_version=trace.prompt_version,
        chunks_retrieved=len(trace.chunks_retrieved),
        steps=[{"name": s.name, "duration_ms": s.duration_ms} for s in trace.steps],
    )

    # Persist trace (best-effort, BUG-013)
    if getattr(request.app.state, "store_traces_enabled", False) is True:
        try:
            svc = getattr(request.app.state, "analytics_service", None)
            factory = getattr(request.app.state, "db_session_factory", None)
            if svc and factory:
                async with factory() as sess:
                    await svc.store_trace(sess, trace, namespace=body.namespace)
                    await sess.commit()
        except Exception:
            log.warning(
                "trace_store_failed",
                response_id=str(trace.response_id),
                exc_info=True,
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
                document_name=s.document_name,
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
    _key: ApiKeyInfo = Depends(require_scope("query")),
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


# ---------------------------------------------------------------------------
# Conversation endpoints (REQ-049, REQ-051)
# ---------------------------------------------------------------------------


def _get_conversation_store(request: Request) -> PersistentConversationStore:
    """Get the PersistentConversationStore from the registry.

    Raises 503 if conversations are not persisted (in-memory fallback).
    """
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        raise HTTPException(status_code=500, detail="ProviderRegistry not initialized")

    try:
        store = registry.get("conversation_store", "default")
    except ValueError:
        raise HTTPException(
            status_code=503,
            detail="Persistent conversation storage is not configured",
        )

    if not isinstance(store, PersistentConversationStore):
        raise HTTPException(
            status_code=503,
            detail="Persistent conversation storage is not configured",
        )

    return store


@router.get(
    "/api/v1/conversations/{conversation_id}",
    response_model=ConversationMetadata,
)
async def get_conversation(
    conversation_id: UUID,
    request: Request,
    _key: ApiKeyInfo = Depends(require_scope("query")),
) -> ConversationMetadata:
    """Return conversation metadata. Never returns content (REQ-051)."""
    store = _get_conversation_store(request)
    meta = await store.get_metadata(conversation_id, namespace=_key.namespace_id)
    if meta is None or meta.get("deleted_at") is not None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return ConversationMetadata(
        id=meta["id"],
        namespace_id=meta["namespace_id"],
        created_at=meta["created_at"],
        updated_at=meta["updated_at"],
        turn_count=meta["turn_count"],
        title=meta["title"],
    )


@router.delete(
    "/api/v1/conversations/{conversation_id}",
    status_code=204,
)
async def delete_conversation(
    conversation_id: UUID,
    request: Request,
    _key: ApiKeyInfo = Depends(require_scope("query")),
) -> Response:
    """Soft-delete a conversation and all its turns."""
    store = _get_conversation_store(request)
    deleted = await store.soft_delete(conversation_id, namespace=_key.namespace_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")

    log.info(
        "conversation_deleted",
        conversation_id=str(conversation_id),
        key_id=str(_key.key_id),
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Feedback endpoints (REQ-055)
# ---------------------------------------------------------------------------


@router.post(
    "/api/v1/feedback/{response_id}",
    response_model=FeedbackCreated,
    status_code=201,
)
async def submit_feedback(
    response_id: UUID,
    body: FeedbackBody,
    request: Request,
    _key: ApiKeyInfo = Depends(require_scope("query")),
    session: AsyncSession = Depends(get_session),
) -> FeedbackCreated:
    """Submit response-level feedback (rating 1-5 with optional comment)."""
    namespace = _key.namespace_id or body.namespace
    stmt = (
        insert(FeedbackOrm)
        .values(
            response_id=response_id,
            citation_id=None,
            namespace_id=namespace,
            key_id=_key.key_id,
            rating=body.rating,
            comment=body.comment,
        )
        .returning(FeedbackOrm.id)
    )
    result = await session.execute(stmt)
    feedback_id = result.scalar_one()
    await session.commit()

    log.info(
        "feedback_submitted",
        feedback_id=str(feedback_id),
        response_id=str(response_id),
        rating=body.rating,
    )
    return FeedbackCreated(id=feedback_id)


@router.post(
    "/api/v1/feedback/citation/{citation_id}",
    response_model=FeedbackCreated,
    status_code=201,
)
async def submit_citation_feedback(
    citation_id: UUID,
    body: CitationFeedbackBody,
    request: Request,
    _key: ApiKeyInfo = Depends(require_scope("query")),
    session: AsyncSession = Depends(get_session),
) -> FeedbackCreated:
    """Submit citation-level feedback (rating 1-5 with optional comment)."""
    namespace = _key.namespace_id or body.namespace
    stmt = (
        insert(FeedbackOrm)
        .values(
            response_id=body.response_id,
            citation_id=citation_id,
            namespace_id=namespace,
            key_id=_key.key_id,
            rating=body.rating,
            comment=body.comment,
        )
        .returning(FeedbackOrm.id)
    )
    result = await session.execute(stmt)
    feedback_id = result.scalar_one()
    await session.commit()

    log.info(
        "citation_feedback_submitted",
        feedback_id=str(feedback_id),
        citation_id=str(citation_id),
        rating=body.rating,
    )
    return FeedbackCreated(id=feedback_id)
