"""Conversation stores for multi-turn RAG queries (REQ-049, ARCH-031).

Two implementations:
- InMemoryConversationStore: dict-keyed, history lost on restart (Phase 1)
- PersistentConversationStore: PostgreSQL with pgcrypto encryption (Phase 2)

The ConversationStore Protocol defines the common interface used by
SimpleQueryPipeline.

Max turns per conversation is enforced on write; oldest turns are pruned.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import structlog
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vektra_core.models import ConversationOrm, ConversationTurnOrm

log = structlog.get_logger(__name__)


@runtime_checkable
class ConversationStore(Protocol):
    """Common interface for conversation stores."""

    async def get_history(
        self, conversation_id: UUID
    ) -> list[dict[str, str | None]]: ...

    async def add_turn(
        self,
        conversation_id: UUID,
        question: str,
        answer: str | None,
    ) -> None: ...

    async def clear(self, conversation_id: UUID) -> None: ...


@dataclass
class _InMemoryTurn:
    question: str
    answer: str | None


class InMemoryConversationStore:
    """Thread-safe in-memory store for conversation history.

    History is keyed by conversation_id UUID.
    Oldest turns are pruned when max_turns is exceeded.
    History is lost on restart (Phase 1 design).
    """

    def __init__(self, max_turns: int = 10) -> None:
        self._max_turns = max_turns
        self._store: dict[UUID, list[_InMemoryTurn]] = {}
        self._lock = asyncio.Lock()

    async def get_history(
        self, conversation_id: UUID
    ) -> list[dict[str, str | None]]:
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
            self._store[conversation_id].append(
                _InMemoryTurn(question=question, answer=answer)
            )
            if len(self._store[conversation_id]) > self._max_turns:
                self._store[conversation_id] = self._store[conversation_id][
                    -self._max_turns :
                ]

    async def clear(self, conversation_id: UUID) -> None:
        """Remove all turns for a conversation."""
        async with self._lock:
            self._store.pop(conversation_id, None)


class PersistentConversationStore:
    """PostgreSQL-backed conversation store with pgcrypto encryption (Phase 2).

    Conversation content is encrypted at rest via pgp_sym_encrypt/pgp_sym_decrypt.
    The encryption key is the VEKTRA_CONVERSATION_KEY env var.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        encryption_key: str,
        max_turns: int = 10,
    ) -> None:
        if not encryption_key:
            raise ValueError(
                "VEKTRA_CONVERSATION_KEY must be a non-empty string "
                "for persistent conversation storage"
            )
        self._session_factory = session_factory
        self._key = encryption_key
        self._max_turns = max_turns
        log.info("persistent_conversation_store_initialized")

    async def create_conversation(
        self,
        namespace_id: str,
        key_id: UUID,
        title: str | None = None,
    ) -> UUID:
        """Create a new conversation row. Returns the conversation UUID."""
        async with self._session_factory() as session:
            stmt = (
                insert(ConversationOrm)
                .values(
                    namespace_id=namespace_id,
                    key_id=key_id,
                    title=title,
                )
                .returning(ConversationOrm.id)
            )
            result = await session.execute(stmt)
            conversation_id = result.scalar_one()
            await session.commit()
            log.info(
                "conversation_created",
                conversation_id=str(conversation_id),
                namespace_id=namespace_id,
            )
            return conversation_id

    async def get_history(
        self, conversation_id: UUID
    ) -> list[dict[str, str | None]]:
        """Return decrypted conversation history ordered by turn_number."""
        async with self._session_factory() as session:
            stmt = (
                select(
                    func.pgp_sym_decrypt(
                        ConversationTurnOrm.question, self._key
                    ).label("question"),
                    func.pgp_sym_decrypt(
                        ConversationTurnOrm.answer, self._key
                    ).label("answer"),
                )
                .where(ConversationTurnOrm.conversation_id == conversation_id)
                .order_by(ConversationTurnOrm.turn_number)
            )
            result = await session.execute(stmt)
            return [
                {"question": row.question, "answer": row.answer}
                for row in result.all()
            ]

    async def add_turn(
        self,
        conversation_id: UUID,
        question: str,
        answer: str | None,
    ) -> None:
        """Encrypt and insert a turn, then prune oldest if exceeding max_turns."""
        async with self._session_factory() as session:
            # Lock the conversation row to serialize concurrent add_turn calls
            # and prevent duplicate turn_numbers (uq_conversation_turns_order).
            count_stmt = (
                select(ConversationOrm.turn_count)
                .where(ConversationOrm.id == conversation_id)
                .with_for_update()
            )
            count_result = await session.execute(count_stmt)
            current_count = count_result.scalar_one_or_none()
            if current_count is None:
                log.warning(
                    "conversation_not_found",
                    conversation_id=str(conversation_id),
                )
                return

            turn_number = current_count + 1

            # Insert encrypted turn
            encrypted_answer: Any = (
                func.pgp_sym_encrypt(answer, self._key) if answer is not None else None
            )
            insert_stmt = insert(ConversationTurnOrm).values(
                conversation_id=conversation_id,
                turn_number=turn_number,
                question=func.pgp_sym_encrypt(question, self._key),
                answer=encrypted_answer,
            )
            await session.execute(insert_stmt)

            # Update turn_count and updated_at
            update_stmt = (
                update(ConversationOrm)
                .where(ConversationOrm.id == conversation_id)
                .values(
                    turn_count=turn_number,
                    updated_at=func.now(),
                )
            )
            await session.execute(update_stmt)

            # Prune oldest turns if exceeding max_turns
            if turn_number > self._max_turns:
                prune_threshold = turn_number - self._max_turns
                prune_stmt = delete(ConversationTurnOrm).where(
                    ConversationTurnOrm.conversation_id == conversation_id,
                    ConversationTurnOrm.turn_number <= prune_threshold,
                )
                await session.execute(prune_stmt)

            await session.commit()

    async def clear(self, conversation_id: UUID) -> None:
        """Delete a conversation and all its turns (CASCADE)."""
        async with self._session_factory() as session:
            stmt = delete(ConversationOrm).where(
                ConversationOrm.id == conversation_id
            )
            await session.execute(stmt)
            await session.commit()
            log.info(
                "conversation_cleared",
                conversation_id=str(conversation_id),
            )

    async def get_metadata(
        self, conversation_id: UUID
    ) -> dict[str, Any] | None:
        """Return conversation metadata (no content). For GET endpoint."""
        async with self._session_factory() as session:
            stmt = select(
                ConversationOrm.id,
                ConversationOrm.namespace_id,
                ConversationOrm.created_at,
                ConversationOrm.updated_at,
                ConversationOrm.turn_count,
                ConversationOrm.title,
                ConversationOrm.deleted_at,
            ).where(ConversationOrm.id == conversation_id)
            result = await session.execute(stmt)
            row = result.one_or_none()
            if row is None:
                return None
            return {
                "id": row.id,
                "namespace_id": row.namespace_id,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "turn_count": row.turn_count,
                "title": row.title,
                "deleted_at": row.deleted_at,
            }

    async def soft_delete(self, conversation_id: UUID) -> bool:
        """Soft-delete a conversation (set deleted_at). Returns True if found."""
        async with self._session_factory() as session:
            stmt = (
                update(ConversationOrm)
                .where(
                    ConversationOrm.id == conversation_id,
                    ConversationOrm.deleted_at.is_(None),
                )
                .values(deleted_at=func.now())
            )
            result = await session.execute(stmt)
            await session.commit()
            row_count: int = result.rowcount  # type: ignore[attr-defined]
            return row_count > 0
