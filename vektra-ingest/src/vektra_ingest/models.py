"""SQLAlchemy ORM models for vektra-ingest.

These models map to tables owned by vektra-ingest (source_documents, ingest_jobs)
and the slim NamespaceOrm needed for FK resolution, as defined in the initial
Alembic migration (0001_initial_schema.py).

ORM models are internal to this package and must not be imported by other
vektra_* packages (ADR-0005).

NOTE: 'metadata' is reserved by SQLAlchemy DeclarativeBase.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

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
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class NamespaceOrm(Base):
    """Slim ORM mapping for namespaces table (FK resolution only).

    vektra-admin owns this table. This model exists only so SQLAlchemy
    can resolve the FK from source_documents.namespace_id.
    """

    __tablename__ = "namespaces"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)


class SourceDocumentOrm(Base):
    """ORM mapping for source_documents table (REQ-034, REQ-056, REQ-057, BR-005).

    Soft delete: set deleted_at and deletion_reason. Document chunks are
    hard-deleted separately in the same logical operation (BR-003).
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
    version: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("1"))
    supersedes_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_documents.id"),
        nullable=True,
    )
    # 'filename_aliases' stored as JSONB array (BR-005)
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


class IngestJobOrm(Base):
    """ORM mapping for ingest_jobs table (BR-004, REQ-014, ARCH-005).

    Tracks asynchronous ingestion jobs persisted in PostgreSQL (ADR-0006).
    Job status survives container restarts (NFR-005).
    """

    __tablename__ = "ingest_jobs"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    document_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_documents.id"),
        nullable=True,
    )
    namespace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("namespaces.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    phase: Mapped[str | None] = mapped_column(String(16), nullable=True)
    filename: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_message: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    chunk_count: Mapped[int | None] = mapped_column(INTEGER, nullable=True)
    percentage: Mapped[int | None] = mapped_column(INTEGER, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'indexed', 'failed')",
            name="ck_ingest_jobs_status",
        ),
        CheckConstraint(
            "phase IS NULL OR phase IN ('extracting', 'chunking', 'embedding')",
            name="ck_ingest_jobs_phase",
        ),
        CheckConstraint(
            "percentage IS NULL OR (percentage >= 0 AND percentage <= 100)",
            name="ck_ingest_jobs_percentage",
        ),
        Index("ix_ingest_jobs_status", "status"),
    )
