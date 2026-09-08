"""Unit tests for PersistentConversationStore (REQ-049, ARCH-031).

Uses mock AsyncSession to verify SQL generation without a live database.
Encryption round-trip requires PostgreSQL with pgcrypto (integration test).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from vektra_core.conversation import (
    ConversationStore,
    InMemoryConversationStore,
    PersistentConversationStore,
)

# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _mock_session_factory():
    """Create a mock async_sessionmaker that yields a mock AsyncSession."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    @asynccontextmanager
    async def _factory():
        yield session

    factory = MagicMock()
    factory.side_effect = _factory
    factory._mock_session = session  # expose for assertions
    return factory


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


def test_in_memory_satisfies_protocol():
    store = InMemoryConversationStore()
    assert isinstance(store, ConversationStore)


def test_persistent_satisfies_protocol():
    factory = _mock_session_factory()
    store = PersistentConversationStore(factory, "test-key")
    assert isinstance(store, ConversationStore)


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_constructor_rejects_empty_key():
    factory = _mock_session_factory()
    with pytest.raises(ValueError, match="non-empty string"):
        PersistentConversationStore(factory, "")


def test_constructor_accepts_valid_key():
    factory = _mock_session_factory()
    store = PersistentConversationStore(factory, "my-secret-key")
    assert store is not None


# ---------------------------------------------------------------------------
# create_conversation
# ---------------------------------------------------------------------------


async def test_create_conversation_returns_uuid():
    factory = _mock_session_factory()
    session = factory._mock_session
    expected_id = uuid4()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = expected_id
    session.execute.return_value = mock_result

    store = PersistentConversationStore(factory, "key123")
    result = await store.create_conversation(namespace_id="test-ns", key_id=uuid4())

    assert result == expected_id
    session.execute.assert_called_once()
    session.commit.assert_called_once()


# ---------------------------------------------------------------------------
# get_history
# ---------------------------------------------------------------------------


async def test_get_history_returns_decrypted_turns():
    factory = _mock_session_factory()
    session = factory._mock_session

    # Simulate two rows returned by pgp_sym_decrypt
    row1 = MagicMock()
    row1.question = "What is RAG?"
    row1.answer = "Retrieval-augmented generation."
    row2 = MagicMock()
    row2.question = "How does it work?"
    row2.answer = "It combines search with LLM."

    mock_result = MagicMock()
    mock_result.all.return_value = [row1, row2]
    session.execute.return_value = mock_result

    store = PersistentConversationStore(factory, "key123")
    history = await store.get_history(uuid4())

    assert len(history) == 2
    assert history[0]["question"] == "What is RAG?"
    assert history[0]["answer"] == "Retrieval-augmented generation."
    assert history[1]["question"] == "How does it work?"


async def test_get_history_empty_conversation():
    factory = _mock_session_factory()
    session = factory._mock_session

    mock_result = MagicMock()
    mock_result.all.return_value = []
    session.execute.return_value = mock_result

    store = PersistentConversationStore(factory, "key123")
    history = await store.get_history(uuid4())

    assert history == []


# ---------------------------------------------------------------------------
# add_turn
# ---------------------------------------------------------------------------


async def test_add_turn_calls_insert_update_commit():
    factory = _mock_session_factory()
    session = factory._mock_session

    # First execute: SELECT turn_count returns 2
    count_result = MagicMock()
    count_result.scalar_one_or_none.return_value = 2
    # Subsequent executes: INSERT and UPDATE
    session.execute.side_effect = [count_result, MagicMock(), MagicMock()]

    store = PersistentConversationStore(factory, "key123", max_turns=10)
    await store.add_turn(uuid4(), "Question?", "Answer!")

    # Should have 3 executes: SELECT count, INSERT turn, UPDATE conversation
    assert session.execute.call_count == 3
    session.commit.assert_called_once()


async def test_add_turn_prunes_when_exceeding_max():
    factory = _mock_session_factory()
    session = factory._mock_session

    # turn_count=5, max_turns=3, so turn_number=6 > max_turns → prune
    count_result = MagicMock()
    count_result.scalar_one_or_none.return_value = 5
    session.execute.side_effect = [
        count_result,
        MagicMock(),  # INSERT
        MagicMock(),  # UPDATE
        MagicMock(),  # DELETE (prune)
    ]

    store = PersistentConversationStore(factory, "key123", max_turns=3)
    await store.add_turn(uuid4(), "Q6", "A6")

    # 4 executes: SELECT + INSERT + UPDATE + DELETE (prune)
    assert session.execute.call_count == 4
    session.commit.assert_called_once()


async def test_add_turn_skips_on_missing_conversation():
    factory = _mock_session_factory()
    session = factory._mock_session

    count_result = MagicMock()
    count_result.scalar_one_or_none.return_value = None
    session.execute.return_value = count_result

    store = PersistentConversationStore(factory, "key123")
    await store.add_turn(uuid4(), "Q?", "A!")

    # Only the SELECT for turn_count, then returns early
    session.execute.assert_called_once()
    session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


async def test_clear_deletes_conversation():
    factory = _mock_session_factory()
    session = factory._mock_session

    store = PersistentConversationStore(factory, "key123")
    await store.clear(uuid4())

    session.execute.assert_called_once()
    session.commit.assert_called_once()


# ---------------------------------------------------------------------------
# get_metadata
# ---------------------------------------------------------------------------


async def test_get_metadata_returns_dict():
    factory = _mock_session_factory()
    session = factory._mock_session

    conv_id = uuid4()
    row = MagicMock()
    row.id = conv_id
    row.namespace_id = "test-ns"
    row.created_at = "2026-03-01T00:00:00Z"
    row.updated_at = "2026-03-01T00:00:00Z"
    row.turn_count = 3
    row.title = "Test conversation"
    row.deleted_at = None

    mock_result = MagicMock()
    mock_result.one_or_none.return_value = row
    session.execute.return_value = mock_result

    store = PersistentConversationStore(factory, "key123")
    meta = await store.get_metadata(conv_id)

    assert meta is not None
    assert meta["id"] == conv_id
    assert meta["turn_count"] == 3
    assert meta["deleted_at"] is None


async def test_get_metadata_returns_none_for_missing():
    factory = _mock_session_factory()
    session = factory._mock_session

    mock_result = MagicMock()
    mock_result.one_or_none.return_value = None
    session.execute.return_value = mock_result

    store = PersistentConversationStore(factory, "key123")
    meta = await store.get_metadata(uuid4())

    assert meta is None


# ---------------------------------------------------------------------------
# soft_delete
# ---------------------------------------------------------------------------


async def test_soft_delete_returns_true_when_found():
    factory = _mock_session_factory()
    session = factory._mock_session

    mock_result = MagicMock()
    mock_result.rowcount = 1
    session.execute.return_value = mock_result

    store = PersistentConversationStore(factory, "key123")
    result = await store.soft_delete(uuid4())

    assert result is True
    session.commit.assert_called_once()


async def test_soft_delete_returns_false_when_not_found():
    factory = _mock_session_factory()
    session = factory._mock_session

    mock_result = MagicMock()
    mock_result.rowcount = 0
    session.execute.return_value = mock_result

    store = PersistentConversationStore(factory, "key123")
    result = await store.soft_delete(uuid4())

    assert result is False


# ---------------------------------------------------------------------------
# Conversation ownership (FEAT-027)
# ---------------------------------------------------------------------------


def _compiled_sql(session) -> str:
    """The SQL of the last statement the store executed, as text."""
    stmt = session.execute.call_args.args[0]
    return str(stmt.compile(compile_kwargs={"literal_binds": False}))


async def test_create_conversation_records_the_owner():
    factory = _mock_session_factory()
    session = factory._mock_session
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = uuid4()
    session.execute.return_value = mock_result

    store = PersistentConversationStore(factory, "key123")
    await store.create_conversation(
        namespace_id="CS101", key_id=uuid4(), owner_subject="student-42"
    )

    assert "owner_subject" in _compiled_sql(session)


async def test_ensure_conversation_records_the_owner():
    factory = _mock_session_factory()
    session = factory._mock_session
    store = PersistentConversationStore(factory, "key123")

    await store.ensure_conversation(
        conversation_id=uuid4(),
        namespace_id="CS101",
        key_id=uuid4(),
        owner_subject="student-42",
    )

    sql = _compiled_sql(session)
    assert "owner_subject" in sql
    # ON CONFLICT DO NOTHING: a second caller sending someone else's
    # conversation id must not overwrite the owner.
    assert "ON CONFLICT" in sql.upper()
    assert "DO UPDATE" not in sql.upper()


async def test_list_conversations_scopes_to_owner_and_excludes_deleted():
    factory = _mock_session_factory()
    session = factory._mock_session
    mock_result = MagicMock()
    mock_result.all.return_value = []
    session.execute.return_value = mock_result

    store = PersistentConversationStore(factory, "key123")
    rows = await store.list_conversations(
        namespace_id="CS101", owner_subject="student-42"
    )

    assert rows == []
    sql = _compiled_sql(session)
    assert "owner_subject" in sql
    assert "namespace_id" in sql
    assert "deleted_at IS NULL" in sql
    assert "ORDER BY" in sql.upper()


async def test_soft_delete_can_be_scoped_to_the_owner():
    factory = _mock_session_factory()
    session = factory._mock_session
    mock_result = MagicMock()
    mock_result.rowcount = 1
    session.execute.return_value = mock_result

    store = PersistentConversationStore(factory, "key123")
    deleted = await store.soft_delete(
        uuid4(), namespace="CS101", owner_subject="student-42"
    )

    assert deleted is True
    assert "owner_subject" in _compiled_sql(session)


async def test_soft_delete_without_owner_is_unscoped():
    """Admin deletion keeps working: the scoping is opt-in, per caller."""
    factory = _mock_session_factory()
    session = factory._mock_session
    mock_result = MagicMock()
    mock_result.rowcount = 1
    session.execute.return_value = mock_result

    store = PersistentConversationStore(factory, "key123")
    await store.soft_delete(uuid4(), namespace="CS101")

    assert "owner_subject" not in _compiled_sql(session)
