"""SQLAlchemy ORM models for vektra-index.

These models map to tables defined in the initial Alembic migration
(0001_initial_schema.py). ORM models are internal to this module
and must not be imported by other vektra_* packages.

DocumentChunk.content maps to the 'content' SQL column
(avoiding the SQL reserved word 'text').
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BIGINT,
    INTEGER,
    TEXT,
    TIMESTAMP,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class NamespaceOrm(Base):
    """ORM mapping for the namespaces table (ARCH-047).

    Required by SQLAlchemy's FK resolution: DocumentChunkOrm and
    SourceDocumentOrm both reference namespaces.id; without this model
    the ORM cannot determine flush ordering and raises NoReferencedTableError.
    """

    __tablename__ = "namespaces"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)


class DocumentChunkOrm(Base):
    """ORM mapping for document_chunks table (ARCH-044, ARCH-051)."""

    __tablename__ = "document_chunks"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    document_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    namespace_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("namespaces.id"),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(TEXT, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(384), nullable=False)
    # 'metadata' is reserved by SQLAlchemy DeclarativeBase; use chunk_metadata mapped to column "metadata"
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'")
    )
    element_type: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'text'")
    )
    content_format: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'text'")
    )
    position: Mapped[int] = mapped_column(INTEGER, nullable=False)
    index_version: Mapped[int] = mapped_column(
        INTEGER, nullable=False, server_default=text("1")
    )
    parent_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    sparse_vector: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    coordinates: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("position >= 0", name="ck_document_chunks_position"),
        CheckConstraint(
            "element_type IN ('text','table','title','list','image','header','footer','caption','page_break','formula')",
            name="ck_document_chunks_element_type",
        ),
        CheckConstraint(
            "content_format IN ('text','html','markdown')",
            name="ck_document_chunks_content_format",
        ),
        Index("ix_document_chunks_ns_version", "namespace_id", "index_version"),
        Index("ix_document_chunks_index_version", "index_version"),
    )


class SourceDocumentOrm(Base):
    """ORM mapping for source_documents table (ARCH-045, REQ-057).

    Soft delete: set deleted_at and deletion_reason; document chunks
    are hard-deleted separately in the same transaction.
    """

    __tablename__ = "source_documents"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    namespace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("namespaces.id"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False)
    chunk_count: Mapped[int | None] = mapped_column(INTEGER, nullable=True)
    version: Mapped[int] = mapped_column(
        INTEGER, nullable=False, server_default=text("1")
    )
    supersedes_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_documents.id"),
        nullable=True,
    )
    filename_aliases: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    deletion_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "deletion_reason IS NULL OR deletion_reason IN ('user_request','superseded','expired')",
            name="ck_source_documents_deletion_reason",
        ),
    )


class ReindexJobOrm(Base):
    """ORM mapping for reindex_jobs table (ARCH-045, REQ-064)."""

    __tablename__ = "reindex_jobs"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    namespace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("namespaces.id"), nullable=False
    )
    source_index_version: Mapped[int] = mapped_column(INTEGER, nullable=False)
    target_index_version: Mapped[int] = mapped_column(INTEGER, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    total_documents: Mapped[int] = mapped_column(
        INTEGER, nullable=False, server_default=text("0")
    )
    processed_documents: Mapped[int] = mapped_column(
        INTEGER, nullable=False, server_default=text("0")
    )
    current_document_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="ck_reindex_jobs_status",
        ),
        Index("ix_reindex_jobs_namespace_status", "namespace_id", "status"),
    )
