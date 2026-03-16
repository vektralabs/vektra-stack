"""Service-layer smoke tests for learn endpoints: enrollment, tokens, JWT, content, error codes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import jwt as pyjwt
import pytest
from fastapi import HTTPException

from vektra_learn.service import (
    ContentIngestRequest,
    EnrollmentRequest,
    EnrollmentResponse,
    LearnService,
    TokenRequest,
)

JWT_SECRET = "test-secret-key-for-api-tests!!x"  # 33 bytes for HS256


# ---------------------------------------------------------------------------
# Enrollment endpoints
# ---------------------------------------------------------------------------


class TestEnrollmentEndpoints:
    async def test_create_enrollment_service_call(self):
        """Test enrollment creation through the service directly."""
        session = MagicMock()
        session.execute = AsyncMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        svc = LearnService(jwt_secret=JWT_SECRET)

        req = EnrollmentRequest(student_id="s1", course_id="CS101", namespace="default")
        result = await svc.create_enrollment(session, req)

        assert result.student_id == "s1"
        assert result.course_id == "CS101"
        assert result.namespace == "default"
        session.add.assert_called_once()


# ---------------------------------------------------------------------------
# Token generation
# ---------------------------------------------------------------------------


class TestTokenEndpoint:
    async def test_generate_token_returns_jwt(self):
        service = LearnService(jwt_secret=JWT_SECRET)
        session = MagicMock()
        session.add = MagicMock()
        session.flush = AsyncMock()

        req = TokenRequest(student_id="s1", course_id="CS101")
        result = await service.generate_token(session, req)

        assert result.token is not None
        payload = pyjwt.decode(result.token, JWT_SECRET, algorithms=["HS256"])
        assert payload["sub"] == "s1"
        assert payload["course_id"] == "CS101"


# ---------------------------------------------------------------------------
# JWT validation dependency
# ---------------------------------------------------------------------------


class TestJWTValidation:
    async def test_valid_token_decoded(self):
        service = LearnService(jwt_secret=JWT_SECRET)
        token = pyjwt.encode(
            {
                "sub": "s1",
                "course_id": "CS101",
                "exp": datetime.now(UTC) + timedelta(hours=1),
            },
            JWT_SECRET,
            algorithm="HS256",
        )
        payload = await service.validate_token(token)
        assert payload["sub"] == "s1"

    async def test_expired_token_rejected(self):
        service = LearnService(jwt_secret=JWT_SECRET)
        token = pyjwt.encode(
            {
                "sub": "s1",
                "course_id": "CS101",
                "exp": datetime.now(UTC) - timedelta(hours=1),
            },
            JWT_SECRET,
            algorithm="HS256",
        )
        with pytest.raises(pyjwt.ExpiredSignatureError):
            await service.validate_token(token)

    async def test_wrong_secret_rejected(self):
        service = LearnService(jwt_secret=JWT_SECRET)
        token = pyjwt.encode(
            {
                "sub": "s1",
                "course_id": "CS101",
                "exp": datetime.now(UTC) + timedelta(hours=1),
            },
            "wrong-secret-that-is-32-bytes!!x",
            algorithm="HS256",
        )
        with pytest.raises(pyjwt.InvalidSignatureError):
            await service.validate_token(token)


# ---------------------------------------------------------------------------
# Content ingest trigger
# ---------------------------------------------------------------------------


class TestContentIngest:
    def test_build_metadata_includes_course_id(self):
        service = LearnService(jwt_secret=JWT_SECRET)

        req = ContentIngestRequest(course_id="CS101", namespace="ns")
        metadata = service.build_ingest_metadata(req)
        assert metadata["course_id"] == "CS101"


# ---------------------------------------------------------------------------
# Error responses
# ---------------------------------------------------------------------------


class TestErrorCodes:
    def test_learn_error_codes_registered(self):
        from vektra_shared.errors import (
            ERR_LEARN_001,
            ERR_LEARN_002,
            ERR_LEARN_003,
            ERR_LEARN_004,
        )

        assert ERR_LEARN_001 == "ERR-LEARN-001"
        assert ERR_LEARN_002 == "ERR-LEARN-002"
        assert ERR_LEARN_003 == "ERR-LEARN-003"
        assert ERR_LEARN_004 == "ERR-LEARN-004"


# ---------------------------------------------------------------------------
# Query endpoint: enrollment required vs optional
# ---------------------------------------------------------------------------


class TestCourseQueryEnrollmentMode:
    """Test namespace resolution with enrollment required (default) vs optional."""

    async def test_query_with_enrollment_required_and_enrolled(self):
        """When enrollment is required and exists, namespace comes from enrollment."""
        from vektra_learn.api import course_query
        from vektra_learn.query import CourseQueryRequest

        req = CourseQueryRequest(question="What is ML?")

        # Mock request with app.state
        mock_app = MagicMock()
        mock_app.state.learn_require_enrollment = True
        mock_pipeline = AsyncMock()
        mock_response = MagicMock()
        mock_response.response_id = uuid4()
        mock_response.answer = "ML is..."
        mock_response.sources = []
        mock_response.conversation_id = None
        mock_response.no_relevant_context = False
        mock_pipeline.execute = AsyncMock(return_value=(mock_response, None))
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_pipeline
        mock_app.state.registry = mock_registry
        mock_request = MagicMock()
        mock_request.app = mock_app

        # Mock service
        enrollment = EnrollmentResponse(
            id=uuid4(),
            student_id="s1",
            course_id="CS101",
            namespace="custom-ns",
            enrolled_at=datetime.now(UTC),
            metadata={},
        )
        service = MagicMock()
        service.list_enrollments = AsyncMock(return_value=[enrollment])

        session = AsyncMock()
        token_payload = {"sub": "s1", "course_id": "CS101"}

        await course_query(req, mock_request, token_payload, service, session)

        # Verify enrollment was queried
        service.list_enrollments.assert_awaited_once()
        # Verify pipeline used namespace from enrollment
        call_args = mock_pipeline.execute.call_args[0][0]
        assert call_args.namespace == "custom-ns"

    async def test_query_with_enrollment_required_and_not_enrolled(self):
        """When enrollment is required but missing, returns 404."""
        from vektra_learn.api import course_query
        from vektra_learn.query import CourseQueryRequest

        req = CourseQueryRequest(question="What is ML?")

        mock_app = MagicMock()
        mock_app.state.learn_require_enrollment = True
        mock_request = MagicMock()
        mock_request.app = mock_app

        service = MagicMock()
        service.list_enrollments = AsyncMock(return_value=[])

        session = AsyncMock()
        token_payload = {"sub": "s1", "course_id": "CS101"}

        with pytest.raises(HTTPException) as exc_info:
            await course_query(req, mock_request, token_payload, service, session)
        assert exc_info.value.status_code == 404

    async def test_query_without_enrollment_uses_course_id_as_namespace(self):
        """When enrollment is not required and JWT has no namespace, use course_id."""
        from vektra_learn.api import course_query
        from vektra_learn.query import CourseQueryRequest

        req = CourseQueryRequest(question="What is ML?")

        mock_app = MagicMock()
        mock_app.state.learn_require_enrollment = False
        mock_pipeline = AsyncMock()
        mock_response = MagicMock()
        mock_response.response_id = uuid4()
        mock_response.answer = "ML is..."
        mock_response.sources = []
        mock_response.conversation_id = None
        mock_response.no_relevant_context = False
        mock_pipeline.execute = AsyncMock(return_value=(mock_response, None))
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_pipeline
        mock_app.state.registry = mock_registry
        mock_request = MagicMock()
        mock_request.app = mock_app

        service = MagicMock()
        session = AsyncMock()
        token_payload = {"sub": "s1", "course_id": "CS101"}

        await course_query(req, mock_request, token_payload, service, session)

        # Enrollment should NOT be queried
        service.list_enrollments.assert_not_called()
        # Pipeline should use course_id as namespace
        call_args = mock_pipeline.execute.call_args[0][0]
        assert call_args.namespace == "CS101"

    async def test_query_without_enrollment_uses_jwt_namespace(self):
        """When enrollment is not required and JWT has namespace, use it."""
        from vektra_learn.api import course_query
        from vektra_learn.query import CourseQueryRequest

        req = CourseQueryRequest(question="What is ML?")

        mock_app = MagicMock()
        mock_app.state.learn_require_enrollment = False
        mock_pipeline = AsyncMock()
        mock_response = MagicMock()
        mock_response.response_id = uuid4()
        mock_response.answer = "ML is..."
        mock_response.sources = []
        mock_response.conversation_id = None
        mock_response.no_relevant_context = False
        mock_pipeline.execute = AsyncMock(return_value=(mock_response, None))
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_pipeline
        mock_app.state.registry = mock_registry
        mock_request = MagicMock()
        mock_request.app = mock_app

        service = MagicMock()
        session = AsyncMock()
        token_payload = {"sub": "s1", "course_id": "CS101", "namespace": "shared-materials"}

        await course_query(req, mock_request, token_payload, service, session)

        # Pipeline should use JWT namespace, not course_id
        call_args = mock_pipeline.execute.call_args[0][0]
        assert call_args.namespace == "shared-materials"


# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------


class TestErrorStatusCodes:
    def test_learn_error_status_codes(self):
        from vektra_shared.errors import (
            ERR_LEARN_002,
            ERR_LEARN_003,
            ERR_LEARN_004,
            ErrorCategory,
            ErrorResponse,
            http_status_for,
        )

        err_not_found = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code=ERR_LEARN_002,
            message="Not found",
            remediation="Check ID",
        )
        assert http_status_for(err_not_found) == 404

        err_token = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code=ERR_LEARN_003,
            message="Bad token",
            remediation="Get new token",
        )
        assert http_status_for(err_token) == 401

        err_dup = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code=ERR_LEARN_004,
            message="Duplicate",
            remediation="Check existing",
        )
        assert http_status_for(err_dup) == 409
