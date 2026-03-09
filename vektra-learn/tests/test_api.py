"""Unit tests for the learn API router: auth enforcement, request/response format, JWT validation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import jwt as pyjwt
import pytest

from vektra_learn.service import LearnService

JWT_SECRET = "test-secret-key-for-api-tests!!x"  # 33 bytes for HS256


# ---------------------------------------------------------------------------
# Enrollment endpoints
# ---------------------------------------------------------------------------


class TestEnrollmentEndpoints:
    async def test_create_enrollment_service_call(self):
        """Test enrollment creation through the service directly."""
        session = MagicMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        svc = LearnService(jwt_secret=JWT_SECRET)

        from vektra_learn.service import EnrollmentRequest

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

        from vektra_learn.service import TokenRequest

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
        from vektra_learn.service import ContentIngestRequest

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
