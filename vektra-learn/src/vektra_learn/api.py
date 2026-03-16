"""FastAPI router for e-learning vertical endpoints.

Management endpoints (enrollments, content, tokens) require admin or ingest scope.
The query endpoint authenticates via JWT dashboard token (not API key).
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vektra_learn.query import (
    CourseQueryRequest,
    CourseQueryResponse,
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
from vektra_shared.auth import ApiKeyInfo, require_scope
from vektra_shared.errors import (
    ERR_LEARN_001,
    ERR_LEARN_002,
    ERR_LEARN_003,
    ERR_LEARN_004,
    ErrorCategory,
    ErrorResponse,
    http_status_for,
)

router = APIRouter(prefix="/api/v1/learn", tags=["learn"])

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
# Course-scoped query
# ---------------------------------------------------------------------------


@router.post("/query", response_model=CourseQueryResponse)
async def course_query(
    req: CourseQueryRequest,
    request: Request,
    token_payload: dict[str, Any] = Depends(_validate_dashboard_token),
    service: LearnService = Depends(_get_service),
    session: AsyncSession = Depends(_get_session),
) -> CourseQueryResponse:
    """Course-scoped RAG query authenticated via JWT dashboard token.

    Extracts course_id and namespace from the token, scopes the query
    to the course, and delegates to the query pipeline.

    When VEKTRA_LEARN_REQUIRE_ENROLLMENT is false, enrollment lookup is
    skipped and namespace is derived from the JWT (namespace claim or
    course_id fallback). This supports LMS integrations where enrollment
    is managed externally.
    """
    course_id = token_payload.get("course_id", "")
    student_id = token_payload.get("sub", "")

    require_enrollment = getattr(
        request.app.state, "learn_require_enrollment", True
    )

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
            raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())
        namespace = enrollments[0].namespace
    else:
        # Trust JWT: use explicit namespace claim, or fall back to course_id
        namespace = token_payload.get("namespace") or course_id

    # Build course-scoped query and delegate to pipeline
    query_req = build_course_query(req, namespace=namespace, course_id=course_id)

    # Get pipeline from ProviderRegistry
    registry = getattr(request.app.state, "registry", None)
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

    response, _trace = await pipeline.execute(query_req)
    return pipeline_response_to_course_response(response)
