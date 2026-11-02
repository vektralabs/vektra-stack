"""SQLAlchemy ORM mirrors of the ``assess_*`` tables (ADR-0022 style).

Internal to ``vektra_assess``: no other package imports these. The migrations
(``migrations/versions/0010``..``0013``) are the source of truth for constraints,
triggers and grants; ``tests/test_models_match_schema.py`` fails when a mirror drifts
from the migrated schema.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    BIGINT,
    INTEGER,
    TEXT,
    TIMESTAMP,
    BigInteger,
    Boolean,
    Computed,
    ForeignKey,
    LargeBinary,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_UUID_PK = {"server_default": text("gen_random_uuid()")}
_NOW = text("now()")


class Base(DeclarativeBase):
    pass


class SiteOrm(Base):
    __tablename__ = "assess_sites"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, **_UUID_PK
    )
    moodle_site_uuid: Mapped[str] = mapped_column(TEXT, unique=True)
    base_url: Mapped[str] = mapped_column(TEXT)
    release: Mapped[str | None] = mapped_column(TEXT)
    min_plugin_version: Mapped[str | None] = mapped_column(TEXT)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=_NOW
    )
    disabled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class SiteKeyOrm(Base):
    __tablename__ = "assess_site_keys"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, **_UUID_PK
    )
    site_id: Mapped[UUID] = mapped_column(
        ForeignKey("assess_sites.id", ondelete="CASCADE")
    )
    api_key_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), unique=True)
    purpose: Mapped[str] = mapped_column(TEXT)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=_NOW
    )


class SiteCapabilityOrm(Base):
    __tablename__ = "assess_site_capabilities"

    site_id: Mapped[UUID] = mapped_column(
        ForeignKey("assess_sites.id", ondelete="CASCADE"), primary_key=True
    )
    flag: Mapped[str] = mapped_column(TEXT, primary_key=True)
    release: Mapped[str | None] = mapped_column(TEXT)
    state: Mapped[str] = mapped_column(TEXT)
    evidence_hash: Mapped[str | None] = mapped_column(TEXT)
    checked_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=_NOW
    )


class SetupCheckOrm(Base):
    __tablename__ = "assess_setup_checks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_id: Mapped[UUID] = mapped_column(
        ForeignKey("assess_sites.id", ondelete="CASCADE")
    )
    item: Mapped[str] = mapped_column(TEXT)
    result: Mapped[str] = mapped_column(TEXT)
    run_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=_NOW
    )


class ActivityConfigOrm(Base):
    __tablename__ = "assess_activity_config"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, **_UUID_PK
    )
    site_id: Mapped[UUID] = mapped_column(
        ForeignKey("assess_sites.id", ondelete="CASCADE")
    )
    cmid: Mapped[int] = mapped_column(BIGINT)
    activity_type: Mapped[str] = mapped_column(TEXT)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    flags: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    rag_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    end_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    attested_opening_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    attested_by: Mapped[str | None] = mapped_column(TEXT)
    recipients_extra: Mapped[list[str]] = mapped_column(
        ARRAY(TEXT), server_default=text("'{}'")
    )
    teacher_config_sealed: Mapped[bytes | None] = mapped_column(LargeBinary)
    teacher_config_kid: Mapped[str | None] = mapped_column(TEXT)
    teacher_config_set: Mapped[bool] = mapped_column(
        Boolean, Computed("teacher_config_sealed IS NOT NULL", persisted=True)
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=_NOW
    )


class ExtraTimeOrm(Base):
    __tablename__ = "assess_extra_time"

    site_id: Mapped[UUID] = mapped_column(
        ForeignKey("assess_sites.id", ondelete="CASCADE"), primary_key=True
    )
    cmid: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    student_userid: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    extra_seconds: Mapped[int] = mapped_column(INTEGER)
    granted_by: Mapped[str | None] = mapped_column(TEXT)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=_NOW
    )


class ResetGrantOrm(Base):
    __tablename__ = "assess_reset_grants"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, **_UUID_PK
    )
    site_id: Mapped[UUID] = mapped_column(
        ForeignKey("assess_sites.id", ondelete="CASCADE")
    )
    cmid: Mapped[int] = mapped_column(BIGINT)
    student_userid: Mapped[int] = mapped_column(BIGINT)
    granted_by: Mapped[str | None] = mapped_column(TEXT)
    granted_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=_NOW
    )
    consumed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
