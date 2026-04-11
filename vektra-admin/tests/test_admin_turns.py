"""Unit tests for admin conversation turns endpoint (DEBT-011)."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from vektra_admin.api import get_conversation_turns


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
