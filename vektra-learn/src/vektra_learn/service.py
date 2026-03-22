"""LearnService: enrollment management, content ingestion trigger, and token generation.

Provides the core business logic for the e-learning vertical. All methods
take an explicit AsyncSession parameter (one session per request). The service
instance is stateless and safe to store as a singleton in app.state.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import jwt
import structlog
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vektra_learn.models import DashboardTokenOrm, EnrollmentOrm, NamespaceOrm

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class EnrollmentRequest(BaseModel):
    student_id: str = Field(min_length=1, max_length=255)
    course_id: str = Field(min_length=1, max_length=255)
    namespace: str = Field(min_length=1, max_length=64)
    metadata: dict[str, Any] = {}


class EnrollmentResponse(BaseModel):
    id: UUID
    student_id: str
    course_id: str
    namespace: str
    enrolled_at: datetime
    metadata: dict[str, Any]


class TokenRequest(BaseModel):
    student_id: str = Field(min_length=1, max_length=255)
    course_id: str = Field(min_length=1, max_length=255)
    namespace: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Override namespace in JWT. When omitted, course_id is used as namespace convention.",
    )
    expires_in: int = Field(default=3600, gt=0, le=86400)  # 1 hour default, max 24h


class TokenResponse(BaseModel):
    token: str
    expires_at: datetime


class ContentIngestRequest(BaseModel):
    course_id: str = Field(min_length=1, max_length=255)
    namespace: str = Field(min_length=1, max_length=64)
    document_url: str | None = None
    metadata: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class LearnService:
    """E-learning service: enrollment CRUD, token management, content trigger.

    The jwt_secret is required for token generation/validation. It is injected
    at construction time from VEKTRA_LEARN_JWT_SECRET config.
    """

    def __init__(self, jwt_secret: str) -> None:
        self._jwt_secret = jwt_secret

    # -- Enrollment CRUD ---------------------------------------------------

    async def create_enrollment(
        self, session: AsyncSession, req: EnrollmentRequest
    ) -> EnrollmentResponse:
        """Create a new student-course enrollment."""
        # Auto-create namespace if it doesn't exist
        stmt = (
            pg_insert(NamespaceOrm)
            .values(id=req.namespace, display_name=req.namespace)
            .on_conflict_do_nothing()
        )
        await session.execute(stmt)

        now = datetime.now(UTC)
        enrollment_id = uuid4()
        orm = EnrollmentOrm(
            id=enrollment_id,
            student_id=req.student_id,
            course_id=req.course_id,
            namespace=req.namespace,
            enrolled_at=now,
            metadata_=req.metadata,
        )
        session.add(orm)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            raise
        log.info(
            "enrollment_created",
            student_id=req.student_id,
            course_id=req.course_id,
            namespace=req.namespace,
        )
        return _orm_to_enrollment(orm)

    async def list_enrollments(
        self,
        session: AsyncSession,
        course_id: str | None = None,
        student_id: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EnrollmentResponse]:
        """List enrollments with optional filters."""
        stmt = select(EnrollmentOrm).order_by(EnrollmentOrm.enrolled_at.desc())
        if course_id is not None:
            stmt = stmt.where(EnrollmentOrm.course_id == course_id)
        if student_id is not None:
            stmt = stmt.where(EnrollmentOrm.student_id == student_id)
        if namespace is not None:
            stmt = stmt.where(EnrollmentOrm.namespace == namespace)
        stmt = stmt.limit(limit).offset(offset)
        result = await session.execute(stmt)
        return [_orm_to_enrollment(row) for row in result.scalars().all()]

    async def delete_enrollment(
        self, session: AsyncSession, enrollment_id: UUID
    ) -> bool:
        """Delete an enrollment by ID. Returns True if deleted, False if not found."""
        stmt = delete(EnrollmentOrm).where(EnrollmentOrm.id == enrollment_id)
        result = await session.execute(stmt)
        deleted: bool = (result.rowcount or 0) > 0  # type: ignore[attr-defined]
        if deleted:
            log.info("enrollment_deleted", enrollment_id=str(enrollment_id))
        return deleted

    # -- Token management --------------------------------------------------

    async def generate_token(
        self, session: AsyncSession, req: TokenRequest
    ) -> TokenResponse:
        """Generate a short-lived JWT for the chatbot widget."""
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=req.expires_in)

        payload: dict[str, Any] = {
            "sub": req.student_id,
            "course_id": req.course_id,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        if req.namespace is not None:
            payload["namespace"] = req.namespace
        token = jwt.encode(payload, self._jwt_secret, algorithm="HS256")

        # Store token hash for audit trail only; revocation checks the
        # JWT expiry claim (validate_token). The hash enables future
        # revocation-list lookup without storing the raw token.
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        orm = DashboardTokenOrm(
            student_id=req.student_id,
            course_id=req.course_id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        session.add(orm)
        await session.flush()
        log.info(
            "dashboard_token_generated",
            student_id=req.student_id,
            course_id=req.course_id,
            namespace=req.namespace or req.course_id,
        )
        return TokenResponse(token=token, expires_at=expires_at)

    async def validate_token(self, token: str) -> dict[str, Any]:
        """Validate and decode a JWT. Raises jwt.InvalidTokenError on failure."""
        return await asyncio.to_thread(
            jwt.decode, token, self._jwt_secret, algorithms=["HS256"]
        )

    # -- Content ingestion trigger -----------------------------------------

    def build_ingest_metadata(self, req: ContentIngestRequest) -> dict[str, Any]:
        """Build chunk metadata for course-scoped ingestion.

        Merges course_id into the metadata dict so that course-scoped queries
        can filter via ARCH-044 JSONB filtering. The ingestion endpoint passes
        this metadata via extra_metadata to the ingest pipeline.
        """
        metadata = dict(req.metadata)
        metadata["course_id"] = req.course_id
        return metadata


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _orm_to_enrollment(orm: EnrollmentOrm) -> EnrollmentResponse:
    return EnrollmentResponse(
        id=orm.id,
        student_id=orm.student_id,
        course_id=orm.course_id,
        namespace=orm.namespace,
        enrolled_at=orm.enrolled_at,
        metadata=orm.metadata_,
    )
