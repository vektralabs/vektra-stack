"""Assess error codes (re-exports from vektra_shared.errors only).

Codes are defined in ``vektra_shared.errors`` (the single registry, REQ-011); this module
only re-exports them and builds the assess envelopes. Owning plan:
.s2s/plans/20261101-154950-assess-foundations.
"""

from __future__ import annotations

from vektra_shared.errors import ERR_ASSESS_001, ErrorCategory, ErrorResponse

__all__ = ["ERR_ASSESS_001", "assess_unavailable"]


def assess_unavailable(failing_item: str) -> ErrorResponse:
    """503 envelope naming the readiness item that failed (never a secret or a row)."""
    return ErrorResponse(
        category=ErrorCategory.TRANSIENT,
        code=ERR_ASSESS_001,
        message="The assessment discussion service is unavailable.",
        remediation=f"Readiness item failing: {failing_item}. See GET /health/assess.",
    )
