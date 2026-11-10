"""Assess error codes (re-exports from vektra_shared.errors only).

Codes are defined in ``vektra_shared.errors`` (the single registry, REQ-011); this module
only re-exports them and builds the assess envelopes. Owning plans:
.s2s/plans/20261101-154950-assess-foundations (001-004),
.s2s/plans/20261101-154950-assess-session-lifecycle (005-016) and
.s2s/plans/20261101-154950-assess-transcript-delivery (017-021) and
.s2s/plans/20261101-154950-assess-teacher-ops (022-025) and
.s2s/plans/20261101-154950-assess-privacy-retention (026-028).

Every message and remediation here is fixed text plus content-free codes: no envelope ever
carries a student's text, a Moodle value or a row of another student.
"""

from __future__ import annotations

from vektra_shared.errors import (
    ERR_ASSESS_001,
    ERR_ASSESS_002,
    ERR_ASSESS_003,
    ERR_ASSESS_004,
    ERR_ASSESS_005,
    ERR_ASSESS_006,
    ERR_ASSESS_007,
    ERR_ASSESS_008,
    ERR_ASSESS_009,
    ERR_ASSESS_010,
    ERR_ASSESS_011,
    ERR_ASSESS_012,
    ERR_ASSESS_013,
    ERR_ASSESS_014,
    ERR_ASSESS_015,
    ERR_ASSESS_016,
    ERR_ASSESS_017,
    ERR_ASSESS_018,
    ERR_ASSESS_019,
    ERR_ASSESS_020,
    ERR_ASSESS_021,
    ERR_ASSESS_022,
    ERR_ASSESS_023,
    ERR_ASSESS_024,
    ERR_ASSESS_025,
    ERR_ASSESS_026,
    ERR_ASSESS_027,
    ERR_ASSESS_028,
    ErrorCategory,
    ErrorResponse,
)

__all__ = [
    "ERR_ASSESS_001",
    "ERR_ASSESS_002",
    "ERR_ASSESS_003",
    "ERR_ASSESS_004",
    "ERR_ASSESS_005",
    "ERR_ASSESS_006",
    "ERR_ASSESS_007",
    "ERR_ASSESS_008",
    "ERR_ASSESS_009",
    "ERR_ASSESS_010",
    "ERR_ASSESS_011",
    "ERR_ASSESS_012",
    "ERR_ASSESS_013",
    "ERR_ASSESS_014",
    "ERR_ASSESS_015",
    "ERR_ASSESS_016",
    "ERR_ASSESS_017",
    "ERR_ASSESS_018",
    "ERR_ASSESS_019",
    "ERR_ASSESS_020",
    "ERR_ASSESS_021",
    "ERR_ASSESS_022",
    "ERR_ASSESS_023",
    "ERR_ASSESS_024",
    "ERR_ASSESS_025",
    "ERR_ASSESS_026",
    "ERR_ASSESS_027",
    "ERR_ASSESS_028",
    "activation_refused",
    "assess_unavailable",
    "bad_request",
    "capacity_reached",
    "config_conflict",
    "config_invalid",
    "deadline_passed",
    "download_token_invalid",
    "erasure_not_completed",
    "key_not_bound",
    "loginas_refused",
    "plugin_too_old",
    "privacy_config_invalid",
    "rate_limited",
    "reply_pending",
    "resend_recipient_forbidden",
    "session_closed",
    "session_not_found",
    "setup_check_not_found",
    "setup_check_too_soon",
    "start_refused",
    "state_conflict",
    "teacher_unverified",
    "token_invalid",
    "transcript_expired",
    "transcript_integrity",
    "transcript_not_found",
]


def assess_unavailable(failing_item: str) -> ErrorResponse:
    """503 envelope naming the readiness item that failed (never a secret or a row)."""
    return ErrorResponse(
        category=ErrorCategory.TRANSIENT,
        code=ERR_ASSESS_001,
        message="The assessment discussion service is unavailable.",
        remediation=f"Readiness item failing: {failing_item}. See GET /health/assess.",
    )


def start_breaker_open(retry_after_s: int) -> ErrorResponse:
    """503: the site's Start breaker is open (plan 8 M5.4); nothing was consumed."""
    wait = max(int(retry_after_s), 1)
    return ErrorResponse(
        category=ErrorCategory.TRANSIENT,
        code=ERR_ASSESS_001,
        message="Servizio momentaneamente non disponibile.",
        remediation=f"Retry after about {wait} seconds; the session was not used.",
        retry_after=wait,
        details={"reason": "start_breaker_open"},
    )


def setup_check_too_soon(retry_after_s: int) -> ErrorResponse:
    return ErrorResponse(
        category=ErrorCategory.TRANSIENT,
        code=ERR_ASSESS_002,
        message="A setup check for this site was requested too recently.",
        remediation=f"Wait about {max(retry_after_s, 1)} seconds; one check per site every 5 minutes.",
    )


def setup_check_not_found() -> ErrorResponse:
    """404 for an unknown site, an unknown report, and a site the key is not bound to: the
    three cases are indistinguishable on purpose (no cross-site probing)."""
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_003,
        message="Site or setup-check report not found.",
        remediation="Check the site and report ids, and that the key belongs to this site.",
    )


def activation_refused(reasons: tuple[str, ...]) -> ErrorResponse:
    """422: the report does not justify activating the triple (NFR-023). Codes only."""
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_004,
        message="Model activation refused: the eval report does not pass the NFR-023 gate.",
        remediation="Failing checks: "
        + ", ".join(reasons)
        + ". Rerun make eval-assess.",
    )


# --- student session lifecycle (plan assess-session-lifecycle) ------------------------------


def token_invalid() -> ErrorResponse:
    """401: one envelope for every token failure (signature, kid, aud, stage, expiry, replay,
    revoked epoch), so a caller learns nothing about which check refused it."""
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_005,
        message="The discussion token is missing, invalid or no longer valid.",
        remediation="Reload the page in Moodle to get a new token.",
    )


def loginas_refused() -> ErrorResponse:
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_006,
        message="A discussion cannot be started or opened from a 'login as' session.",
        remediation="Log in as yourself; the request must assert loginas=false and realuser_id=null.",
    )


def key_not_bound(purpose: str = "mint") -> ErrorResponse:
    """403: the key is not bound to an enabled site for ``purpose`` (``mint`` | ``teacher``);
    the remediation names the scope the route needs."""
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_007,
        message="This API key is not bound to an assess site for this purpose.",
        remediation=(
            f"Use the site's own assess:{purpose} key, bound to an enabled site "
            f"(assess_site_keys, purpose '{purpose}')."
        ),
    )


def session_not_found() -> ErrorResponse:
    """404 for unknown, rolled-back and not-owned sessions alike (no cross-student probing)."""
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_008,
        message="Discussion session not found.",
        remediation="Reload the page in Moodle; a new token names your own session.",
    )


def session_closed() -> ErrorResponse:
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_009,
        message="This discussion is closed.",
        remediation="Nothing more can be done in this session; the transcript is sent by email.",
    )


def reply_pending() -> ErrorResponse:
    return ErrorResponse(
        category=ErrorCategory.TRANSIENT,
        code=ERR_ASSESS_010,
        message="Attendi la risposta al messaggio precedente.",
        remediation="Wait for the reply in progress, then send the next message.",
    )


def state_conflict(state: str) -> ErrorResponse:
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_011,
        message="The discussion's state does not allow this action now.",
        remediation="Wait for the session to become active, or reload the page.",
        details={"state": state},
    )


def deadline_passed() -> ErrorResponse:
    """409: the session's time is over; input is locked (REQ-070)."""
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_011,
        message="Il tempo della discussione è scaduto.",
        remediation="No further messages can be sent in this session.",
        details={"reason": "deadline_passed"},
    )


def rate_limited(retry_after_s: int, reason: str = "rate_limit") -> ErrorResponse:
    wait = max(int(retry_after_s), 1)
    return ErrorResponse(
        category=ErrorCategory.TRANSIENT,
        code=ERR_ASSESS_012,
        message="Attendi qualche secondo.",
        remediation=f"Retry after about {wait} seconds.",
        retry_after=wait,
        details={"reason": reason},
    )


def capacity_reached() -> ErrorResponse:
    return ErrorResponse(
        category=ErrorCategory.TRANSIENT,
        code=ERR_ASSESS_013,
        message="Too many discussions are starting or running right now; nothing was consumed.",
        remediation="Try again in a few minutes.",
        details={"reason": "capacity"},
    )


def bad_request(reason: str) -> ErrorResponse:
    """400 with an explicit envelope (DEBT-035: never FastAPI's default 422 body)."""
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_014,
        message="The request body is malformed or too long.",
        remediation="Send the documented JSON body within the documented limits.",
        details={"reason": reason},
    )


def start_refused(reason: str) -> ErrorResponse:
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_015,
        message="The discussion cannot be started now.",
        remediation="Reload the page in Moodle to see why; nothing was consumed.",
        details={"reason": reason},
    )


def plugin_too_old() -> ErrorResponse:
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_016,
        message="The Moodle plugin version is below the minimum this service accepts.",
        remediation="Upgrade local_vektradiscuss on the Moodle site.",
    )


#: close reason -> the content-free message code the plugin renders (in Italian, client side).
#: ``closed_service_failure`` is REQ-086's "Sessione interrotta per un problema tecnico, non per
#: causa tua. Il docente è stato avvisato e può abilitarti una nuova sessione."
CLOSE_MESSAGE_CODES = {
    reason: f"closed_{reason}"
    for reason in (
        "finished",
        "timed_out",
        "service_failure",
        "budget_exhausted",
        "terminated_by_teacher",
        "access_revoked",
    )
}


def close_message_code(reason: str | None) -> str | None:
    return CLOSE_MESSAGE_CODES.get(reason) if reason else None


# --- transcripts (plan assess-transcript-delivery) -------------------------------------------


def transcript_not_found() -> ErrorResponse:
    """404 for an unknown transcript, one of another site, or one not generated yet."""
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_017,
        message="Transcript not found.",
        remediation="The transcript may still be generating: retry in a minute.",
    )


def download_token_invalid() -> ErrorResponse:
    """401: one envelope for every download-token failure (signature, audience, expiry, replay,
    wrong transcript), so a caller learns nothing about which check refused it."""
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_018,
        message="The download token is missing, invalid, expired or already used.",
        remediation="Request a new download from Moodle.",
    )


def resend_recipient_forbidden() -> ErrorResponse:
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_019,
        message="A resend cannot name a recipient.",
        remediation=(
            "Recipients are the course's graders, re-resolved from Moodle (REQ-082): send "
            "the request without any recipient field."
        ),
    )


def transcript_integrity() -> ErrorResponse:
    return ErrorResponse(
        category=ErrorCategory.CONFIGURATION,
        code=ERR_ASSESS_020,
        message="The stored transcript failed its integrity check and is not served.",
        remediation="The operator has been alerted; see the assess.transcript.alert log.",
    )


def teacher_unverified(*, stale: bool, role: str = "grader") -> ErrorResponse:
    """403. ``stale``: the worker-written role list was absent, too old or unreadable and a
    refresh WAS queued (retry); otherwise the fresh list does not name the teacher (no refresh,
    no point retrying). ``role``: ``grader`` (download, resend) or ``editor`` (configuration,
    REQ-077 "activity editors only")."""
    what = "an editor" if role == "editor" else "a grader"
    refused = what.replace(" ", "_")  # not_a_grader | not_an_editor
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_021,
        message=f"The teacher could not be verified as {what} of this activity.",
        remediation=(
            "A fresh check has been queued: retry in a few seconds."
            if stale
            else f"Moodle does not list this user as {what} of the activity's course; "
            "a retry will not succeed until that changes in Moodle."
        ),
        details={"reason": f"{role}_check_stale" if stale else f"not_{refused}"},
    )


# --- teacher configuration (plan assess-teacher-ops) ----------------------------------------


def config_invalid(fields: list[dict[str, object]]) -> ErrorResponse:
    """400 with every field-level error (BR-013, REQ-077). Each item is ``{field, code}`` plus
    the rule's bounds (``range``, ``allowed_domains``...): codes and limits only, never the
    submitted value."""
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_022,
        message="The discussion configuration was not saved: some fields are invalid.",
        remediation="Correct the fields listed in details.fields and save again.",
        details={"fields": fields},
    )


def config_conflict(current_version: int) -> ErrorResponse:
    """409: another save landed since this form was loaded (optimistic concurrency)."""
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_023,
        message="The discussion configuration was changed meanwhile.",
        remediation="Reload the settings page and save again.",
        details={"current_version": int(current_version)},
    )


# --- teacher operations (plan assess-teacher-ops 11b) ---------------------------------------


def reset_refused(reason: str, **details: object) -> ErrorResponse:
    """409 (REQ-087). ``reason``: ``not_closed``, ``not_latest``, ``already_reset``,
    ``gate_inputs_stale`` (a render check was queued: retry in a few seconds) or ``gate`` (with
    ``gate``: the START-gate code, and ``field`` when a teacher setting must change first, e.g.
    ``available_until`` after the end date). A session not in the activity is
    ``session_not_found`` (404), never this code."""
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_024,
        message="The new session was not granted.",
        remediation=(
            "Terminate an active session first; after the end date, move "
            "'Discussione disponibile fino al' and grant the reset again."
        ),
        details={"reason": reason, **details},
    )


def terminate_count_mismatch(current: int) -> ErrorResponse:
    """409 (REQ-081): the teacher confirmed another number of active sessions than there are
    now; nothing was changed."""
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_025,
        message="The number of active sessions changed since the confirmation.",
        remediation="Confirm again with the current number of active sessions.",
        details={"current": int(current)},
    )


# --- privacy and retention (plan assess-privacy-retention) -----------------------------------

#: REQ-088: the student-facing text of a purged transcript, shown as is by the plugin
TRANSCRIPT_EXPIRED_TEXT = (
    "Trascrizione non più disponibile (periodo di conservazione scaduto)"
)


def transcript_expired() -> ErrorResponse:
    """410 (REQ-088): the transcript existed and was purged at the end of its retention period;
    a tombstone says so, nothing of its content remains."""
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_026,
        message=TRANSCRIPT_EXPIRED_TEXT,
        remediation="The retention period of this transcript has expired: it cannot be restored.",
    )


def erasure_not_completed(reason: str, retry_after_s: int = 5) -> ErrorResponse:
    """503 (OQ-033): ``pending`` (the worker has not finished: the same request again is
    idempotent) or ``failed`` (the count check found rows left: the deletion was rolled back, though live sessions closed beforehand stay closed)."""
    return ErrorResponse(
        category=ErrorCategory.TRANSIENT,
        code=ERR_ASSESS_027,
        message="The erasure has not completed.",
        remediation="Retry the same request; if it keeps failing, contact the operator.",
        retry_after=retry_after_s,
        details={"reason": reason},
    )


def privacy_config_invalid(fields: list[dict[str, object]]) -> ErrorResponse:
    """400 (REQ-089): the privacy settings were refused, ``details.fields`` = ``{field, code}``
    per invalid field, never the value."""
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_028,
        message="The privacy settings were refused.",
        remediation="Correct the listed fields and save again.",
        details={"fields": fields},
    )
