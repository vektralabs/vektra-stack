"""Unit tests for admin conversation turns endpoint (DEBT-011) and the
shared request_id fallback helper (DEBT-020)."""

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from vektra_admin.api import _resolve_request_id, get_conversation_turns


@pytest.mark.asyncio
async def test_returns_turns_from_persistent_store():
    """Admin endpoint should return decrypted turns from persistent store."""
    cid = uuid4()
    rid = uuid4()
    turns = [
        {
            "turn_number": 1,
            "question": "What is RAG?",
            "answer": "Retrieval-augmented generation.",
            "response_id": rid,
            "model": "gpt-4o",
            "prompt_tokens": 150,
            "completion_tokens": 30,
            "created_at": "2026-03-28T10:00:00+00:00",
        }
    ]

    conv_store = MagicMock()
    conv_store.get_turns_detail = AsyncMock(return_value=turns)

    registry = MagicMock()
    registry.get = MagicMock(return_value=conv_store)

    app_state = MagicMock()
    app_state.registry = registry

    request = MagicMock()
    request.app.state = app_state
    request.state.request_id = "test-req-id"

    background_tasks = MagicMock()
    key_info = MagicMock()
    key_info.scopes = ["admin"]

    result = await get_conversation_turns(cid, request, background_tasks, key_info)
    assert result == turns
    conv_store.get_turns_detail.assert_called_once_with(cid)

    # Verify audit log was queued
    background_tasks.add_task.assert_called_once()
    call_kwargs = background_tasks.add_task.call_args.kwargs
    assert call_kwargs["action"] == "conversation_turns_read"
    assert call_kwargs["log_metadata"]["conversation_id"] == str(cid)
    assert call_kwargs["log_metadata"]["turn_count"] == 1


@pytest.mark.asyncio
async def test_returns_404_when_conversation_not_found():
    """Should raise 404 when conversation doesn't exist."""
    conv_store = MagicMock()
    conv_store.get_turns_detail = AsyncMock(return_value=None)

    registry = MagicMock()
    registry.get = MagicMock(return_value=conv_store)

    app_state = MagicMock()
    app_state.registry = registry

    request = MagicMock()
    request.app.state = app_state

    key_info = MagicMock()

    with pytest.raises(HTTPException) as exc_info:
        await get_conversation_turns(uuid4(), request, MagicMock(), key_info)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_audits_with_synthetic_request_id_when_middleware_missing():
    """Sensitive read endpoints must audit even if the request-id middleware
    didn't populate ``request.state.request_id`` (NFR-007 / DEBT-020).
    """
    cid = uuid4()
    turns = [
        {
            "turn_number": 1,
            "question": "Q",
            "answer": "A",
            "response_id": uuid4(),
            "model": "x",
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "created_at": "2026-03-28T10:00:00+00:00",
        }
    ]

    conv_store = MagicMock()
    conv_store.get_turns_detail = AsyncMock(return_value=turns)

    registry = MagicMock()
    registry.get = MagicMock(return_value=conv_store)

    app_state = MagicMock()
    app_state.registry = registry

    request = MagicMock()
    request.app.state = app_state
    request.state.request_id = None  # middleware did not set it

    background_tasks = MagicMock()
    key_info = MagicMock()
    key_info.scopes = ["admin"]

    result = await get_conversation_turns(cid, request, background_tasks, key_info)
    assert result == turns

    # Audit must fire with a synthetic UUID request_id rather than skip.
    background_tasks.add_task.assert_called_once()
    call_kwargs = background_tasks.add_task.call_args.kwargs
    assert call_kwargs["action"] == "conversation_turns_read"
    assert isinstance(call_kwargs["request_id"], UUID)


def test_resolve_request_id_returns_existing_when_present():
    """Helper should pass through a UUID set by the middleware."""
    rid = uuid4()
    request = MagicMock()
    request.state.request_id = rid
    assert _resolve_request_id(request) == rid


def test_resolve_request_id_synthesizes_uuid_when_missing(caplog):
    """Helper should synthesize a UUID and log a warning when state attr missing."""
    request = MagicMock()
    request.state.request_id = None

    result = _resolve_request_id(request)

    assert isinstance(result, UUID)
    # Two distinct calls produce distinct UUIDs (no shared state).
    request2 = MagicMock()
    request2.state.request_id = None
    assert _resolve_request_id(request2) != result


@pytest.mark.asyncio
async def test_returns_501_for_inmemory_store():
    """Should raise 501 when conversation store has no get_turns_detail method."""
    conv_store = MagicMock(spec=[])  # no methods

    registry = MagicMock()
    registry.get = MagicMock(return_value=conv_store)

    app_state = MagicMock()
    app_state.registry = registry

    request = MagicMock()
    request.app.state = app_state

    key_info = MagicMock()

    with pytest.raises(HTTPException) as exc_info:
        await get_conversation_turns(uuid4(), request, MagicMock(), key_info)
    assert exc_info.value.status_code == 501
