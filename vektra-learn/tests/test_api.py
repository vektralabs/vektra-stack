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

    async def test_query_rejects_token_without_course_id(self):
        """When course_id is missing from JWT, returns 401."""
        from vektra_learn.api import course_query
        from vektra_learn.query import CourseQueryRequest

        req = CourseQueryRequest(question="What is ML?")

        mock_app = MagicMock()
        mock_app.state.learn_require_enrollment = False
        mock_request = MagicMock()
        mock_request.app = mock_app

        service = MagicMock()
        session = AsyncMock()
        token_payload = {"sub": "s1"}  # no course_id

        with pytest.raises(HTTPException) as exc_info:
            await course_query(req, mock_request, token_payload, service, session)
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error"]["code"] == "ERR-LEARN-003"

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
        assert exc_info.value.detail["error"]["code"] == "ERR-LEARN-002"

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
        token_payload = {
            "sub": "s1",
            "course_id": "CS101",
            "namespace": "shared-materials",
        }

        await course_query(req, mock_request, token_payload, service, session)

        # Pipeline should use JWT namespace, not course_id
        call_args = mock_pipeline.execute.call_args[0][0]
        assert call_args.namespace == "shared-materials"


# ---------------------------------------------------------------------------
# Conversation auto-creation (BUG-010)
# ---------------------------------------------------------------------------


class TestConversationAutoCreation:
    """Test that course_query auto-generates conversation_id when omitted."""

    async def test_auto_generates_conversation_id_when_omitted(self):
        """First query without conversation_id gets one auto-generated."""
        from vektra_learn.api import course_query
        from vektra_learn.query import CourseQueryRequest

        req = CourseQueryRequest(question="What is ML?")
        assert req.conversation_id is None

        mock_app = MagicMock()
        mock_app.state.learn_require_enrollment = False
        mock_pipeline = AsyncMock()
        mock_response = MagicMock()
        mock_response.response_id = uuid4()
        mock_response.answer = "ML is..."
        mock_response.sources = []
        mock_response.conversation_id = uuid4()
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

        result = await course_query(req, mock_request, token_payload, service, session)

        # Pipeline should have received a non-None conversation_id
        call_args = mock_pipeline.execute.call_args[0][0]
        assert call_args.conversation_id is not None
        # Return value should carry the conversation_id back to the client
        assert result.conversation_id == mock_response.conversation_id

    async def test_preserves_client_provided_conversation_id(self):
        """When client provides conversation_id, use it as-is."""
        from vektra_learn.api import course_query
        from vektra_learn.query import CourseQueryRequest

        existing_id = uuid4()
        req = CourseQueryRequest(question="Follow up", conversation_id=existing_id)

        mock_app = MagicMock()
        mock_app.state.learn_require_enrollment = False
        mock_pipeline = AsyncMock()
        mock_response = MagicMock()
        mock_response.response_id = uuid4()
        mock_response.answer = "Sure..."
        mock_response.sources = []
        mock_response.conversation_id = existing_id
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

        result = await course_query(req, mock_request, token_payload, service, session)

        # Pipeline should have received the exact conversation_id provided
        call_args = mock_pipeline.execute.call_args[0][0]
        assert call_args.conversation_id == existing_id
        # Return value should carry the same conversation_id
        assert result.conversation_id == existing_id


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


# ---------------------------------------------------------------------------
# Conversation turns endpoint (WI-1 / FEAT-004)
# ---------------------------------------------------------------------------


class TestConversationTurnsEndpoint:
    """JWT-scoped GET /api/v1/learn/conversations/{id}/turns."""

    @staticmethod
    def _make_request(conv_store):
        app = MagicMock()
        registry = MagicMock()
        registry.get = MagicMock(return_value=conv_store)
        app.state.registry = registry
        request = MagicMock()
        request.app = app
        return request

    async def test_returns_decrypted_turns_on_namespace_match(self):
        from vektra_learn.api import get_conversation_turns

        cid = uuid4()
        conv_store = MagicMock()
        conv_store.get_metadata = AsyncMock(
            return_value={
                "id": cid,
                "namespace_id": "CS101",
                "deleted_at": None,
                "turn_count": 2,
            }
        )
        conv_store.get_turns_detail = AsyncMock(
            return_value=[
                {
                    "turn_number": 1,
                    "question": "What is RAG?",
                    "answer": "Retrieval-augmented generation.",
                    "response_id": uuid4(),
                    "model": "gpt-4o",
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "created_at": datetime.now(UTC),
                },
                {
                    "turn_number": 2,
                    "question": "Follow up?",
                    "answer": "Sure.",
                    "response_id": None,
                    "model": None,
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "created_at": datetime.now(UTC),
                },
            ]
        )

        request = self._make_request(conv_store)
        token_payload = {"sub": "s1", "course_id": "CS101"}

        resp = await get_conversation_turns(cid, request, token_payload)
        assert resp.conversation_id == cid
        assert resp.namespace == "CS101"
        assert len(resp.turns) == 2
        assert resp.turns[0].question == "What is RAG?"
        assert resp.turns[0].answer == "Retrieval-augmented generation."
        assert resp.turns[0].sources == []
        # Admin-only metadata must not be exposed
        assert not hasattr(resp.turns[0], "model")
        assert not hasattr(resp.turns[0], "response_id")

    async def test_403_on_namespace_mismatch(self):
        """Conversation exists but belongs to a different course."""
        from vektra_learn.api import get_conversation_turns

        cid = uuid4()
        conv_store = MagicMock()
        conv_store.get_metadata = AsyncMock(
            return_value={
                "id": cid,
                "namespace_id": "OTHER-COURSE",
                "deleted_at": None,
                "turn_count": 1,
            }
        )
        conv_store.get_turns_detail = AsyncMock()

        request = self._make_request(conv_store)
        token_payload = {"sub": "s1", "course_id": "CS101"}

        with pytest.raises(HTTPException) as exc_info:
            await get_conversation_turns(cid, request, token_payload)
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["error"]["code"] == "ERR-LEARN-006"
        # Must not leak the actual content of the other-namespace conversation
        conv_store.get_turns_detail.assert_not_awaited()

    async def test_404_on_missing_conversation(self):
        from vektra_learn.api import get_conversation_turns

        conv_store = MagicMock()
        conv_store.get_metadata = AsyncMock(return_value=None)
        conv_store.get_turns_detail = AsyncMock()

        request = self._make_request(conv_store)
        token_payload = {"sub": "s1", "course_id": "CS101"}

        with pytest.raises(HTTPException) as exc_info:
            await get_conversation_turns(uuid4(), request, token_payload)
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["error"]["code"] == "ERR-LEARN-005"
        conv_store.get_turns_detail.assert_not_awaited()

    async def test_404_on_soft_deleted_conversation(self):
        """Soft-deleted conversations behave as if they don't exist."""
        from vektra_learn.api import get_conversation_turns

        cid = uuid4()
        conv_store = MagicMock()
        conv_store.get_metadata = AsyncMock(
            return_value={
                "id": cid,
                "namespace_id": "CS101",
                "deleted_at": datetime.now(UTC),  # soft-deleted
                "turn_count": 0,
            }
        )
        conv_store.get_turns_detail = AsyncMock()

        request = self._make_request(conv_store)
        token_payload = {"sub": "s1", "course_id": "CS101"}

        with pytest.raises(HTTPException) as exc_info:
            await get_conversation_turns(cid, request, token_payload)
        assert exc_info.value.status_code == 404

    async def test_rejects_token_without_course_id(self):
        from vektra_learn.api import get_conversation_turns

        conv_store = MagicMock()
        request = self._make_request(conv_store)
        token_payload = {"sub": "s1"}  # no course_id

        with pytest.raises(HTTPException) as exc_info:
            await get_conversation_turns(uuid4(), request, token_payload)
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error"]["code"] == "ERR-LEARN-003"

    async def test_respects_namespace_claim_over_course_id(self):
        """Namespace claim in JWT takes precedence over course_id."""
        from vektra_learn.api import get_conversation_turns

        cid = uuid4()
        conv_store = MagicMock()
        conv_store.get_metadata = AsyncMock(
            return_value={
                "id": cid,
                "namespace_id": "shared-materials",
                "deleted_at": None,
                "turn_count": 0,
            }
        )
        conv_store.get_turns_detail = AsyncMock(return_value=[])

        request = self._make_request(conv_store)
        token_payload = {
            "sub": "s1",
            "course_id": "CS101",
            "namespace": "shared-materials",
        }

        resp = await get_conversation_turns(cid, request, token_payload)
        assert resp.namespace == "shared-materials"

    async def test_501ish_when_store_lacks_decryption(self):
        """In-memory store (no get_metadata / get_turns_detail) returns 503."""
        from vektra_learn.api import get_conversation_turns

        # bare object with none of the required methods
        class _BareStore:
            pass

        conv_store = _BareStore()
        request = self._make_request(conv_store)
        token_payload = {"sub": "s1", "course_id": "CS101"}

        with pytest.raises(HTTPException) as exc_info:
            await get_conversation_turns(uuid4(), request, token_payload)
        # ERR-LEARN-001 is CONFIGURATION → 500
        assert exc_info.value.status_code == 500
        assert exc_info.value.detail["error"]["code"] == "ERR-LEARN-001"
