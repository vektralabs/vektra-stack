"""Unit tests for ConversationStore (REQ-049)."""
import asyncio
from uuid import uuid4

import pytest
from vektra_core.conversation import ConversationStore


async def test_get_history_empty_for_new_id():
    store = ConversationStore(max_turns=5)
    history = await store.get_history(uuid4())
    assert history == []


async def test_add_and_get_history():
    store = ConversationStore(max_turns=5)
    cid = uuid4()
    await store.add_turn(cid, "What is RAG?", "RAG is retrieval-augmented generation.")
    await store.add_turn(cid, "How does it work?", "It combines search with LLM.")

    history = await store.get_history(cid)
    assert len(history) == 2
    assert history[0]["question"] == "What is RAG?"
    assert history[0]["answer"] == "RAG is retrieval-augmented generation."
    assert history[1]["question"] == "How does it work?"


async def test_max_turns_prunes_oldest():
    store = ConversationStore(max_turns=3)
    cid = uuid4()
    for i in range(5):
        await store.add_turn(cid, f"Q{i}", f"A{i}")

    history = await store.get_history(cid)
    assert len(history) == 3
    # Should contain turns 2, 3, 4 (oldest 0, 1 pruned)
    assert history[0]["question"] == "Q2"
    assert history[-1]["question"] == "Q4"


async def test_add_turn_with_none_answer():
    store = ConversationStore()
    cid = uuid4()
    await store.add_turn(cid, "Is this allowed?", None)
    history = await store.get_history(cid)
    assert history[0]["answer"] is None


async def test_clear_removes_history():
    store = ConversationStore()
    cid = uuid4()
    await store.add_turn(cid, "Hello", "Hi")
    await store.clear(cid)
    assert await store.get_history(cid) == []


async def test_independent_conversations():
    store = ConversationStore(max_turns=10)
    cid1, cid2 = uuid4(), uuid4()
    await store.add_turn(cid1, "Question A", "Answer A")
    await store.add_turn(cid2, "Question B", "Answer B")

    assert len(await store.get_history(cid1)) == 1
    assert len(await store.get_history(cid2)) == 1
    assert (await store.get_history(cid1))[0]["question"] == "Question A"


async def test_concurrent_add_is_safe():
    """Multiple coroutines adding turns concurrently should not lose data."""
    store = ConversationStore(max_turns=50)
    cid = uuid4()

    async def add_turns(start: int) -> None:
        for i in range(5):
            await store.add_turn(cid, f"Q{start+i}", f"A{start+i}")

    await asyncio.gather(add_turns(0), add_turns(10), add_turns(20))
    history = await store.get_history(cid)
    assert len(history) == 15
