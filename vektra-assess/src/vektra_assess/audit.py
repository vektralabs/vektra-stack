"""Lifecycle audit events (REQ-085): the event enum and the in-transaction writer.

Every event is written on the SAME connection and transaction as the state change it describes,
through ``write_event``. If the insert fails, the exception propagates and the caller's
transaction rolls back, so the action does not happen without its audit row (fail closed),
unlike ``vektra_admin.audit.log_event``, which swallows errors.

``created_at`` is the wall-clock time of the write (``clock_timestamp()``), not the start of the
transaction (``now()``): latencies measured between two events (the burst's Start-to-commit) are
real elapsed time.

Rows are content-free by construction: ``data`` accepts only short strings, integers, booleans
and ``None`` (ids, counts, timestamps as epoch seconds, reason codes). A value that is not one of
those, or a string longer than ``MAX_VALUE_CHARS``, raises before anything is written: a caller
cannot slip a student's text or a value read from the LMS into the audit table by mistake.

Plan assess-session-lifecycle owns the events below (M5.6 completes the set and its tests).
"""

from __future__ import annotations

import enum
import json
from collections.abc import Mapping
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy import text

MAX_VALUE_CHARS = 64
Actor = Literal["api", "worker"]


class AssessAuditEvent(enum.StrEnum):
    TOKEN_MINT = "assess_token_mint"
    START = "assess_start"
    START_ROLLBACK = "assess_start_rollback"
    START_RATE_CAP_HIT = "assess_start_rate_cap_hit"
    ROLLBACK_CAP_HIT = "assess_rollback_cap_hit"
    COMMIT = "assess_commit"
    OUTAGE_START = "assess_outage_start"
    OUTAGE_END = "assess_outage_end"
    CLOSE = "assess_close"
    IN_SESSION_GATE_REJECTED = "assess_in_session_gate_rejected"
    LOGINAS_REJECTED = "assess_loginas_rejected"
    OWNERSHIP_MISMATCH_REJECTED = "assess_ownership_mismatch_rejected"
    ATTEMPT_MISMATCH_REJECTED = "assess_attempt_mismatch_rejected"
    TEAM_SUBMISSION_REFUSED = "assess_team_submission_refused"
    BUDGET_WARNING = "assess_budget_warning"
    FLAG_ADDED = "assess_flag_added"
    REPLY_RECONCILED = (
        "assess_reply_reconciled"  # a lost reply answered with the marked turn
    )
    START_BREAKER_OPEN = "assess_start_breaker_open"  # plan 8 M5.4, per-site breaker
    START_BREAKER_CLOSE = "assess_start_breaker_close"
    START_REFUSED = (
        "assess_start_refused"  # advisory refusal at the API, nothing consumed
    )
    # plan assess-transcript-delivery
    TRANSCRIPT_GENERATED = "assess_transcript_generated"
    TRANSCRIPT_INTEGRITY_FAILED = "assess_transcript_integrity_failed"
    TRANSCRIPT_RECIPIENTS_UNRESOLVED = "assess_transcript_recipients_unresolved"
    DOWNLOAD_TOKEN_MINT = "assess_download_token_mint"
    STUDENT_DOWNLOAD = "assess_student_download"
    TEACHER_DOWNLOAD = "assess_teacher_download"
    TRANSCRIPT_RESEND = (
        "assess_transcript_resend"  # written by assess_enqueue_resend (0034)
    )
    # plan assess-transcript-delivery 10b: every attempt and result per recipient (REQ-085),
    # recipient changes old/new (REQ-082), the OQ-029 crash-window resend. Refs, never addresses.
    DELIVERY_ATTEMPT = "assess_delivery_attempt"
    DELIVERY_SENT = "assess_delivery_sent"
    DELIVERY_FAILED = "assess_delivery_failed"
    DELIVERY_POSSIBLE_DUPLICATE = "assess_delivery_possible_duplicate"
    DELIVERY_RECIPIENT_REPLACED = "assess_delivery_recipient_replaced"
    DELIVERY_RECIPIENT_ADDED = "assess_delivery_recipient_added"
    # teacher_notify / operator_notify (revision 0039): the same per-recipient record for the
    # notification emails, keyed by the notification's job id. Refs and codes, never addresses.
    NOTIFY_ATTEMPT = "assess_notify_attempt"
    NOTIFY_SENT = "assess_notify_sent"
    NOTIFY_FAILED = "assess_notify_failed"
    NOTIFY_POSSIBLE_DUPLICATE = "assess_notify_possible_duplicate"
    # plan assess-teacher-ops: a teacher refused by the fresh role check (REQ-076, INT-001), and
    # a configuration change old/new (REQ-077, REQ-085). Sealed fields are audited by the
    # database trigger of revision 0036 under the same event name, with ``set|replaced|cleared``
    # and recipient refs, never content.
    TEACHER_UNVERIFIED = "assess_teacher_unverified"
    TEACHER_CONFIG_CHANGED = "assess_teacher_config_changed"
    # plan assess-teacher-ops 11b (REQ-085 teacher events): ids, counts and codes only. The
    # teacher's reason is reachable through ``reset_id`` / ``termination_id``, never copied here.
    TEACHER_RESET = "assess_teacher_reset"
    TEACHER_TERMINATE = "assess_teacher_terminate"
    TEACHER_EXTRA_TIME = "assess_teacher_extra_time"
    TEACHER_OPENING_ATTESTED = "assess_teacher_opening_attested"
    # plan assess-privacy-retention (REQ-085 purge, erasure and privacy events): ids, counts and
    # codes only. A failed purge or erasure rolls its transaction back, so its alert is a log
    # line, never an audit row that would claim a deletion.
    PRIVACY_CONFIG_CHANGED = "assess_privacy_config_changed"
    PURGE = "assess_purge"
    DELIVERY_ABANDONED_ON_PURGE = "assess_delivery_abandoned_on_purge"
    ERASURE = "assess_erasure"
    ERASURE_RETAINED = "assess_erasure_retained"
    PRIVACY_EXPORT = "assess_privacy_export"
    PRIVACY_SUBJECTS_LISTED = "assess_privacy_subjects_listed"
    # plan 12b: the operator's SMTP test send (requested by the API, its outcome by the worker)
    SMTP_TEST_REQUESTED = "assess_smtp_test_requested"
    SMTP_TEST_RESULT = "assess_smtp_test_result"


class Conn(Protocol):
    async def execute(self, statement: Any, parameters: Any = ..., /) -> Any: ...


_INSERT = text(
    "INSERT INTO assess_audit_events (site_id, session_id, event, reason_code, actor_kind, data, "
    "created_at) VALUES (:site, :session, :event, :reason, :actor, CAST(:data AS jsonb), "
    "clock_timestamp())"
)


def _clean(data: Mapping[str, Any] | None) -> str:
    out: dict[str, Any] = {}
    for key, value in (data or {}).items():
        if not isinstance(key, str) or len(key) > MAX_VALUE_CHARS:
            raise ValueError("audit data keys are short strings")
        if isinstance(value, UUID):
            value = str(value)
        if value is not None and not isinstance(value, (str, int, bool)):
            raise ValueError(f"audit data {key!r} must be an id, a count or a code")
        if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
            raise ValueError(f"audit data {key!r} is too long to be an id or a code")
        out[key] = value
    return json.dumps(out, sort_keys=True)


async def write_event(
    conn: Conn,
    event: AssessAuditEvent,
    *,
    actor: Actor,
    site_id: UUID | None = None,
    session_id: UUID | None = None,
    reason_code: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> None:
    """Insert one audit row in the caller's transaction; any failure raises (fail closed)."""
    if reason_code is not None and len(reason_code) > MAX_VALUE_CHARS:
        raise ValueError("reason_code is a short code")
    await conn.execute(
        _INSERT,
        {
            "site": site_id,
            "session": session_id,
            "event": AssessAuditEvent(event).value,
            "reason": reason_code,
            "actor": actor,
            "data": _clean(data),
        },
    )
