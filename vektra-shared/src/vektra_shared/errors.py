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
# Domain exceptions
# ---------------------------------------------------------------------------


class ActiveIndexVersionError(Exception):
    """Raised when something tries to delete the index version being served.

    The guard behind REQ-064's cleanup step. A vector store refuses this from
    inside delete_index_version() rather than trusting callers to check first,
    because it is the only component that knows which version it reads.
    """

    def __init__(self, namespace: str, index_version: int) -> None:
        self.namespace = namespace
        self.index_version = index_version
        super().__init__(
            f"Refusing to delete index version {index_version} of namespace "
            f"'{namespace}': it is the version currently being served. Switch "
            "VEKTRA_ACTIVE_INDEX_VERSION to the new version and restart before "
            "cleaning up the old one."
        )


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
ERR_INGEST_005 = "ERR-INGEST-005"  # Invalid per-document metadata (FEAT-026)

# Query errors
ERR_QUERY_001 = "ERR-QUERY-001"  # No documents indexed in namespace
ERR_QUERY_002 = "ERR-QUERY-002"  # LLM unavailable (both primary and fallback failed)
ERR_QUERY_003 = "ERR-QUERY-003"  # Query too long (exceeds token limit)
ERR_QUERY_004 = "ERR-QUERY-004"  # Vector store read failed
ERR_QUERY_005 = "ERR-QUERY-005"  # Query pipeline provider not registered/available

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

# Namespace errors
ERR_NAMESPACE_001 = "ERR-NAMESPACE-001"  # Reserved namespace: no content may be written

# Analytics errors (ARCH-041)
ERR_ANALYTICS_001 = "ERR-ANALYTICS-001"  # Analytics service not available
ERR_ANALYTICS_002 = "ERR-ANALYTICS-002"  # Trace not found

# Conversation errors (core + admin conversation surface, distinct from the
# learn vertical's ERR-LEARN-005/006 which are scoped to the widget's own path)
ERR_CONV_001 = "ERR-CONV-001"  # Conversation not found
ERR_CONV_002 = "ERR-CONV-002"  # Persistent conversation store not available
ERR_CONV_003 = "ERR-CONV-003"  # Configured store cannot return decrypted turns

# Learn errors (e-learning vertical)
ERR_LEARN_001 = "ERR-LEARN-001"  # Learn service not available
ERR_LEARN_002 = "ERR-LEARN-002"  # Enrollment not found
ERR_LEARN_003 = "ERR-LEARN-003"  # Invalid or expired dashboard token
ERR_LEARN_004 = "ERR-LEARN-004"  # Duplicate enrollment
ERR_LEARN_005 = "ERR-LEARN-005"  # Conversation not found (WI-1)
ERR_LEARN_006 = "ERR-LEARN-006"  # Conversation belongs to another course (WI-1)
ERR_LEARN_007 = "ERR-LEARN-007"  # Conversation belongs to another student (FEAT-027)
ERR_LEARN_008 = "ERR-LEARN-008"  # Conversation ownership could not be verified

# Index errors (ARCH-045, index versioning)
ERR_INDEX_001 = "ERR-INDEX-001"  # Refused: that index version is the one being served
ERR_INDEX_002 = "ERR-INDEX-002"  # Invalid index version (< 1)
ERR_INDEX_003 = "ERR-INDEX-003"  # Reindex target equals the active version
ERR_INDEX_004 = "ERR-INDEX-004"  # Reindex job not found

# Assess errors (vektra_assess). Namespace rule: each plan takes the next free
# number, codes are never reused (docs/reference/error-codes.md).
ERR_ASSESS_001 = "ERR-ASSESS-001"  # Assess unavailable: a readiness item failed
ERR_ASSESS_002 = (
    "ERR-ASSESS-002"  # Setup check requested too soon (1 per 5 min per site)
)
ERR_ASSESS_003 = (
    "ERR-ASSESS-003"  # Site or setup-check report not found (also cross-site)
)
ERR_ASSESS_004 = (
    "ERR-ASSESS-004"  # Model activation refused: the eval report does not pass NFR-023
)
# Student session lifecycle (plan assess-session-lifecycle)
ERR_ASSESS_005 = (
    "ERR-ASSESS-005"  # Discussion token missing, invalid, expired or revoked
)
ERR_ASSESS_006 = "ERR-ASSESS-006"  # Refused: an LMS 'login as' session (REQ-084)
ERR_ASSESS_007 = "ERR-ASSESS-007"  # Key not bound to a site for this purpose
ERR_ASSESS_008 = "ERR-ASSESS-008"  # Session not found (unknown, or not the caller's)
ERR_ASSESS_009 = "ERR-ASSESS-009"  # Session closed: no action, no re-mint
ERR_ASSESS_010 = "ERR-ASSESS-010"  # A reply is still pending: wait for it
ERR_ASSESS_011 = "ERR-ASSESS-011"  # Session state does not allow this action
ERR_ASSESS_012 = "ERR-ASSESS-012"  # Too many requests (rate limit or Start cap)
ERR_ASSESS_013 = "ERR-ASSESS-013"  # Capacity reached: no Start admitted now
ERR_ASSESS_014 = "ERR-ASSESS-014"  # Malformed or over-length request body
ERR_ASSESS_015 = "ERR-ASSESS-015"  # Start refused: the discussion is not available
ERR_ASSESS_016 = "ERR-ASSESS-016"  # Plugin version below the required minimum
# Transcripts (plan assess-transcript-delivery)
ERR_ASSESS_017 = (
    "ERR-ASSESS-017"  # Transcript not found (unknown, other site, not generated)
)
ERR_ASSESS_018 = (
    "ERR-ASSESS-018"  # Download token missing, invalid, expired or already used
)
ERR_ASSESS_019 = (
    "ERR-ASSESS-019"  # Resend refused: the request names a recipient (REQ-082)
)
ERR_ASSESS_020 = (
    "ERR-ASSESS-020"  # Stored transcript fails its integrity check: not served
)
ERR_ASSESS_021 = "ERR-ASSESS-021"  # Teacher not verified as a grader/editor of the activity (fresh check)
ERR_ASSESS_022 = "ERR-ASSESS-022"  # Teacher configuration refused: field-level errors (BR-013, REQ-077)
ERR_ASSESS_023 = "ERR-ASSESS-023"  # Teacher configuration changed meanwhile: expected_version is stale
ERR_ASSESS_024 = "ERR-ASSESS-024"  # Teacher reset refused: session not CLOSED or a START gate refuses (REQ-087)
ERR_ASSESS_025 = "ERR-ASSESS-025"  # Teacher terminate refused: expected_active_count is not the current count (REQ-081)
ERR_ASSESS_026 = (
    "ERR-ASSESS-026"  # Transcript purged: its retention period expired (REQ-088)
)
ERR_ASSESS_027 = "ERR-ASSESS-027"  # Subject erasure not completed yet or failed its count check: retry (OQ-033)
ERR_ASSESS_028 = (
    "ERR-ASSESS-028"  # Privacy configuration refused: field-level errors (REQ-089)
)

# Admin errors (ERR-ADMIN-001..007 live in vektra-admin as string literals;
# 008 is shared because it backs a factory reused within that module)
ERR_ADMIN_008 = "ERR-ADMIN-008"  # Deep-health auth: registry/key store not yet ready


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
    ERR_NAMESPACE_001: 403,
    ERR_INGEST_002: 413,  # Payload Too Large
    ERR_QUERY_003: 422,
    ERR_ANALYTICS_002: 404,
    ERR_LEARN_002: 404,
    ERR_LEARN_003: 401,
    ERR_LEARN_004: 409,
    ERR_LEARN_005: 404,
    ERR_LEARN_006: 403,
    ERR_LEARN_007: 403,
    ERR_CONV_001: 404,
    ERR_CONV_003: 501,  # Not Implemented: this store cannot decrypt turns
    ERR_INDEX_001: 409,  # Conflict: the version is live, deleting it is refused
    ERR_INDEX_002: 400,
    ERR_INDEX_003: 400,  # Bad Request: target must differ from the active version
    ERR_INDEX_004: 404,
    ERR_ASSESS_001: 503,
    ERR_ASSESS_002: 429,
    ERR_ASSESS_003: 404,
    ERR_ASSESS_004: 422,
    ERR_ASSESS_005: 401,
    ERR_ASSESS_006: 403,
    ERR_ASSESS_007: 403,
    ERR_ASSESS_008: 404,
    ERR_ASSESS_009: 409,
    ERR_ASSESS_010: 409,
    ERR_ASSESS_011: 409,
    ERR_ASSESS_012: 429,
    ERR_ASSESS_013: 503,
    ERR_ASSESS_014: 400,  # explicit envelope, never the 422 validation path (DEBT-035)
    ERR_ASSESS_015: 409,
    ERR_ASSESS_016: 403,
    ERR_ASSESS_017: 404,
    ERR_ASSESS_018: 401,
    ERR_ASSESS_019: 400,  # explicit envelope, never the 422 validation path (DEBT-035)
    ERR_ASSESS_020: 500,
    ERR_ASSESS_021: 403,
    ERR_ASSESS_022: 400,  # explicit envelope with details.fields, never the 422 path (DEBT-035)
    ERR_ASSESS_023: 409,
    ERR_ASSESS_024: 409,
    ERR_ASSESS_025: 409,
    ERR_ASSESS_026: 410,
    ERR_ASSESS_027: 503,
    ERR_ASSESS_028: 400,  # explicit envelope with details.fields, never the 422 path (DEBT-035)
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


def provider_registry_unavailable(request_id: UUID | None = None) -> ErrorResponse:
    """500 for a request that reached a handler before the app finished wiring.

    Reuses ERR-CONFIG-001, the same code the global unhandled-exception handler
    assigns to internal failures: from a client's point of view a missing
    provider registry is an internal setup fault, not something it can fix.
    """
    return ErrorResponse(
        category=ErrorCategory.CONFIGURATION,
        code=ERR_CONFIG_001,
        message="The provider registry is not initialized.",
        remediation=(
            "This indicates the service did not start up correctly. Check the "
            "server logs and restart the service."
        ),
        request_id=request_id or uuid4(),
    )


def conversation_not_found(
    conversation_id: object, request_id: UUID | None = None
) -> ErrorResponse:
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_CONV_001,
        message=f"Conversation '{conversation_id}' not found.",
        remediation="Verify the conversation id; it may have been deleted.",
        request_id=request_id or uuid4(),
    )


def conversation_store_unavailable(request_id: UUID | None = None) -> ErrorResponse:
    return ErrorResponse(
        category=ErrorCategory.TRANSIENT,
        code=ERR_CONV_002,
        message="Persistent conversation storage is not available.",
        remediation=(
            "The service may be starting up, or no persistent conversation store "
            "is configured (set VEKTRA_CONVERSATION_KEY). Retry shortly."
        ),
        request_id=request_id or uuid4(),
    )


def conversation_turns_unsupported(request_id: UUID | None = None) -> ErrorResponse:
    """501 when the configured store cannot return decrypted turns.

    The in-memory store used in tests/dev has no decryption path; the request is
    well-formed but this deployment cannot serve it.
    """
    return ErrorResponse(
        category=ErrorCategory.CONFIGURATION,
        code=ERR_CONV_003,
        message="The configured conversation store cannot return decrypted turns.",
        remediation=(
            "Configure a persistent conversation store with VEKTRA_CONVERSATION_KEY "
            "set; the in-memory store cannot decrypt turn history."
        ),
        request_id=request_id or uuid4(),
    )


def query_pipeline_unavailable(request_id: UUID | None = None) -> ErrorResponse:
    """503 when the query pipeline provider is not registered yet."""
    return ErrorResponse(
        category=ErrorCategory.TRANSIENT,
        code=ERR_QUERY_005,
        message="The query pipeline is not available.",
        remediation=(
            "The service may be starting up, or no query pipeline is configured. "
            "Retry shortly."
        ),
        request_id=request_id or uuid4(),
    )


def key_store_unavailable(request_id: UUID | None = None) -> ErrorResponse:
    """500 when the API key store provider is not configured."""
    return ErrorResponse(
        category=ErrorCategory.CONFIGURATION,
        code=ERR_CONFIG_002,
        message="The API key store is not configured.",
        remediation=(
            "Verify the key store provider is registered. Check the server logs "
            "and restart the service."
        ),
        request_id=request_id or uuid4(),
    )


def service_initializing(request_id: UUID | None = None) -> ErrorResponse:
    """503 for a deep-health auth check that ran before the store was ready."""
    return ErrorResponse(
        category=ErrorCategory.TRANSIENT,
        code=ERR_ADMIN_008,
        message="The service is still initializing.",
        remediation="Retry shortly; the service is starting up.",
        request_id=request_id or uuid4(),
    )


def namespace_reserved(namespace: str) -> ErrorResponse:
    return ErrorResponse(
        category=ErrorCategory.PERMANENT,
        code=ERR_NAMESPACE_001,
        message=f"Namespace '{namespace}' is reserved and cannot hold content or be configured.",
        remediation="Use a different namespace; reserved namespaces are managed by Vektra.",
    )
