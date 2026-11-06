"""Assess error codes (re-exports from vektra_shared.errors only).

Codes are defined in ``vektra_shared.errors`` (the single registry, REQ-011); this module
only re-exports them and builds the assess envelopes. Owning plans:
.s2s/plans/20261101-154950-assess-foundations (001-004) and
.s2s/plans/20261101-154950-assess-session-lifecycle (005-016).

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
    "activation_refused",
    "assess_unavailable",
    "bad_request",
    "capacity_reached",
    "deadline_passed",
    "key_not_bound",
    "loginas_refused",
    "plugin_too_old",
    "rate_limited",
    "reply_pending",
    "session_closed",
    "session_not_found",
    "setup_check_not_found",
    "setup_check_too_soon",
    "start_refused",
    "state_conflict",
    "token_invalid",
]


def assess_unavailable(failing_item: str) -> ErrorResponse:
    """503 envelope naming the readiness item that failed (never a secret or a row)."""
    return ErrorResponse(
        category=ErrorCategory.TRANSIENT,
        code=ERR_ASSESS_001,
        message="The assessment discussion service is unavailable.",
        remediation=f"Readiness item failing: {failing_item}. See GET /health/assess.",
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


def key_not_bound() -> ErrorResponse:
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_ASSESS_007,
        message="This API key is not bound to an assess site for this purpose.",
        remediation="Use the site's own assess:mint key (assess_site_keys, purpose 'mint').",
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
