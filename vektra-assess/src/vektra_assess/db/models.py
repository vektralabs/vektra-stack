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
    BOOLEAN,
    CHAR,
    INTEGER,
    TEXT,
    TIMESTAMP,
    VARCHAR,
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

_GEN_UUID = text("gen_random_uuid()")
_NOW = text("now()")


class Base(DeclarativeBase):
    pass


class SiteOrm(Base):
    __tablename__ = "assess_sites"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=_GEN_UUID
    )
    moodle_site_uuid: Mapped[str] = mapped_column(TEXT, unique=True)
    base_url: Mapped[str] = mapped_column(TEXT)
    release: Mapped[str | None] = mapped_column(TEXT)
    min_plugin_version: Mapped[str | None] = mapped_column(TEXT)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=_NOW
    )
    disabled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    probe_manifest: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_seen_release: Mapped[str | None] = mapped_column(TEXT)
    moodle_origin: Mapped[str | None] = mapped_column(TEXT)  # revision 0033 (plan 9)


class SiteKeyOrm(Base):
    __tablename__ = "assess_site_keys"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=_GEN_UUID
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
    reason_code: Mapped[str | None] = mapped_column(TEXT)
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
        PG_UUID(as_uuid=True), primary_key=True, server_default=_GEN_UUID
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
    # revision 0022: the session cap BR-013 (5-120 min); NULL = activity not configured yet
    cap_minutes: Mapped[int | None] = mapped_column(INTEGER)


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
        PG_UUID(as_uuid=True), primary_key=True, server_default=_GEN_UUID
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


def _ts(nullable: bool = False, default: bool = False) -> Mapped[Any]:
    return mapped_column(
        TIMESTAMP(timezone=True),
        nullable=nullable,
        server_default=_NOW if default else None,
    )


class SessionOrm(Base):
    __tablename__ = "assess_sessions"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=_GEN_UUID
    )
    site_id: Mapped[UUID] = mapped_column(ForeignKey("assess_sites.id"))
    cmid: Mapped[int] = mapped_column(BIGINT)
    student_userid: Mapped[int] = mapped_column(BIGINT)
    session_no: Mapped[int] = mapped_column(
        INTEGER
    )  # numbered by a BEFORE INSERT trigger
    attempt_ref: Mapped[str | None] = mapped_column(TEXT)
    state: Mapped[str] = mapped_column(TEXT)
    close_reason: Mapped[str | None] = mapped_column(TEXT)
    starting_expires_at: Mapped[datetime | None] = _ts(nullable=True)
    deadline_at: Mapped[datetime | None] = _ts(nullable=True)
    credited_outage_s: Mapped[int] = mapped_column(INTEGER, server_default=text("0"))
    tokens_used: Mapped[int] = mapped_column(
        BigInteger, server_default=text("0")
    )  # revision 0027
    seq: Mapped[int] = mapped_column(INTEGER, server_default=text("0"))
    epoch: Mapped[int] = mapped_column(INTEGER, server_default=text("1"))
    notice_version: Mapped[str | None] = mapped_column(TEXT)
    regrade_flag: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    committed_at: Mapped[datetime | None] = _ts(nullable=True)
    closed_at: Mapped[datetime | None] = _ts(nullable=True)
    last_activity_at: Mapped[datetime | None] = _ts(nullable=True)
    created_at: Mapped[datetime] = _ts(default=True)
    # revision 0022 (plan assess-session-lifecycle)
    courseid_hint: Mapped[int | None] = mapped_column(BIGINT)
    duration_s: Mapped[int | None] = mapped_column(INTEGER)
    frozen_policy: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    preflight_started_at: Mapped[datetime | None] = _ts(nullable=True)
    # revision 0023: set by a student turn, cleared by the bot turn (assess_append_turn)
    pending_reply_since: Mapped[datetime | None] = _ts(nullable=True)
    # revision 0024: the reply grace frozen at commit, read by assess_append_turn
    reply_grace_s: Mapped[int] = mapped_column(INTEGER, server_default=text("60"))
    # revision 0034 (plan 10): the activity cap frozen at the commit
    cap_s: Mapped[int | None] = mapped_column(INTEGER)


class StartAttemptOrm(Base):
    __tablename__ = "assess_start_attempts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_id: Mapped[UUID] = mapped_column(ForeignKey("assess_sites.id"))
    cmid: Mapped[int] = mapped_column(BIGINT)
    student_userid: Mapped[int] = mapped_column(BIGINT)
    outcome: Mapped[str] = mapped_column(TEXT)
    reason_code: Mapped[str | None] = mapped_column(TEXT)
    created_at: Mapped[datetime] = _ts(default=True)
    session_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))


class TurnFlagOrm(Base):
    """REQ-103 'segnalato dallo studente' marker (revision 0030): one per bot turn."""

    __tablename__ = "assess_turn_flags"

    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("assess_sessions.id", ondelete="CASCADE"), primary_key=True
    )
    turn_no: Mapped[int] = mapped_column(INTEGER, primary_key=True)
    created_at: Mapped[datetime] = _ts(default=True)


class StartBreakerOrm(Base):
    """Per-site Start breaker (revision 0028, plan 8 M5.4): worker-written, API-read."""

    __tablename__ = "assess_start_breaker"

    site_id: Mapped[UUID] = mapped_column(
        ForeignKey("assess_sites.id", ondelete="CASCADE"), primary_key=True
    )
    state: Mapped[str] = mapped_column(TEXT)
    opened_at: Mapped[datetime | None] = _ts(nullable=True)
    half_open_at: Mapped[datetime | None] = _ts(nullable=True)
    updated_at: Mapped[datetime] = _ts(default=True)


class TokenJtiOrm(Base):
    """A minted pre-session token: single-use store and mint-to-Start correlation (0022)."""

    __tablename__ = "assess_token_jti"

    jti: Mapped[str] = mapped_column(TEXT, primary_key=True)
    site_id: Mapped[UUID] = mapped_column(
        ForeignKey("assess_sites.id", ondelete="CASCADE")
    )
    student_userid: Mapped[int] = mapped_column(BIGINT)
    cmid: Mapped[int] = mapped_column(BIGINT)
    courseid_hint: Mapped[int | None] = mapped_column(BIGINT)
    mint_key_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    minted_at: Mapped[datetime] = _ts(default=True)
    expires_at: Mapped[datetime] = _ts()
    consumed_at: Mapped[datetime | None] = _ts(nullable=True)


class OutageIncidentOrm(Base):
    __tablename__ = "assess_outage_incidents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("assess_sessions.id", ondelete="CASCADE")
    )
    started_at: Mapped[datetime] = _ts(default=True)
    ended_at: Mapped[datetime | None] = _ts(nullable=True)
    reason_code: Mapped[str | None] = mapped_column(TEXT)
    credited_s: Mapped[int] = mapped_column(
        INTEGER, server_default=text("0")
    )  # revision 0026


class EnrolmentCheckOrm(Base):
    __tablename__ = "assess_enrolment_checks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("assess_sessions.id", ondelete="CASCADE")
    )
    checked_at: Mapped[datetime] = _ts(default=True)
    result: Mapped[str] = mapped_column(TEXT)


class SnapshotOrm(Base):
    __tablename__ = "assess_snapshots"

    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("assess_sessions.id", ondelete="CASCADE"), primary_key=True
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    content_hash: Mapped[str] = mapped_column(TEXT)
    created_at: Mapped[datetime] = _ts(default=True)


class TurnOrm(Base):
    __tablename__ = "assess_turns"

    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("assess_sessions.id", ondelete="CASCADE"), primary_key=True
    )
    turn_no: Mapped[int] = mapped_column(INTEGER, primary_key=True)
    role: Mapped[str] = mapped_column(TEXT)
    kind: Mapped[str] = mapped_column(TEXT)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    turn_hash: Mapped[str] = mapped_column(TEXT)
    prev_hash: Mapped[str] = mapped_column(TEXT)
    marker_flags: Mapped[int] = mapped_column(INTEGER, server_default=text("0"))
    created_at: Mapped[datetime] = _ts(default=True)


class SessionKeyOrm(Base):
    __tablename__ = "assess_session_keys"

    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("assess_sessions.id", ondelete="CASCADE"), primary_key=True
    )
    kek_wrapped: Mapped[bytes | None] = mapped_column(LargeBinary)
    kek_kid: Mapped[str] = mapped_column(TEXT)
    key_version: Mapped[int] = mapped_column(INTEGER, server_default=text("1"))
    shredded_at: Mapped[datetime | None] = _ts(nullable=True)
    created_at: Mapped[datetime] = _ts(default=True)


class SessionHandoffOrm(Base):
    __tablename__ = "assess_session_handoff"

    session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    session_state: Mapped[str] = mapped_column(TEXT)
    epoch: Mapped[int] = mapped_column(INTEGER)
    sealed_dek: Mapped[bytes] = mapped_column(LargeBinary)
    handoff_kid: Mapped[str] = mapped_column(TEXT)
    created_at: Mapped[datetime] = _ts(default=True)


class TranscriptOrm(Base):
    __tablename__ = "assess_transcripts"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=_GEN_UUID
    )
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("assess_sessions.id"), unique=True
    )
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    dek_wrapped: Mapped[bytes | None] = mapped_column(LargeBinary)
    transcript_kid: Mapped[str] = mapped_column(TEXT)
    content_hash: Mapped[str] = mapped_column(TEXT)
    generated_at: Mapped[datetime] = _ts(default=True)
    shredded_at: Mapped[datetime | None] = _ts(nullable=True)
    # revision 0034 (plan transcript-delivery)
    turns_digest: Mapped[str | None] = mapped_column(TEXT)
    filename: Mapped[str | None] = mapped_column(TEXT)
    byte_size: Mapped[int | None] = mapped_column(INTEGER)
    # revision 0035: the course short name read at close (email subject)
    course_shortname: Mapped[str | None] = mapped_column(TEXT)


class DeliveryOrm(Base):
    """The delivery outbox (revision 0034): one row per (transcript, round, recipient).

    Revision 0035: a configured address is named by ``recipient_ref`` (``sha256(site|address)``),
    never stored; ``recipient_userid`` stays for the user-addressed kinds."""

    __tablename__ = "assess_deliveries"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=_GEN_UUID
    )
    transcript_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assess_transcripts.id")
    )
    kind: Mapped[str] = mapped_column(TEXT, server_default=text("'transcript'"))
    round: Mapped[int] = mapped_column(INTEGER, server_default=text("1"))
    recipient_kind: Mapped[str] = mapped_column(TEXT)
    recipient_userid: Mapped[int | None] = mapped_column(BIGINT)
    recipient_ref: Mapped[str | None] = mapped_column(TEXT)
    status: Mapped[str] = mapped_column(TEXT, server_default=text("'PENDING'"))
    attempts: Mapped[int] = mapped_column(INTEGER, server_default=text("0"))
    not_before: Mapped[datetime] = _ts(default=True)
    attempt_started_at: Mapped[datetime | None] = _ts(nullable=True)
    last_error_class: Mapped[str | None] = mapped_column(TEXT)
    smtp_code: Mapped[int | None] = mapped_column(INTEGER)
    message_id: Mapped[str | None] = mapped_column(TEXT)
    sent_at: Mapped[datetime | None] = _ts(nullable=True)
    created_at: Mapped[datetime] = _ts(default=True)


class TurnNoteOrm(Base):
    """Post-check marks and contest entries of a bot turn (revision 0034)."""

    __tablename__ = "assess_turn_notes"

    session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    turn_no: Mapped[int] = mapped_column(INTEGER, primary_key=True)
    marks: Mapped[list[str]] = mapped_column(ARRAY(TEXT), server_default=text("'{}'"))
    contests: Mapped[list[Any]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    lang: Mapped[str | None] = mapped_column(TEXT)


class JobOrm(Base):
    __tablename__ = "assess_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(TEXT)
    args: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    dedupe_key: Mapped[str | None] = mapped_column(TEXT)
    state: Mapped[str] = mapped_column(TEXT, server_default=text("'ready'"))
    not_before: Mapped[datetime] = _ts(default=True)
    lease_until: Mapped[datetime | None] = _ts(nullable=True)
    leased_by: Mapped[str | None] = mapped_column(TEXT)
    attempts: Mapped[int] = mapped_column(INTEGER, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(INTEGER, server_default=text("5"))
    created_at: Mapped[datetime] = _ts(default=True)
    done_at: Mapped[datetime | None] = _ts(nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(TEXT)


class RenderCheckOrm(Base):
    __tablename__ = "assess_render_checks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_id: Mapped[UUID] = mapped_column(ForeignKey("assess_sites.id"))
    student_userid: Mapped[int] = mapped_column(BIGINT)
    cmid: Mapped[int] = mapped_column(BIGINT)
    requested_at: Mapped[datetime] = _ts(default=True)
    done_at: Mapped[datetime | None] = _ts(nullable=True)


class RenderStateOrm(Base):
    __tablename__ = "assess_render_state"

    site_id: Mapped[UUID] = mapped_column(
        ForeignKey("assess_sites.id"), primary_key=True
    )
    student_userid: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    cmid: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    status: Mapped[str] = mapped_column(TEXT)
    reason_code: Mapped[str | None] = mapped_column(TEXT)
    computed_at: Mapped[datetime] = _ts(default=True)


class GateInputOrm(Base):
    __tablename__ = "assess_gate_inputs"

    site_id: Mapped[UUID] = mapped_column(
        ForeignKey("assess_sites.id"), primary_key=True
    )
    student_userid: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    cmid: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    inputs: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    refreshed_at: Mapped[datetime] = _ts(default=True)


class ConsumedJtiOrm(Base):
    __tablename__ = "assess_consumed_jtis"

    jti_hash: Mapped[str] = mapped_column(TEXT, primary_key=True)
    aud: Mapped[str] = mapped_column(TEXT)
    expires_at: Mapped[datetime] = _ts()
    consumed_at: Mapped[datetime] = _ts(default=True)


class TranscriptDownloadTokenOrm(Base):
    __tablename__ = "assess_transcript_download_tokens"

    jti_hash: Mapped[str] = mapped_column(TEXT, primary_key=True)
    transcript_id: Mapped[UUID] = mapped_column(ForeignKey("assess_transcripts.id"))
    subject_userid: Mapped[int] = mapped_column(BIGINT)
    expires_at: Mapped[datetime] = _ts()
    consumed_at: Mapped[datetime | None] = _ts(nullable=True)
    audience_role: Mapped[str] = mapped_column(TEXT)  # revision 0034: owner | grader
    site_id: Mapped[UUID | None] = mapped_column(ForeignKey("assess_sites.id"))


class AuditEventOrm(Base):
    __tablename__ = "assess_audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    session_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    event: Mapped[str] = mapped_column(TEXT)
    reason_code: Mapped[str | None] = mapped_column(TEXT)
    actor_kind: Mapped[str] = mapped_column(TEXT)
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = _ts(default=True)


class ErasureRecordOrm(Base):
    __tablename__ = "assess_erasure_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_id: Mapped[UUID] = mapped_column(ForeignKey("assess_sites.id"))
    subject_userid_hash: Mapped[str] = mapped_column(TEXT)
    requested_at: Mapped[datetime] = _ts(default=True)
    completed_at: Mapped[datetime | None] = _ts(nullable=True)
    rows_deleted: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )


class HeartbeatOrm(Base):
    __tablename__ = "assess_heartbeat"

    component: Mapped[str] = mapped_column(TEXT, primary_key=True)
    beat_at: Mapped[datetime] = _ts(default=True)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )


class SetupCheckReportOrm(Base):
    __tablename__ = "assess_setup_check_reports"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=_GEN_UUID
    )
    site_id: Mapped[UUID] = mapped_column(
        ForeignKey("assess_sites.id", ondelete="CASCADE")
    )
    requested_by_key_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(TEXT, server_default=text("'queued'"))
    moodle_release: Mapped[str | None] = mapped_column(TEXT)
    items: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = _ts(default=True)
    finished_at: Mapped[datetime | None] = _ts(nullable=True)


class ModelActivationOrm(Base):
    """NFR-023 activation record (revision 0021): append-only, one row per accepted report."""

    __tablename__ = "assess_model_activations"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(TEXT)
    model: Mapped[str] = mapped_column(TEXT)
    prompt_version: Mapped[str] = mapped_column(VARCHAR(8))
    report_sha256: Mapped[str] = mapped_column(CHAR(64))
    hard_pass: Mapped[bool] = mapped_column(BOOLEAN)
    activated_at: Mapped[datetime] = _ts(default=True)
    activated_by_key_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
