"""SQLAlchemy ORM models for vektra-analytics.

Maps to the query_traces table created by migration 0002_phase2_tables.py.
ORM models are internal to this package and must not be imported by other
vektra_* packages (ADR-0005).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    INTEGER,
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


class QueryTraceOrm(Base):
    """ORM mapping for query_traces table (ARCH-041, REQ-060).

    Steps and chunks_retrieved are stored as JSONB for query flexibility.
    No separate step_traces table: step data is always read with the parent trace.
    """

    __tablename__ = "query_traces"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    response_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        unique=True,
    )
    namespace_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("namespaces.id"),
        nullable=False,
    )
    steps: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    total_duration_ms: Mapped[int] = mapped_column(INTEGER, nullable=False)
    chunks_retrieved: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    llm_model: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("total_duration_ms >= 0", name="ck_query_traces_duration"),
        Index("ix_query_traces_namespace", "namespace_id"),
        Index("ix_query_traces_created", "created_at"),
    )
