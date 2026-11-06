"""Assess error codes (re-exports from vektra_shared.errors only).

Codes are defined in ``vektra_shared.errors`` (the single registry, REQ-011); this module
only re-exports them and builds the assess envelopes. Owning plan:
.s2s/plans/20261101-154950-assess-foundations.
"""

from __future__ import annotations

from vektra_shared.errors import (
    ERR_ASSESS_001,
    ERR_ASSESS_002,
    ERR_ASSESS_003,
    ERR_ASSESS_004,
    ErrorCategory,
    ErrorResponse,
)

__all__ = [
    "ERR_ASSESS_001",
    "ERR_ASSESS_002",
    "ERR_ASSESS_003",
    "ERR_ASSESS_004",
    "activation_refused",
    "assess_unavailable",
    "setup_check_not_found",
    "setup_check_too_soon",
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
