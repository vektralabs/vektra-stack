"""In-memory conversation store for multi-turn RAG queries (REQ-049).

ConversationStore is a dict keyed by conversation_id (UUID). History is lost
on restart — this is the Phase 1 design (documented, not a bug). Phase 2 will
add persistent storage with encryption (ARCH-031).

Max turns per conversation is enforced on write; oldest turns are pruned.
All methods are asyncio-safe via an asyncio.Lock.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID


@dataclass
class ConversationTurn:
    question: str
    answer: str | None


class ConversationStore:
    """Thread-safe in-memory store for conversation history.

    History is keyed by conversation_id UUID.
    Oldest turns are pruned when max_turns is exceeded.
    """

    def __init__(self, max_turns: int = 10) -> None:
        self._max_turns = max_turns
        self._store: dict[UUID, list[ConversationTurn]] = {}
        self._lock = asyncio.Lock()

    async def get_history(self, conversation_id: UUID) -> list[dict[str, str | None]]:
        """Return conversation history as a list of dicts (oldest first)."""
        async with self._lock:
            turns = self._store.get(conversation_id, [])
            return [{"question": t.question, "answer": t.answer} for t in turns]

    async def add_turn(
        self,
        conversation_id: UUID,
        question: str,
        answer: str | None,
    ) -> None:
        """Append a turn and prune to max_turns (oldest removed first)."""
        async with self._lock:
            if conversation_id not in self._store:
                self._store[conversation_id] = []
            self._store[conversation_id].append(ConversationTurn(question=question, answer=answer))
            if len(self._store[conversation_id]) > self._max_turns:
                self._store[conversation_id] = self._store[conversation_id][-self._max_turns:]

    async def clear(self, conversation_id: UUID) -> None:
        """Remove all turns for a conversation."""
        async with self._lock:
            self._store.pop(conversation_id, None)
