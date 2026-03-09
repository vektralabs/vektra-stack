"""SQLAlchemy ORM models for vektra-learn.

Maps to the enrollments and dashboard_tokens tables created by
migration 0005_learn_tables.py. ORM models are internal to this
package and must not be imported by other vektra_* packages (ADR-0005).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import TIMESTAMP, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class EnrollmentOrm(Base):
    """ORM mapping for enrollments table (student-course binding)."""

    __tablename__ = "enrollments"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    student_id: Mapped[str] = mapped_column(String(255), nullable=False)
    course_id: Mapped[str] = mapped_column(String(255), nullable=False)
    namespace: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("namespaces.id"),
        nullable=False,
    )
    enrolled_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "enrollment_metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )

    __table_args__ = (
        UniqueConstraint(
            "student_id", "course_id", name="uq_enrollment_student_course"
        ),
    )


class DashboardTokenOrm(Base):
    """ORM mapping for dashboard_tokens table (short-lived JWT tracking)."""

    __tablename__ = "dashboard_tokens"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    student_id: Mapped[str] = mapped_column(String(255), nullable=False)
    course_id: Mapped[str] = mapped_column(String(255), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
