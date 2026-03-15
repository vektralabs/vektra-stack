"""SQLAlchemy ORM models for vektra-core.

These models map to tables owned by vektra-core (conversations,
conversation_turns, feedback), as defined in migration 0002_phase2_tables.py.
ORM models are internal to this package and must not be imported by other
vektra_* packages (ADR-0005).

Encryption columns (question, answer in conversation_turns) are BYTEA
storing pgp_sym_encrypt output. Encryption/decryption happens at the
query layer via PersistentConversationStore, not in the ORM model.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    INTEGER,
    SMALLINT,
    TEXT,
    TIMESTAMP,
    CheckConstraint,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class NamespaceOrm(Base):
    """ORM mapping for the namespaces table.

    Required by SQLAlchemy's FK resolution: ConversationOrm and FeedbackOrm
    reference namespaces.id; without this model the ORM cannot determine
    flush ordering and raises NoReferencedTableError.
    """

    __tablename__ = "namespaces"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ConversationOrm(Base):
    """ORM mapping for conversations table (ARCH-031, REQ-049).

    Persistent multi-turn conversations with soft delete for GDPR.
    key_id is NOT a FK: revoked/deleted keys must still appear in history.
    """

    __tablename__ = "conversations"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    namespace_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("namespaces.id"),
        nullable=False,
    )
    key_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    turn_count: Mapped[int] = mapped_column(
        INTEGER, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint("turn_count >= 0", name="ck_conversations_turn_count"),
        Index("ix_conversations_namespace", "namespace_id"),
        Index("ix_conversations_key_id", "key_id"),
        Index(
            "ix_conversations_active",
            "namespace_id",
            "updated_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class ConversationTurnOrm(Base):
    """ORM mapping for conversation_turns table (ARCH-031, ADR-0011).

    question and answer are BYTEA columns storing pgp_sym_encrypt output.
    Encryption/decryption is handled by PersistentConversationStore, not here.
    """

    __tablename__ = "conversation_turns"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    conversation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    turn_number: Mapped[int] = mapped_column(INTEGER, nullable=False)
    question: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    answer: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    response_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(INTEGER, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(INTEGER, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("turn_number >= 1", name="ck_conversation_turns_number"),
        UniqueConstraint(
            "conversation_id", "turn_number", name="uq_conversation_turns_order"
        ),
        Index("ix_conversation_turns_conversation", "conversation_id"),
        Index(
            "ix_conversation_turns_response",
            "response_id",
            postgresql_where=text("response_id IS NOT NULL"),
        ),
    )


class FeedbackOrm(Base):
    """ORM mapping for feedback table (REQ-055).

    response_id is NOT a FK: QueryResponse is ephemeral in Phase 1.
    citation_id NULL = response-level feedback, non-NULL = citation-level.
    key_id is NOT a FK: revoked/deleted keys must still appear in feedback.
    """

    __tablename__ = "feedback"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    response_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    citation_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    namespace_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("namespaces.id"),
        nullable=False,
    )
    key_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    rating: Mapped[int] = mapped_column(SMALLINT, nullable=False)
    comment: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("rating >= 1 AND rating <= 5", name="ck_feedback_rating"),
        Index("ix_feedback_response", "response_id"),
        Index("ix_feedback_namespace", "namespace_id"),
        Index("ix_feedback_created", "created_at"),
    )
