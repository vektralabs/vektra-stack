"""FastAPI router for e-learning vertical endpoints.

Management endpoints (enrollments, content, tokens) require admin or ingest scope.
The query endpoint authenticates via JWT dashboard token (not API key).
"""

from __future__ import annotations

import ipaddress
import json
import socket
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
import jwt
import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vektra_learn.query import (
    CourseQueryRequest,
    build_course_query,
    pipeline_response_to_course_response,
)
from vektra_learn.service import (
    ContentIngestRequest,
    EnrollmentRequest,
    EnrollmentResponse,
    LearnService,
    TokenRequest,
    TokenResponse,
)
from vektra_shared.audit import log_event as _audit_log_event
from vektra_shared.auth import ApiKeyInfo, require_scope
from vektra_shared.errors import (
    ERR_LEARN_001,
    ERR_LEARN_002,
    ERR_LEARN_003,
    ERR_LEARN_004,
    ERR_LEARN_005,
    ERR_LEARN_006,
    ErrorCategory,
    ErrorResponse,
    http_status_for,
)
from vektra_shared.namespace import resolve_grounding_mode, resolve_show_sources
from vektra_shared.types import trace_from_dict

# Sentinel key_id for learn-originated conversations (JWT auth has no API key).
_LEARN_SENTINEL_KEY_ID = UUID("00000000-0000-0000-0000-000000000000")


def _resolve_request_id(request: Request) -> UUID:
    """Return ``request.state.request_id`` or synthesize a fallback ``uuid4()``.

    Always returns a UUID so audit logging never silently skips sensitive
    endpoints (NFR-007) when the request-id middleware misbehaves. Emits a
    structlog warning on the fallback path so misconfiguration is observable.
    """
    rid: UUID | None = getattr(request.state, "request_id", None)
    if rid is not None:
        return rid
    fallback = uuid4()
    structlog.get_logger(__name__).warning(
        "request_id_middleware_missing_fallback", fallback=str(fallback)
    )
    return fallback


router = APIRouter(prefix="/api/v1/learn", tags=["learn"])


async def _learn_sse_generator(
    stream: AsyncGenerator[Any, None],
    request: Request,
    conversation_id: str | None = None,
    *,
    analytics_service: Any | None = None,
    db_session_factory: Any | None = None,
    namespace: str = "default",
    store_traces: bool = False,
    show_sources: bool = True,
) -> AsyncGenerator[str, None]:
    """Format QueryChunk events as SSE lines for learn endpoint.

    The *show_sources* flag (FEAT-014) is attached to the ``sources`` event
    so the widget can decide whether to render citations for this response.
    The payload itself is always sent in full for analytics/debugging.
    """
    log = structlog.get_logger(__name__)
    try:
        async for chunk in stream:
            if await request.is_disconnected():
                break
            if chunk.type == "token":
                payload = json.dumps({"type": "token", "data": chunk.data})
                yield f"data: {payload}\n\n"
            elif chunk.type in ("sources", "error", "trace"):
                event: dict[str, Any] = {"type": chunk.type, "data": chunk.data}
                if chunk.type == "sources":
                    event["show_sources"] = show_sources
                payload = json.dumps(event)
                yield f"data: {payload}\n\n"
                # Persist trace (best-effort, BUG-013)
                if (
                    chunk.type == "trace"
                    and store_traces
                    and analytics_service
                    and db_session_factory
                ):
                    try:
                        trace_obj = trace_from_dict(chunk.data)
                        async with db_session_factory() as sess:
                            await analytics_service.store_trace(
                                sess, trace_obj, namespace=namespace
                            )
                            await sess.commit()
                    except Exception:
                        log.warning("stream_trace_store_failed", exc_info=True)
            elif chunk.type == "done":
                if conversation_id:
                    meta = json.dumps(
                        {"type": "done", "data": {"conversation_id": conversation_id}}
                    )
                    yield f"data: {meta}\n\n"
                yield "data: [DONE]\n\n"
    finally:
        await stream.aclose()


_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


def _resolve_and_validate_url(url: str) -> tuple[str, str]:
    """Resolve DNS once, validate IPs, return (safe_url, hostname).

    Prevents DNS rebinding by resolving the hostname to an IP, validating
    that the IP is not private/reserved, then building a URL that connects
    directly to the validated IP. The original hostname is returned so
    the caller can set the Host header for virtual-host routing.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")
    try:
        addr_info = socket.getaddrinfo(
            hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve hostname: {exc}") from exc
    if not addr_info:
        raise ValueError(f"Cannot resolve hostname: {hostname}")

    # Validate ALL resolved addresses (not just the first)
    for _family, _type, _proto, _canonname, sockaddr in addr_info:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
            raise ValueError(f"URL resolves to private/reserved address: {ip}")

    # Use first resolved IP to build a safe URL (prevents DNS rebinding)
    validated_ip = addr_info[0][4][0]
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    safe_url = f"{parsed.scheme}://{validated_ip}{port}{path}{query}"
    return safe_url, hostname


class EnrollmentListResponse(BaseModel):
    items: list[EnrollmentResponse]
    count: int


class ContentIngestResponse(BaseModel):
    status: str
    namespace: str
    course_id: str
    metadata: dict[str, Any]
    document_id: str | None = None
    chunk_count: int | None = None


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _get_service(request: Request) -> LearnService:
    """Retrieve LearnService from app state."""
    svc: LearnService | None = getattr(request.app.state, "learn_service", None)
    if svc is None or not isinstance(svc, LearnService):
        err = ErrorResponse(
            category=ErrorCategory.TRANSIENT,
            code=ERR_LEARN_001,
            message="Learn service is not available.",
            remediation="The service may be starting up. Try again shortly.",
        )
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())
    return svc


async def _get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """Yield a request-scoped database session."""
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        err = ErrorResponse(
            category=ErrorCategory.TRANSIENT,
            code=ERR_LEARN_001,
            message="Learn database session is not configured.",
            remediation="The service may be starting up. Try again shortly.",
        )
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())
    async with factory() as session:
        yield session


async def _validate_dashboard_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict[str, Any]:
    """Validate a JWT dashboard token for the query endpoint.

    Returns the decoded payload containing sub (student_id), course_id.
    """
    if credentials is None:
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code=ERR_LEARN_003,
            message="Dashboard token is missing.",
            remediation="Include a valid dashboard JWT in the Authorization header.",
        )
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())

    service = _get_service(request)
    try:
        payload = await service.validate_token(credentials.credentials)
    except jwt.InvalidTokenError as exc:
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code=ERR_LEARN_003,
            message=f"Invalid or expired dashboard token: {exc}",
            remediation="Request a new dashboard token via POST /api/v1/learn/tokens.",
        )
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())
    return payload


# ---------------------------------------------------------------------------
# Enrollment endpoints
# ---------------------------------------------------------------------------


@router.post("/enrollments", response_model=EnrollmentResponse, status_code=201)
async def create_enrollment(
    req: EnrollmentRequest,
    _key: ApiKeyInfo = Depends(require_scope("ingest")),
    service: LearnService = Depends(_get_service),
    session: AsyncSession = Depends(_get_session),
) -> EnrollmentResponse:
    """Register a student enrollment."""
    try:
        enrollment = await service.create_enrollment(session, req)
    except IntegrityError as exc:
        pgcode = getattr(exc.orig, "sqlstate", None) or getattr(
            exc.orig, "pgcode", None
        )
        if pgcode == "23505":
            err = ErrorResponse(
                category=ErrorCategory.PERMANENT,
                code=ERR_LEARN_004,
                message=f"Enrollment already exists for student '{req.student_id}' in course '{req.course_id}'.",
                remediation="Use GET /api/v1/learn/enrollments to check existing enrollments.",
            )
        elif pgcode == "23503":
            err = ErrorResponse(
                category=ErrorCategory.PERMANENT,
                code=ERR_LEARN_002,
                message=f"Namespace '{req.namespace}' does not exist.",
                remediation="Create the namespace via the admin dashboard before enrolling students.",
            )
        else:
            err = ErrorResponse(
                category=ErrorCategory.TRANSIENT,
                code=ERR_LEARN_001,
                message="Failed to create enrollment.",
                remediation="Check the request data and try again.",
            )
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())
    await session.commit()
    return enrollment


@router.get("/enrollments", response_model=EnrollmentListResponse)
async def list_enrollments(
    course_id: str | None = Query(None),
    student_id: str | None = Query(None),
    namespace: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _key: ApiKeyInfo = Depends(require_scope("admin")),
    service: LearnService = Depends(_get_service),
    session: AsyncSession = Depends(_get_session),
) -> EnrollmentListResponse:
    """List enrollments with optional filters."""
    items = await service.list_enrollments(
        session,
        course_id=course_id,
        student_id=student_id,
        namespace=namespace,
        limit=limit,
        offset=offset,
    )
    return EnrollmentListResponse(items=items, count=len(items))


@router.delete("/enrollments/{enrollment_id}", status_code=204)
async def delete_enrollment(
    enrollment_id: UUID,
    _key: ApiKeyInfo = Depends(require_scope("admin")),
    service: LearnService = Depends(_get_service),
    session: AsyncSession = Depends(_get_session),
) -> None:
    """Remove an enrollment."""
    deleted = await service.delete_enrollment(session, enrollment_id)
    if not deleted:
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code=ERR_LEARN_002,
            message=f"Enrollment '{enrollment_id}' not found.",
            remediation="Verify the enrollment ID is correct.",
        )
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())
    await session.commit()


# ---------------------------------------------------------------------------
# Content ingestion trigger
# ---------------------------------------------------------------------------


@router.post("/content/ingest", response_model=ContentIngestResponse)
async def trigger_ingest(
    req: ContentIngestRequest,
    request: Request,
    _key: ApiKeyInfo = Depends(require_scope("ingest")),
    service: LearnService = Depends(_get_service),
    session: AsyncSession = Depends(_get_session),
) -> ContentIngestResponse:
    """Trigger content ingestion with course metadata.

    When document_url is provided, fetches the file and runs it through
    the ingest pipeline with course-scoped metadata. Without a URL,
    returns the enriched metadata for manual ingestion via /api/v1/ingest.
    """
    metadata = service.build_ingest_metadata(req)

    if req.document_url:
        registry = getattr(request.app.state, "registry", None)
        if registry is None or not registry.has("ingest", "default"):
            err = ErrorResponse(
                category=ErrorCategory.TRANSIENT,
                code=ERR_LEARN_001,
                message="Ingest pipeline not available.",
                remediation="The service may be starting up. Try again shortly.",
            )
            raise HTTPException(
                status_code=http_status_for(err), detail=err.to_envelope()
            )

        # Fetch document from URL
        parsed = urlparse(req.document_url)
        if parsed.scheme not in ("http", "https"):
            err = ErrorResponse(
                category=ErrorCategory.PERMANENT,
                code=ERR_LEARN_004,
                message="Only http and https URLs are supported.",
                remediation="Provide a valid http or https URL.",
            )
            raise HTTPException(
                status_code=http_status_for(err), detail=err.to_envelope()
            )

        try:
            safe_url, original_host = _resolve_and_validate_url(req.document_url)
        except ValueError as exc:
            err = ErrorResponse(
                category=ErrorCategory.PERMANENT,
                code=ERR_LEARN_004,
                message=f"Blocked URL: {exc}",
                remediation="Provide a publicly accessible URL.",
            )
            raise HTTPException(
                status_code=http_status_for(err), detail=err.to_envelope()
            )

        try:
            max_redirects = 5
            current_url = safe_url
            current_host = original_host
            async with httpx.AsyncClient(
                timeout=30.0,
                verify=True,
                follow_redirects=False,
            ) as client:
                for _ in range(max_redirects + 1):
                    resp = await client.get(current_url, headers={"Host": current_host})
                    if resp.is_redirect:
                        location = resp.headers.get("location", "")
                        current_url, current_host = _resolve_and_validate_url(location)
                        continue
                    break
                resp.raise_for_status()
                file_bytes = resp.content
        except ValueError as exc:
            err = ErrorResponse(
                category=ErrorCategory.PERMANENT,
                code=ERR_LEARN_004,
                message=f"Blocked redirect URL: {exc}",
                remediation="Ensure redirect targets are publicly accessible URLs.",
            )
            raise HTTPException(
                status_code=http_status_for(err), detail=err.to_envelope()
            )
        except httpx.HTTPError as exc:
            err = ErrorResponse(
                category=ErrorCategory.TRANSIENT,
                code=ERR_LEARN_001,
                message=f"Failed to fetch document from URL: {exc}",
                remediation="Verify the URL is accessible and try again.",
            )
            raise HTTPException(
                status_code=http_status_for(err), detail=err.to_envelope()
            )

        # Extract filename from URL path
        filename = parsed.path.rsplit("/", 1)[-1] or "document"

        # Run ingest pipeline with course metadata attached to chunks
        ingest_fn = registry.get("ingest", "default")
        result = await ingest_fn(
            file_content=file_bytes,
            filename=filename,
            namespace=req.namespace,
            session=session,
            registry=registry,
            extra_metadata=metadata,
        )
        await session.commit()

        return ContentIngestResponse(
            status=result.status,
            namespace=req.namespace,
            course_id=req.course_id,
            metadata=metadata,
            document_id=str(result.document_id) if result.document_id else None,
            chunk_count=result.chunk_count,
        )

    return ContentIngestResponse(
        status="accepted",
        namespace=req.namespace,
        course_id=req.course_id,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Token generation
# ---------------------------------------------------------------------------


@router.post("/tokens", response_model=TokenResponse, status_code=201)
async def generate_token(
    req: TokenRequest,
    _key: ApiKeyInfo = Depends(require_scope("admin")),
    service: LearnService = Depends(_get_service),
    session: AsyncSession = Depends(_get_session),
) -> TokenResponse:
    """Generate a dashboard token (JWT) for the chatbot widget."""
    token_resp = await service.generate_token(session, req)
    await session.commit()
    return token_resp


# ---------------------------------------------------------------------------
# Conversation turns (WI-1, FEAT-004)
# ---------------------------------------------------------------------------


class ConversationTurnItem(BaseModel):
    """Decrypted student-facing conversation turn.

    Intentionally omits admin-only metadata (model, prompt_tokens,
    completion_tokens, response_id). v0.5.0 returns an empty ``sources``
    list; source enrichment from query_traces is deferred.
    """

    turn_number: int
    question: str
    answer: str | None
    created_at: datetime
    sources: list[dict[str, Any]] = []


class ConversationTurnsResponse(BaseModel):
    conversation_id: UUID
    namespace: str
    turns: list[ConversationTurnItem]


def _resolve_namespace_from_token(
    token_payload: dict[str, Any], request: Request
) -> str:
    """Same fallback chain used by /query: namespace claim, else course_id."""
    course_id = token_payload.get("course_id")
    if not course_id:
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code=ERR_LEARN_003,
            message="Dashboard token is missing 'course_id' claim.",
            remediation="Request a new dashboard token with a valid 'course_id'.",
        )
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())
    ns = token_payload.get("namespace") or course_id
    return str(ns)


@router.get(
    "/conversations/{conversation_id}/turns",
    response_model=ConversationTurnsResponse,
)
async def get_conversation_turns(
    conversation_id: UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    token_payload: dict[str, Any] = Depends(_validate_dashboard_token),
) -> ConversationTurnsResponse:
    """Return decrypted turns for a conversation belonging to the token's course.

    Authorization is namespace-scoped: the conversation must match the namespace
    derived from the JWT (``namespace`` claim or ``course_id`` fallback). A
    mismatch returns 403 so the widget can distinguish "wrong course" from
    "deleted conversation" (404) and reset its local state accordingly.
    """
    namespace = _resolve_namespace_from_token(token_payload, request)

    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        err = ErrorResponse(
            category=ErrorCategory.TRANSIENT,
            code=ERR_LEARN_001,
            message="Conversation store not available.",
            remediation="The service may be starting up. Try again shortly.",
        )
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())

    try:
        conv_store = registry.get("conversation_store", "default")
    except ValueError:
        err = ErrorResponse(
            category=ErrorCategory.TRANSIENT,
            code=ERR_LEARN_001,
            message="Conversation store not configured.",
            remediation="The service may be starting up. Try again shortly.",
        )
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())

    # In-memory store (test/dev) does not support decryption — treat as 501-ish.
    if not hasattr(conv_store, "get_metadata") or not hasattr(
        conv_store, "get_turns_detail"
    ):
        err = ErrorResponse(
            category=ErrorCategory.CONFIGURATION,
            code=ERR_LEARN_001,
            message="Conversation store does not support turn retrieval.",
            remediation=(
                "Configure a persistent conversation store "
                "(VEKTRA_CONVERSATION_KEY must be set)."
            ),
        )
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())

    meta = await conv_store.get_metadata(conversation_id)
    if meta is None or meta.get("deleted_at") is not None:
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code=ERR_LEARN_005,
            message=f"Conversation '{conversation_id}' not found.",
            remediation="Start a new conversation from the widget.",
        )
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())

    if meta["namespace_id"] != namespace:
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code=ERR_LEARN_006,
            message="Conversation belongs to a different course.",
            remediation="Open the course this conversation was created in.",
        )
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())

    turns = await conv_store.get_turns_detail(conversation_id)
    if turns is None:
        # Race: metadata said present, now it's gone. Same envelope as 404.
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code=ERR_LEARN_005,
            message=f"Conversation '{conversation_id}' not found.",
            remediation="Start a new conversation from the widget.",
        )
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())

    items = [
        ConversationTurnItem(
            turn_number=t["turn_number"],
            question=t["question"],
            answer=t.get("answer"),
            created_at=t["created_at"],
            sources=[],
        )
        for t in turns
    ]

    # NFR-007: log sensitive content access (decrypted conversation turns).
    # Learn endpoints authenticate via JWT and do not carry a key_id, so we
    # use the sentinel defined for learn-originated rows. Fire the audit
    # unconditionally with a synthesized request_id if the middleware hasn't
    # set one — a missing correlation id must not silently skip the audit
    # row (compliance gap otherwise invisible).
    request_id = _resolve_request_id(request)
    background_tasks.add_task(
        _audit_log_event,
        key_id=_LEARN_SENTINEL_KEY_ID,
        endpoint=f"/api/v1/learn/conversations/{conversation_id}/turns",
        method="GET",
        status_code=200,
        request_id=request_id,
        action="learn_conversation_turns_read",
        log_metadata={
            "namespace": namespace,
            "conversation_id": str(conversation_id),
            "turn_count": len(items),
            "student_id": token_payload.get("sub"),
            "course_id": token_payload.get("course_id"),
        },
    )

    return ConversationTurnsResponse(
        conversation_id=conversation_id,
        namespace=namespace,
        turns=items,
    )


# ---------------------------------------------------------------------------
# Course-scoped query
# ---------------------------------------------------------------------------


@router.post("/query", response_model=None)
async def course_query(
    req: CourseQueryRequest,
    request: Request,
    token_payload: dict[str, Any] = Depends(_validate_dashboard_token),
    service: LearnService = Depends(_get_service),
    session: AsyncSession = Depends(_get_session),
) -> Any:
    """Course-scoped RAG query authenticated via JWT dashboard token.

    Extracts course_id and namespace from the token, scopes the query
    to the course, and delegates to the query pipeline.

    When VEKTRA_LEARN_REQUIRE_ENROLLMENT is false, enrollment lookup is
    skipped and namespace is derived from the JWT (namespace claim or
    course_id fallback). This supports LMS integrations where enrollment
    is managed externally.
    """
    course_id = token_payload.get("course_id")
    student_id = token_payload.get("sub", "")

    if not course_id:
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code=ERR_LEARN_003,
            message="Dashboard token is missing 'course_id' claim.",
            remediation="Request a new dashboard token with a valid 'course_id'.",
        )
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())

    require_enrollment = getattr(request.app.state, "learn_require_enrollment", True)

    if require_enrollment:
        # Look up namespace from enrollment
        enrollments = await service.list_enrollments(
            session, course_id=course_id, student_id=student_id, limit=1
        )
        if not enrollments:
            err = ErrorResponse(
                category=ErrorCategory.PERMANENT,
                code=ERR_LEARN_002,
                message=f"No enrollment found for student '{student_id}' in course '{course_id}'.",
                remediation="Ensure the student is enrolled before querying.",
            )
            raise HTTPException(
                status_code=http_status_for(err), detail=err.to_envelope()
            )
        namespace = enrollments[0].namespace
    else:
        # Trust JWT: use explicit namespace claim, or fall back to course_id
        namespace = token_payload.get("namespace") or course_id

    # Auto-generate conversation_id for multi-turn continuity (BUG-010).
    # The widget sends the first query without a conversation_id; we create
    # one server-side so the pipeline saves the turn and the response carries
    # the ID back to the client for subsequent queries.
    if req.conversation_id is None:
        req = req.model_copy(update={"conversation_id": uuid4()})

    # Ensure conversation row exists for persistent multi-turn (BUG-014).
    # The learn endpoint uses JWT auth (no API key), so key_id is a sentinel.
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        try:
            conv_store = registry.get("conversation_store", "default")
            if (
                hasattr(conv_store, "ensure_conversation")
                and req.conversation_id is not None
            ):
                await conv_store.ensure_conversation(
                    conversation_id=req.conversation_id,
                    namespace_id=namespace,
                    key_id=_LEARN_SENTINEL_KEY_ID,
                )
        except ValueError:
            pass  # conversation store not registered
        except Exception as exc:
            structlog.get_logger(__name__).warning(
                "conversation_create_failed", error=str(exc)
            )

    # Build course-scoped query and delegate to pipeline
    query_req = build_course_query(req, namespace=namespace, course_id=course_id)

    # Resolve grounding mode: namespace config > env var > default (FEAT-020)
    _db_factory_gm = getattr(request.app.state, "db_session_factory", None)
    _default_mode = getattr(request.app.state, "grounding_mode_default", "strict")
    if _db_factory_gm:
        _gm = await resolve_grounding_mode(
            namespace, _db_factory_gm, default_mode=_default_mode
        )
    else:
        _gm = _default_mode
    query_req.grounding_mode = _gm

    # Resolve show_sources: namespace config > env var > default true (FEAT-014)
    _default_show_sources = getattr(
        request.app.state, "learn_show_sources_default", True
    )
    if _db_factory_gm:
        _show_sources = await resolve_show_sources(
            namespace, _db_factory_gm, default_value=_default_show_sources
        )
    else:
        _show_sources = _default_show_sources

    if registry is None:
        err = ErrorResponse(
            category=ErrorCategory.TRANSIENT,
            code=ERR_LEARN_001,
            message="Query pipeline not available.",
            remediation="The service may be starting up. Try again shortly.",
        )
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())

    try:
        pipeline = registry.get("query_pipeline", "default")
    except ValueError:
        err = ErrorResponse(
            category=ErrorCategory.TRANSIENT,
            code=ERR_LEARN_001,
            message="Query pipeline not configured.",
            remediation="The service may be starting up. Try again shortly.",
        )
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())

    _store_traces = getattr(request.app.state, "store_traces_enabled", False) is True
    _analytics_svc = getattr(request.app.state, "analytics_service", None)
    _db_factory = getattr(request.app.state, "db_session_factory", None)

    if req.stream:
        stream_iter = await pipeline.execute_stream(query_req)
        return StreamingResponse(
            _learn_sse_generator(
                stream_iter,
                request,
                str(query_req.conversation_id),
                analytics_service=_analytics_svc,
                db_session_factory=_db_factory,
                namespace=namespace,
                store_traces=_store_traces,
                show_sources=_show_sources,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    response, trace = await pipeline.execute(query_req)

    # Persist trace (best-effort, BUG-013)
    if _store_traces and trace is not None and _analytics_svc and _db_factory:
        try:
            async with _db_factory() as sess:
                await _analytics_svc.store_trace(sess, trace, namespace=namespace)
                await sess.commit()
        except Exception:
            structlog.get_logger(__name__).warning(
                "trace_store_failed",
                response_id=str(trace.response_id) if trace is not None else None,
                exc_info=True,
            )

    return pipeline_response_to_course_response(response, show_sources=_show_sources)
