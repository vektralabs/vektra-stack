"""Service-layer smoke tests for learn endpoints: enrollment, tokens, JWT, content, error codes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import jwt as pyjwt
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from vektra_learn.service import (
    ContentIngestRequest,
    EnrollmentRequest,
    EnrollmentResponse,
    LearnService,
    TokenRequest,
)
from vektra_shared.http_errors import register_error_handlers

JWT_SECRET = "test-secret-key-for-api-tests!!x"  # 33 bytes for HS256


# ---------------------------------------------------------------------------
# Enrollment endpoints
# ---------------------------------------------------------------------------


def _registry_returning(pipeline, conv_store=None):
    """Registry mock that answers per slot, not one object for everything.

    `course_query` reads the conversation store to check ownership before it
    reaches the pipeline (FEAT-027), so a registry that hands back the pipeline
    for every slot makes the endpoint look at a conversation that is not one.
    The default store reports "no such conversation", which is what these tests
    mean when they pass no conversation_id.
    """
    if conv_store is None:
        conv_store = MagicMock()
        conv_store.get_metadata = AsyncMock(return_value=None)
        conv_store.ensure_conversation = AsyncMock()

    def _get(category, name="default"):
        return conv_store if category == "conversation_store" else pipeline

    registry = MagicMock()
    registry.get = MagicMock(side_effect=_get)
    return registry


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
        mock_registry = _registry_returning(mock_pipeline)
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
        mock_registry = _registry_returning(mock_pipeline)
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
        mock_registry = _registry_returning(mock_pipeline)
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
        mock_registry = _registry_returning(mock_pipeline)
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
        mock_registry = _registry_returning(mock_pipeline)
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
                "owner_subject": "s1",
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
        bg = MagicMock()
        token_payload = {"sub": "s1", "course_id": "CS101"}

        resp = await get_conversation_turns(cid, request, bg, token_payload)
        assert resp.conversation_id == cid
        assert resp.namespace == "CS101"
        assert len(resp.turns) == 2
        assert resp.turns[0].question == "What is RAG?"
        assert resp.turns[0].answer == "Retrieval-augmented generation."
        assert resp.turns[0].sources == []
        # Admin-only metadata must not be exposed
        assert not hasattr(resp.turns[0], "model")
        assert not hasattr(resp.turns[0], "response_id")
        # Audit log scheduled for content access (NFR-007)
        bg.add_task.assert_called_once()
        call_kwargs = bg.add_task.call_args.kwargs
        assert call_kwargs["action"] == "learn_conversation_turns_read"
        assert call_kwargs["log_metadata"]["conversation_id"] == str(cid)
        assert call_kwargs["log_metadata"]["namespace"] == "CS101"
        assert call_kwargs["log_metadata"]["turn_count"] == 2
        assert call_kwargs["log_metadata"]["student_id"] == "s1"

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
        bg = MagicMock()
        token_payload = {"sub": "s1", "course_id": "CS101"}

        with pytest.raises(HTTPException) as exc_info:
            await get_conversation_turns(cid, request, bg, token_payload)
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["error"]["code"] == "ERR-LEARN-006"
        # Must not leak the actual content of the other-namespace conversation
        conv_store.get_turns_detail.assert_not_awaited()
        # No audit log on denied access (we only log successful reads)
        bg.add_task.assert_not_called()

    async def test_404_on_missing_conversation(self):
        from vektra_learn.api import get_conversation_turns

        conv_store = MagicMock()
        conv_store.get_metadata = AsyncMock(return_value=None)
        conv_store.get_turns_detail = AsyncMock()

        request = self._make_request(conv_store)
        bg = MagicMock()
        token_payload = {"sub": "s1", "course_id": "CS101"}

        with pytest.raises(HTTPException) as exc_info:
            await get_conversation_turns(uuid4(), request, bg, token_payload)
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
        bg = MagicMock()
        token_payload = {"sub": "s1", "course_id": "CS101"}

        with pytest.raises(HTTPException) as exc_info:
            await get_conversation_turns(cid, request, bg, token_payload)
        assert exc_info.value.status_code == 404

    async def test_rejects_token_without_course_id(self):
        from vektra_learn.api import get_conversation_turns

        conv_store = MagicMock()
        request = self._make_request(conv_store)
        bg = MagicMock()
        token_payload = {"sub": "s1"}  # no course_id

        with pytest.raises(HTTPException) as exc_info:
            await get_conversation_turns(uuid4(), request, bg, token_payload)
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
                "owner_subject": "s1",
                "deleted_at": None,
                "turn_count": 0,
            }
        )
        conv_store.get_turns_detail = AsyncMock(return_value=[])

        request = self._make_request(conv_store)
        bg = MagicMock()
        token_payload = {
            "sub": "s1",
            "course_id": "CS101",
            "namespace": "shared-materials",
        }

        resp = await get_conversation_turns(cid, request, bg, token_payload)
        assert resp.namespace == "shared-materials"

    async def test_500_err_learn_001_when_store_lacks_decryption(self):
        """In-memory store (no get_metadata / get_turns_detail) returns 500.

        ERR-LEARN-001 is CONFIGURATION → 500.
        """
        from vektra_learn.api import get_conversation_turns

        # bare object with none of the required methods
        class _BareStore:
            pass

        conv_store = _BareStore()
        request = self._make_request(conv_store)
        bg = MagicMock()
        token_payload = {"sub": "s1", "course_id": "CS101"}

        with pytest.raises(HTTPException) as exc_info:
            await get_conversation_turns(uuid4(), request, bg, token_payload)
        assert exc_info.value.status_code == 500
        assert exc_info.value.detail["error"]["code"] == "ERR-LEARN-001"

    async def test_audit_fires_even_without_request_id(self):
        """NFR-007: audit must fire unconditionally on successful turns read.

        If the RequestIdMiddleware isn't wired (tests, early boot), a missing
        request.state.request_id must not silently skip the audit row — the
        handler synthesizes a UUID fallback so every authenticated content
        access leaves an audit trail.
        """
        from vektra_learn.api import get_conversation_turns

        cid = uuid4()
        conv_store = MagicMock()
        conv_store.get_metadata = AsyncMock(
            return_value={
                "id": cid,
                "namespace_id": "CS101",
                "owner_subject": "s1",
                "deleted_at": None,
                "turn_count": 0,
            }
        )
        conv_store.get_turns_detail = AsyncMock(return_value=[])

        # Simulate middleware not wired: request.state has no attribute
        request = self._make_request(conv_store)

        class _EmptyState:
            pass

        request.state = _EmptyState()

        bg = MagicMock()
        token_payload = {"sub": "s1", "course_id": "CS101"}

        await get_conversation_turns(cid, request, bg, token_payload)
        bg.add_task.assert_called_once()
        call_kwargs = bg.add_task.call_args.kwargs
        assert call_kwargs["action"] == "learn_conversation_turns_read"
        # Synthesized request_id must still be a UUID (not None)
        from uuid import UUID as _UUID

        assert isinstance(call_kwargs["request_id"], _UUID)


# ---------------------------------------------------------------------------
# FEAT-014: show_sources propagation
# ---------------------------------------------------------------------------


class TestShowSourcesPropagation:
    """show_sources flag resolution and propagation to non-stream + SSE responses."""

    def _make_pipeline_mock(self) -> tuple[MagicMock, MagicMock]:
        mock_response = MagicMock()
        mock_response.response_id = uuid4()
        mock_response.answer = "answer"
        mock_response.sources = []
        mock_response.conversation_id = None
        mock_response.no_relevant_context = False
        mock_pipeline = AsyncMock()
        mock_pipeline.execute = AsyncMock(return_value=(mock_response, None))
        return mock_pipeline, mock_response

    def _make_app(
        self, pipeline: MagicMock, *, show_sources_default: bool
    ) -> MagicMock:
        mock_app = MagicMock()
        mock_app.state.learn_require_enrollment = False
        mock_app.state.learn_show_sources_default = show_sources_default
        # No DB factory: the resolver falls back to the env-default directly.
        mock_app.state.db_session_factory = None
        mock_registry = _registry_returning(pipeline)
        mock_app.state.registry = mock_registry
        return mock_app

    async def test_non_stream_response_carries_env_default_true(self):
        """When namespace has no override, the env-default (true) reaches the client."""
        from vektra_learn.api import course_query
        from vektra_learn.query import CourseQueryRequest

        pipeline, _ = self._make_pipeline_mock()
        mock_app = self._make_app(pipeline, show_sources_default=True)
        mock_request = MagicMock()
        mock_request.app = mock_app
        req = CourseQueryRequest(question="Q")

        result = await course_query(
            req,
            mock_request,
            {"sub": "s1", "course_id": "CS101"},
            MagicMock(),
            AsyncMock(),
        )
        assert result.show_sources is True

    async def test_non_stream_response_carries_env_default_false(self):
        """VEKTRA_LEARN_SHOW_SOURCES=false is surfaced when no namespace override exists."""
        from vektra_learn.api import course_query
        from vektra_learn.query import CourseQueryRequest

        pipeline, _ = self._make_pipeline_mock()
        mock_app = self._make_app(pipeline, show_sources_default=False)
        mock_request = MagicMock()
        mock_request.app = mock_app
        req = CourseQueryRequest(question="Q")

        result = await course_query(
            req,
            mock_request,
            {"sub": "s1", "course_id": "CS101"},
            MagicMock(),
            AsyncMock(),
        )
        assert result.show_sources is False

    async def test_sse_sources_event_carries_show_sources(self):
        """The SSE ``sources`` event payload includes the flag for the widget."""
        import json as _json

        from vektra_learn.api import _learn_sse_generator

        chunk = MagicMock()
        chunk.type = "sources"
        chunk.data = [{"doc_id": "d1", "chunk_id": "c1", "score": 0.9, "snippet": "s"}]

        async def _stream():
            yield chunk

        request = MagicMock()
        request.is_disconnected = AsyncMock(return_value=False)

        events = []
        async for ev in _learn_sse_generator(
            _stream(), request, "conv-1", show_sources=False
        ):
            events.append(ev)

        assert any("sources" in ev for ev in events)
        sources_event = next(ev for ev in events if '"type": "sources"' in ev)
        # Strip SSE framing ("data: ...\n\n") before parsing.
        payload = _json.loads(sources_event.removeprefix("data: ").strip())
        assert payload["type"] == "sources"
        assert payload["show_sources"] is False
        assert payload["data"] == chunk.data

    async def test_sse_non_sources_events_do_not_include_flag(self):
        """Only the ``sources`` event carries the flag; tokens and errors don't."""
        import json as _json

        from vektra_learn.api import _learn_sse_generator

        token_chunk = MagicMock()
        token_chunk.type = "token"
        token_chunk.data = "hello"

        async def _stream():
            yield token_chunk

        request = MagicMock()
        request.is_disconnected = AsyncMock(return_value=False)

        events = []
        async for ev in _learn_sse_generator(
            _stream(), request, "conv-1", show_sources=False
        ):
            events.append(ev)

        token_event = next(ev for ev in events if '"type": "token"' in ev)
        payload = _json.loads(token_event.removeprefix("data: ").strip())
        assert "show_sources" not in payload


# ---------------------------------------------------------------------------
# BUG-026: top_k bounds (ge=1, le=100) on POST /api/v1/learn/query
# ---------------------------------------------------------------------------


class TestCourseQueryTopKBounds:
    """The query body must reject an out-of-bounds top_k with a 422, mirroring
    /api/v1/search and QueryBody, instead of letting it drive the retrieval
    fetch (top_k>100) or answer 200 no_relevant_context (top_k<=0)."""

    @staticmethod
    def _make_app() -> FastAPI:
        from vektra_learn.api import (
            _get_service,
            _get_session,
            _validate_dashboard_token,
            router,
        )

        app = FastAPI()
        app.include_router(router)
        register_error_handlers(app)
        # FastAPI resolves dependencies before validating the request body, so
        # the auth dependency must pass for a Field-bound violation to surface
        # as 422 (not 401). The token intentionally omits course_id: a valid
        # top_k then clears validation and the handler raises ERR-LEARN-003, so
        # one app proves both edges (invalid -> 422, valid -> reaches handler).
        app.dependency_overrides[_validate_dashboard_token] = lambda: {"sub": "s1"}
        app.dependency_overrides[_get_service] = lambda: MagicMock()
        app.dependency_overrides[_get_session] = lambda: AsyncMock()
        app.state.learn_require_enrollment = False
        return app

    @pytest.mark.parametrize("top_k", [0, -1, 101, 100000])
    async def test_out_of_bounds_returns_422(self, top_k: int) -> None:
        app = self._make_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/learn/query",
                json={"question": "What is ML?", "top_k": top_k},
                headers={"Authorization": "Bearer test-token"},
            )
        assert resp.status_code == 422
        # Consistent with /api/v1/search: a Field-bound violation is FastAPI's
        # standard RequestValidationError shape, not the REQ-010 envelope.
        body = resp.json()
        assert isinstance(body["detail"], list)
        assert body["detail"][0]["loc"][-1] == "top_k"

    @pytest.mark.parametrize("top_k", [1, 5, 100])
    async def test_within_bounds_clears_validation(self, top_k: int) -> None:
        """A valid boundary value is not a 422: it clears validation and the
        handler runs (raising ERR-LEARN-003 for the course_id-less token)."""
        app = self._make_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/learn/query",
                json={"question": "What is ML?", "top_k": top_k},
                headers={"Authorization": "Bearer test-token"},
            )
        assert resp.status_code != 422
        assert resp.json()["error"]["code"] == "ERR-LEARN-003"


class TestConversationOwnership:
    """Per-student ownership of conversations (FEAT-027)."""

    @staticmethod
    def _make_request(conv_store):
        app = MagicMock()
        registry = MagicMock()
        registry.get = MagicMock(return_value=conv_store)
        app.state.registry = registry
        request = MagicMock()
        request.app = app
        return request

    @staticmethod
    def _store_with_meta(owner):
        conv_store = MagicMock()
        conv_store.get_metadata = AsyncMock(
            return_value={
                "id": uuid4(),
                "namespace_id": "CS101",
                "owner_subject": owner,
                "deleted_at": None,
                "turn_count": 1,
            }
        )
        conv_store.get_turns_detail = AsyncMock(return_value=[])
        return conv_store

    async def test_another_students_conversation_is_refused(self):
        """The hole this closes: same course, same token shape, other student."""
        from vektra_learn.api import get_conversation_turns

        conv_store = self._store_with_meta("student-b")
        request = self._make_request(conv_store)

        with pytest.raises(HTTPException) as exc:
            await get_conversation_turns(
                uuid4(),
                request,
                MagicMock(),
                {"sub": "student-a", "course_id": "CS101"},
            )

        assert exc.value.status_code == 403
        assert exc.value.detail["error"]["code"] == "ERR-LEARN-007"
        # The refusal happens before any content is decrypted.
        conv_store.get_turns_detail.assert_not_called()

    async def test_ownerless_conversation_is_refused(self):
        """Rows predating ownership are nobody's, not everybody's."""
        from vektra_learn.api import get_conversation_turns

        conv_store = self._store_with_meta(None)
        request = self._make_request(conv_store)

        with pytest.raises(HTTPException) as exc:
            await get_conversation_turns(
                uuid4(),
                request,
                MagicMock(),
                {"sub": "student-a", "course_id": "CS101"},
            )

        assert exc.value.status_code == 403
        assert exc.value.detail["error"]["code"] == "ERR-LEARN-007"

    async def test_own_conversation_is_returned(self):
        from vektra_learn.api import get_conversation_turns

        conv_store = self._store_with_meta("student-a")
        request = self._make_request(conv_store)

        resp = await get_conversation_turns(
            uuid4(), request, MagicMock(), {"sub": "student-a", "course_id": "CS101"}
        )
        assert resp.namespace == "CS101"

    async def test_token_without_subject_is_refused(self):
        """No subject means no ownership; it must not fall back to the course."""
        from vektra_learn.api import get_conversation_turns

        conv_store = self._store_with_meta("student-a")
        request = self._make_request(conv_store)

        with pytest.raises(HTTPException) as exc:
            await get_conversation_turns(
                uuid4(), request, MagicMock(), {"course_id": "CS101"}
            )

        assert exc.value.detail["error"]["code"] == "ERR-LEARN-003"

    async def test_list_returns_only_the_callers_conversations(self):
        from vektra_learn.api import list_my_conversations

        now = datetime.now(UTC)
        conv_store = MagicMock()
        conv_store.get_metadata = AsyncMock()
        conv_store.list_conversations = AsyncMock(
            return_value=[
                {
                    "id": uuid4(),
                    "title": None,
                    "turn_count": 3,
                    "created_at": now,
                    "updated_at": now,
                }
            ]
        )
        request = self._make_request(conv_store)

        resp = await list_my_conversations(
            request, 20, {"sub": "student-a", "course_id": "CS101"}
        )

        assert len(resp.conversations) == 1
        assert resp.namespace == "CS101"
        conv_store.list_conversations.assert_awaited_once_with(
            namespace_id="CS101", owner_subject="student-a", limit=20
        )

    async def test_delete_is_scoped_to_the_caller(self):
        from vektra_learn.api import delete_my_conversation

        cid = uuid4()
        conv_store = MagicMock()
        conv_store.get_metadata = AsyncMock()
        conv_store.soft_delete = AsyncMock(return_value=True)
        request = self._make_request(conv_store)
        bg = MagicMock()

        resp = await delete_my_conversation(
            cid, request, bg, {"sub": "student-a", "course_id": "CS101"}
        )

        assert resp.status_code == 204
        conv_store.soft_delete.assert_awaited_once_with(
            cid, namespace="CS101", owner_subject="student-a"
        )
        assert bg.add_task.call_args.kwargs["action"] == "learn_conversation_deleted"

    async def test_deleting_someone_elses_conversation_is_a_404(self):
        """Not 403: a student must not learn which conversation ids exist."""
        from vektra_learn.api import delete_my_conversation

        conv_store = MagicMock()
        conv_store.get_metadata = AsyncMock()
        conv_store.soft_delete = AsyncMock(return_value=False)
        request = self._make_request(conv_store)

        with pytest.raises(HTTPException) as exc:
            await delete_my_conversation(
                uuid4(),
                request,
                MagicMock(),
                {"sub": "student-a", "course_id": "CS101"},
            )

        assert exc.value.status_code == 404
        assert exc.value.detail["error"]["code"] == "ERR-LEARN-005"


class TestConversationOwnerRecording:
    """The query endpoint is the only place a conversation's owner is set."""

    async def test_query_records_the_token_subject_as_owner(self):
        from vektra_learn.api import course_query
        from vektra_learn.query import CourseQueryRequest

        req = CourseQueryRequest(question="What is ML?")

        conv_store = MagicMock()
        conv_store.ensure_conversation = AsyncMock()

        mock_pipeline = AsyncMock()
        mock_response = MagicMock()
        mock_response.response_id = uuid4()
        mock_response.answer = "ML is..."
        mock_response.sources = []
        mock_response.conversation_id = uuid4()
        mock_response.no_relevant_context = False
        mock_pipeline.execute = AsyncMock(return_value=(mock_response, None))

        def _get(category, name="default"):
            return conv_store if category == "conversation_store" else mock_pipeline

        mock_app = MagicMock()
        mock_app.state.learn_require_enrollment = False
        mock_registry = MagicMock()
        mock_registry.get = MagicMock(side_effect=_get)
        mock_app.state.registry = mock_registry
        mock_request = MagicMock()
        mock_request.app = mock_app

        await course_query(
            req,
            mock_request,
            {"sub": "student-a", "course_id": "CS101"},
            MagicMock(),
            AsyncMock(),
        )

        kwargs = conv_store.ensure_conversation.call_args.kwargs
        assert kwargs["owner_subject"] == "student-a"
        assert kwargs["namespace_id"] == "CS101"


class TestQueryPathOwnership:
    """The query endpoint takes a client-supplied conversation_id (FEAT-027)."""

    @staticmethod
    def _make_request(conv_store, pipeline):
        def _get(category, name="default"):
            return conv_store if category == "conversation_store" else pipeline

        app = MagicMock()
        app.state.learn_require_enrollment = False
        registry = MagicMock()
        registry.get = MagicMock(side_effect=_get)
        app.state.registry = registry
        request = MagicMock()
        request.app = app
        return request

    @staticmethod
    def _pipeline():
        pipeline = AsyncMock()
        resp = MagicMock()
        resp.response_id = uuid4()
        resp.answer = "..."
        resp.sources = []
        resp.conversation_id = uuid4()
        resp.no_relevant_context = False
        pipeline.execute = AsyncMock(return_value=(resp, None))
        return pipeline

    async def test_querying_someone_elses_conversation_is_refused(self):
        """Reading turns was owner-scoped; asking a question was not.

        `ensure_conversation` does nothing when the row exists, so student A
        sending B's id had B's history loaded into A's prompt and A's turn
        appended to B's conversation.
        """
        from vektra_learn.api import course_query
        from vektra_learn.query import CourseQueryRequest

        conv_store = MagicMock()
        conv_store.get_metadata = AsyncMock(
            return_value={
                "id": uuid4(),
                "namespace_id": "CS101",
                "owner_subject": "student-b",
                "deleted_at": None,
                "turn_count": 4,
            }
        )
        conv_store.ensure_conversation = AsyncMock()
        pipeline = self._pipeline()

        with pytest.raises(HTTPException) as exc:
            await course_query(
                CourseQueryRequest(
                    question="What did they ask?", conversation_id=uuid4()
                ),
                self._make_request(conv_store, pipeline),
                {"sub": "student-a", "course_id": "CS101"},
                MagicMock(),
                AsyncMock(),
            )

        assert exc.value.status_code == 403
        assert exc.value.detail["error"]["code"] == "ERR-LEARN-007"
        # Refused before the pipeline could read the other student's history,
        # and before anything was written to their conversation.
        pipeline.execute.assert_not_called()
        conv_store.ensure_conversation.assert_not_called()

    async def test_own_conversation_still_works(self):
        from vektra_learn.api import course_query
        from vektra_learn.query import CourseQueryRequest

        cid = uuid4()
        conv_store = MagicMock()
        conv_store.get_metadata = AsyncMock(
            return_value={
                "id": cid,
                "namespace_id": "CS101",
                "owner_subject": "student-a",
                "deleted_at": None,
                "turn_count": 2,
            }
        )
        conv_store.ensure_conversation = AsyncMock()

        result = await course_query(
            CourseQueryRequest(question="Follow up", conversation_id=cid),
            self._make_request(conv_store, self._pipeline()),
            {"sub": "student-a", "course_id": "CS101"},
            MagicMock(),
            AsyncMock(),
        )
        assert result.answer == "..."

    async def test_unknown_conversation_id_is_created_for_the_caller(self):
        """A first query carries an id nobody owns yet: it becomes the caller's."""
        from vektra_learn.api import course_query
        from vektra_learn.query import CourseQueryRequest

        conv_store = MagicMock()
        conv_store.get_metadata = AsyncMock(return_value=None)
        conv_store.ensure_conversation = AsyncMock()

        await course_query(
            CourseQueryRequest(question="First"),
            self._make_request(conv_store, self._pipeline()),
            {"sub": "student-a", "course_id": "CS101"},
            MagicMock(),
            AsyncMock(),
        )
        assert (
            conv_store.ensure_conversation.call_args.kwargs["owner_subject"]
            == "student-a"
        )
