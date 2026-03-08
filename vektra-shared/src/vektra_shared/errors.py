"""Error types and code registry for the Vektra platform.

REQ-010: ErrorResponse envelope - all API errors use {"error": <ErrorResponse>}.
REQ-011: Phase 1 normative error code registry (13 codes).
BR-001: Four-category error taxonomy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

# ---------------------------------------------------------------------------
# Error category (BR-001)
# ---------------------------------------------------------------------------


class ErrorCategory:
    """Four-category error taxonomy (BR-001)."""

    TRANSIENT = "TRANSIENT"  # HTTP 503 - retry may succeed
    PERMANENT = "PERMANENT"  # HTTP 400/422 - request needs modification
    CONFIGURATION = "CONFIGURATION"  # HTTP 500 - system misconfiguration
    UPSTREAM = "UPSTREAM"  # HTTP 502 - external dependency failure


# ---------------------------------------------------------------------------
# Error envelope (REQ-010)
# ---------------------------------------------------------------------------


@dataclass
class ErrorResponse:
    """API error response envelope. Serialized as {"error": <this object>}.

    REQ-010: every error response uses this exact shape.
    remediation is never empty (REQ-009).
    """

    category: str  # ErrorCategory constant
    code: str  # ERR-{COMPONENT}-{NUMBER}
    message: str  # diagnostic: what happened
    remediation: str  # prescriptive: what to do next
    request_id: UUID = field(default_factory=uuid4)
    retry_after: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_envelope(self) -> dict[str, Any]:
        """Serialize to the {"error": {...}} envelope shape."""
        data: dict[str, Any] = {
            "category": self.category,
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
            "request_id": str(self.request_id),
            "details": self.details,
        }
        if self.retry_after is not None:
            data["retry_after"] = self.retry_after
        return {"error": data}


# ---------------------------------------------------------------------------
# Normative error codes (REQ-011)
# HTTP status mapping:
#   TRANSIENT    -> 503
#   PERMANENT    -> 400 / 422
#   CONFIGURATION -> 500
#   UPSTREAM     -> 502
#   ERR-AUTH-001 -> 401
#   ERR-AUTH-003 -> 403
# ---------------------------------------------------------------------------

# Ingest errors
ERR_INGEST_001 = (
    "ERR-INGEST-001"  # Invalid file (unsupported type, corrupt, unrecognized MIME)
)
ERR_INGEST_002 = "ERR-INGEST-002"  # File too large
ERR_INGEST_003 = "ERR-INGEST-003"  # Scanned PDF detected (no text layer)
ERR_INGEST_004 = "ERR-INGEST-004"  # Vector store write failed

# Query errors
ERR_QUERY_001 = "ERR-QUERY-001"  # No documents indexed in namespace
ERR_QUERY_002 = "ERR-QUERY-002"  # LLM unavailable (both primary and fallback failed)
ERR_QUERY_003 = "ERR-QUERY-003"  # Query too long (exceeds token limit)
ERR_QUERY_004 = "ERR-QUERY-004"  # Vector store read failed

# Configuration errors
ERR_CONFIG_001 = "ERR-CONFIG-001"  # Missing required configuration variable
ERR_CONFIG_002 = "ERR-CONFIG-002"  # Invalid provider configuration

# Auth errors (REQ-041)
ERR_AUTH_001 = (
    "ERR-AUTH-001"  # Invalid token (missing, malformed, unrecognized, revoked)
)
ERR_AUTH_002 = "ERR-AUTH-002"  # Expired token (Phase 2)
ERR_AUTH_003 = "ERR-AUTH-003"  # Insufficient scope
ERR_AUTH_004 = "ERR-AUTH-004"  # Rate limit exceeded

# Quota errors (ARCH-047)
ERR_QUOTA_001 = "ERR-QUOTA-001"  # Namespace quota exceeded

# Analytics errors (ARCH-041)
ERR_ANALYTICS_001 = "ERR-ANALYTICS-001"  # Analytics service not available
ERR_ANALYTICS_002 = "ERR-ANALYTICS-002"  # Trace not found


# ---------------------------------------------------------------------------
# HTTP status mapping helpers
# ---------------------------------------------------------------------------

_CATEGORY_STATUS: dict[str, int] = {
    ErrorCategory.TRANSIENT: 503,
    ErrorCategory.PERMANENT: 422,
    ErrorCategory.CONFIGURATION: 500,
    ErrorCategory.UPSTREAM: 502,
}

_CODE_STATUS_OVERRIDE: dict[str, int] = {
    ERR_AUTH_001: 401,
    ERR_AUTH_002: 401,
    ERR_AUTH_003: 403,
    ERR_AUTH_004: 429,
    ERR_QUOTA_001: 422,
    ERR_INGEST_002: 413,  # Payload Too Large
    ERR_QUERY_003: 422,
    ERR_ANALYTICS_002: 404,
}


def http_status_for(error: ErrorResponse) -> int:
    """Return the appropriate HTTP status code for an ErrorResponse."""
    if error.code in _CODE_STATUS_OVERRIDE:
        return _CODE_STATUS_OVERRIDE[error.code]
    return _CATEGORY_STATUS.get(error.category, 500)


# ---------------------------------------------------------------------------
# Pre-built error factories for common cases
# ---------------------------------------------------------------------------


def auth_invalid_token(request_id: UUID | None = None) -> ErrorResponse:
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_AUTH_001,
        message="Authentication token is missing, malformed, unrecognized, or revoked.",
        remediation=(
            "Include a valid API key as a Bearer token in the Authorization header. "
            "Create a new API key via POST /api/v1/admin/api-keys if the key was revoked."
        ),
        request_id=request_id or uuid4(),
    )


def auth_insufficient_scope(
    required_scope: str, request_id: UUID | None = None
) -> ErrorResponse:
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_AUTH_003,
        message=f"The provided API key does not have the required scope: '{required_scope}'.",
        remediation=(
            f"Use an API key with the '{required_scope}' scope, or request a new key "
            "with the appropriate scope from your administrator."
        ),
        request_id=request_id or uuid4(),
    )
