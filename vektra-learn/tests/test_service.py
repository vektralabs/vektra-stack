"""Unit tests for LearnService: enrollment CRUD, token generation/validation, content trigger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import jwt as pyjwt
import pytest
from sqlalchemy.exc import IntegrityError

from vektra_learn.service import (
    ContentIngestRequest,
    EnrollmentRequest,
    LearnService,
    TokenRequest,
)

JWT_SECRET = "test-secret-key-for-unit-tests!!x"  # 33 bytes for HS256


def _make_service() -> LearnService:
    return LearnService(jwt_secret=JWT_SECRET)


def _make_enrollment_orm(
    student_id: str = "student-1",
    course_id: str = "CS101",
    namespace: str = "default",
) -> MagicMock:
    orm = MagicMock()
    orm.id = uuid4()
    orm.student_id = student_id
    orm.course_id = course_id
    orm.namespace = namespace
    orm.enrolled_at = datetime.now(UTC)
    orm.metadata_ = {}
    return orm


# ---------------------------------------------------------------------------
# Enrollment CRUD
# ---------------------------------------------------------------------------


class TestCreateEnrollment:
    async def test_creates_enrollment(self):
        session = MagicMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        svc = _make_service()

        req = EnrollmentRequest(
            student_id="student-1",
            course_id="CS101",
            namespace="default",
        )
        result = await svc.create_enrollment(session, req)

        session.add.assert_called_once()
        session.flush.assert_awaited_once()
        assert result.student_id == "student-1"
        assert result.course_id == "CS101"
        assert result.namespace == "default"

    async def test_creates_enrollment_with_metadata(self):
        session = MagicMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        svc = _make_service()

        req = EnrollmentRequest(
            student_id="student-2",
            course_id="CS102",
            namespace="ns-2",
            metadata={"section": "A"},
        )
        result = await svc.create_enrollment(session, req)

        orm = session.add.call_args[0][0]
        assert orm.metadata_ == {"section": "A"}
        assert result.student_id == "student-2"

    async def test_duplicate_enrollment_raises_integrity_error(self):
        session = MagicMock()
        session.add = MagicMock()
        session.flush = AsyncMock(side_effect=IntegrityError("", None, None))
        session.rollback = AsyncMock()
        svc = _make_service()

        req = EnrollmentRequest(
            student_id="student-1",
            course_id="CS101",
            namespace="default",
        )
        with pytest.raises(IntegrityError):
            await svc.create_enrollment(session, req)
        session.rollback.assert_awaited_once()


class TestListEnrollments:
    async def test_list_all(self):
        orm1 = _make_enrollment_orm(student_id="s1")
        orm2 = _make_enrollment_orm(student_id="s2")

        session = AsyncMock()
        result_mock = MagicMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [orm1, orm2]
        result_mock.scalars.return_value = scalars_mock
        session.execute = AsyncMock(return_value=result_mock)

        svc = _make_service()
        results = await svc.list_enrollments(session)

        assert len(results) == 2
        assert results[0].student_id == "s1"
        assert results[1].student_id == "s2"

    async def test_list_empty(self):
        session = AsyncMock()
        result_mock = MagicMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = []
        result_mock.scalars.return_value = scalars_mock
        session.execute = AsyncMock(return_value=result_mock)

        svc = _make_service()
        results = await svc.list_enrollments(session)
        assert results == []


class TestDeleteEnrollment:
    async def test_delete_existing(self):
        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.rowcount = 1
        session.execute = AsyncMock(return_value=result_mock)

        svc = _make_service()
        deleted = await svc.delete_enrollment(session, uuid4())
        assert deleted is True

    async def test_delete_not_found(self):
        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.rowcount = 0
        session.execute = AsyncMock(return_value=result_mock)

        svc = _make_service()
        deleted = await svc.delete_enrollment(session, uuid4())
        assert deleted is False


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------


class TestGenerateToken:
    async def test_generates_valid_jwt(self):
        session = MagicMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        svc = _make_service()

        req = TokenRequest(student_id="student-1", course_id="CS101", expires_in=3600)
        result = await svc.generate_token(session, req)

        assert result.token is not None
        assert result.expires_at > datetime.now(UTC)

        # Decode and verify
        payload = pyjwt.decode(result.token, JWT_SECRET, algorithms=["HS256"])
        assert payload["sub"] == "student-1"
        assert payload["course_id"] == "CS101"
        assert "exp" in payload
        assert "iat" in payload

    async def test_stores_token_hash(self):
        session = MagicMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        svc = _make_service()

        req = TokenRequest(student_id="s1", course_id="CS101")
        await svc.generate_token(session, req)

        session.add.assert_called_once()
        orm = session.add.call_args[0][0]
        assert orm.student_id == "s1"
        assert orm.course_id == "CS101"
        assert len(orm.token_hash) == 64  # SHA-256 hex digest

    async def test_custom_expiration(self):
        session = MagicMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        svc = _make_service()

        req = TokenRequest(student_id="s1", course_id="CS101", expires_in=60)
        result = await svc.generate_token(session, req)

        # Should expire within ~60 seconds
        delta = result.expires_at - datetime.now(UTC)
        assert delta.total_seconds() < 65
        assert delta.total_seconds() > 55


class TestValidateToken:
    def test_validates_valid_token(self):
        svc = _make_service()
        token = pyjwt.encode(
            {"sub": "s1", "course_id": "CS101", "exp": datetime.now(UTC) + timedelta(hours=1)},
            JWT_SECRET,
            algorithm="HS256",
        )
        payload = svc.validate_token(token)
        assert payload["sub"] == "s1"
        assert payload["course_id"] == "CS101"

    def test_rejects_expired_token(self):
        svc = _make_service()
        token = pyjwt.encode(
            {"sub": "s1", "course_id": "CS101", "exp": datetime.now(UTC) - timedelta(hours=1)},
            JWT_SECRET,
            algorithm="HS256",
        )
        with pytest.raises(pyjwt.ExpiredSignatureError):
            svc.validate_token(token)

    def test_rejects_wrong_secret(self):
        svc = _make_service()
        token = pyjwt.encode(
            {"sub": "s1", "course_id": "CS101", "exp": datetime.now(UTC) + timedelta(hours=1)},
            "wrong-secret-that-is-32-bytes!!x",
            algorithm="HS256",
        )
        with pytest.raises(pyjwt.InvalidSignatureError):
            svc.validate_token(token)


# ---------------------------------------------------------------------------
# Content ingestion trigger
# ---------------------------------------------------------------------------


class TestBuildIngestMetadata:
    def test_adds_course_id(self):
        svc = _make_service()
        req = ContentIngestRequest(
            course_id="CS101",
            namespace="default",
        )
        metadata = svc.build_ingest_metadata(req)
        assert metadata["course_id"] == "CS101"

    def test_merges_existing_metadata(self):
        svc = _make_service()
        req = ContentIngestRequest(
            course_id="CS101",
            namespace="default",
            metadata={"module_id": "M1", "academic_year": "2025-2026"},
        )
        metadata = svc.build_ingest_metadata(req)
        assert metadata["course_id"] == "CS101"
        assert metadata["module_id"] == "M1"
        assert metadata["academic_year"] == "2025-2026"

    def test_does_not_mutate_original(self):
        svc = _make_service()
        original = {"module_id": "M1"}
        req = ContentIngestRequest(
            course_id="CS101",
            namespace="default",
            metadata=original,
        )
        metadata = svc.build_ingest_metadata(req)
        assert "course_id" not in original
        assert metadata["course_id"] == "CS101"
