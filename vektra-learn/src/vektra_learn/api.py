"""FastAPI router for e-learning vertical endpoints.

Management endpoints (enrollments, content, tokens) require admin or ingest scope.
The query endpoint authenticates via JWT dashboard token (not API key).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from uuid import UUID

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


class EnrollmentListResponse(BaseModel):
    items: list[EnrollmentResponse]
    count: int


class ContentIngestResponse(BaseModel):
    status: str
    namespace: str
    course_id: str
    metadata: dict


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _get_service(request: Request) -> LearnService:
    """Retrieve LearnService from app state."""
    svc = getattr(request.app.state, "learn_service", None)
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
) -> dict:
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
        payload = service.validate_token(credentials.credentials)
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
    except IntegrityError:
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code=ERR_LEARN_004,
            message=f"Enrollment already exists for student '{req.student_id}' in course '{req.course_id}'.",
            remediation="Use GET /api/v1/learn/enrollments to check existing enrollments.",
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
    _key: ApiKeyInfo = Depends(require_scope("ingest")),
    service: LearnService = Depends(_get_service),
) -> ContentIngestResponse:
    """Trigger content ingestion with course metadata.

    Builds metadata with course_id for course-scoped filtering.
    The actual ingestion is wired in infra-phase2 via ProviderRegistry.
    """
    metadata = service.build_ingest_metadata(req)
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
    token_payload: dict = Depends(_validate_dashboard_token),
) -> CourseQueryResponse:
    """Course-scoped RAG query authenticated via JWT dashboard token.

    Extracts course_id and namespace from the token, scopes the query
    to the course, and delegates to the query pipeline.
    """
    course_id = token_payload.get("course_id", "")
    student_id = token_payload.get("sub", "")

    # Look up namespace from enrollment
    service = _get_service(request)
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        err = ErrorResponse(
            category=ErrorCategory.TRANSIENT,
            code=ERR_LEARN_001,
            message="Database session not configured.",
            remediation="The service may be starting up. Try again shortly.",
        )
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())

    async with factory() as session:
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
