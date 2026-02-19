"""SQLAlchemy ORM models for vektra-admin.

These models map to tables owned by vektra-admin (api_keys, audit_log,
namespaces, system_state), as defined in the initial Alembic migration
(0001_initial_schema.py). ORM models are internal to this package and
must not be imported by other vektra_* packages (ADR-0005).

NOTE: 'metadata' is reserved by SQLAlchemy DeclarativeBase.
In AuditLogOrm the column is mapped as 'log_metadata' pointing to the
'metadata' SQL column.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ARRAY,
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
    """ORM mapping for namespaces table (ARCH-047).

    vektra-admin owns this table; provides namespace CRUD and bootstrap
    namespace seeding. Other packages reference this table via a slim
    model (e.g. vektra-index NamespaceOrm); this version has all columns.
    """

    __tablename__ = "namespaces"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_key_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("api_keys.id", ondelete="SET NULL"),
        nullable=True,
    )
    quota_chunks: Mapped[int | None] = mapped_column(INTEGER, nullable=True)
    quota_documents: Mapped[int | None] = mapped_column(INTEGER, nullable=True)
    # 'config' is not reserved by SQLAlchemy but we alias for clarity
    ns_config: Mapped[dict[str, Any]] = mapped_column(
        "config", JSONB, nullable=False, server_default=text("'{}'")
    )
    retention_days: Mapped[int | None] = mapped_column(INTEGER, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "retention_days IS NULL OR retention_days > 0",
            name="ck_namespaces_retention",
        ),
    )


class ApiKeyOrm(Base):
    """ORM mapping for api_keys table (REQ-023, ARCH-023).

    key_hash stores the argon2id hash (never the plaintext key).
    key_preview is the last 4 characters of the plaintext key.
    revoked_at NULL means the key is active; non-NULL means revoked.
    """

    __tablename__ = "api_keys"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    key_preview: Mapped[str] = mapped_column(String(4), nullable=False)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # PostgreSQL TEXT[] array; server default is '{admin}' (single-element array)
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(TEXT), nullable=False, server_default=text("'{admin}'")
    )
    rate_limit_rpm: Mapped[int | None] = mapped_column(INTEGER, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "scopes <@ ARRAY['admin','ingest','query']::text[]",
            name="ck_api_keys_scopes",
        ),
    )


class AuditLogOrm(Base):
    """ORM mapping for audit_log table (REQ-022, NFR-007).

    key_id is intentionally NOT a FK: revoked or deleted keys must still
    appear in the audit history per REQ-022. Never contains query text or
    response content (REQ-051).

    'metadata' is reserved by DeclarativeBase; the column is mapped via
    'log_metadata' with column name 'metadata'.
    """

    __tablename__ = "audit_log"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # NOT a FK — intentional, see docstring
    key_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, index=True
    )
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    status_code: Mapped[int] = mapped_column(INTEGER, nullable=False)
    request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    log_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_audit_log_created_at", "created_at"),
        Index("ix_audit_log_action", "action"),
    )


class SystemStateOrm(Base):
    """ORM mapping for system_state table (REQ-036).

    Tracks persistent flags across restarts. The 'bootstrap_consumed' key
    is seeded to 'false' by the initial migration (0001_initial_schema.py).
    Atomically updated via SELECT ... FOR UPDATE to prevent race conditions.
    """

    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(TEXT, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
