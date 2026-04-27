"""vektra-admin FastAPI router (REQ-004, REQ-006, REQ-019 to REQ-025).

Mounts at application root (/ prefix). Provides:
  - Health endpoints (two-tier model, REQ-025)
  - API key CRUD (REQ-020, REQ-023)
  - Admin dashboard redirect (GET /admin -> /admin/)
  - Prometheus metrics are mounted by infra-app-entrypoint (starlette-prometheus).

Bootstrap auth (REQ-021, REQ-036): POST /api-keys accepts the bootstrap env-var key
as authentication (once only) or a valid admin-scoped Bearer token.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import structlog
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from vektra_admin import audit as _audit
from vektra_admin import bootstrap as _bootstrap
from vektra_admin import health as _health
from vektra_admin.keys import generate_key
from vektra_shared.auth import ApiKeyInfo, KeyStoreProvider, require_scope
from vektra_shared.db import get_session
from vektra_shared.errors import (
    ErrorCategory,
    ErrorResponse,
    auth_invalid_token,
    http_status_for,
)

# ---------------------------------------------------------------------------
# Namespace config whitelist (WI-3)
# ---------------------------------------------------------------------------
# Tight whitelist: unknown keys are rejected (not silently ignored) so that
# typos from upstream clients (e.g. Moodle plugin) surface immediately. Each
# key is a public contract — extend cautiously.
#
# Validation model: every allowed key MUST be declared in exactly one of the
# two maps below.
#   - ALLOWED_CONFIG_VALUES[key]: set of allowed values (enum-style key)
#   - ALLOWED_CONFIG_TYPES[key]: required runtime type (type-validated key)
# ALLOWED_CONFIG_KEYS is derived (not hand-maintained) so a key cannot reach
# the whitelist without a corresponding validation rule. The disjoint check
# below fails at import if the same key is wired into both maps.
ALLOWED_CONFIG_VALUES: dict[str, set[str]] = {
    "grounding_mode": {"strict", "hybrid"},
}
ALLOWED_CONFIG_TYPES: dict[str, type] = {
    "show_sources": bool,
}
assert ALLOWED_CONFIG_VALUES.keys().isdisjoint(ALLOWED_CONFIG_TYPES.keys()), (
    "Each config key must appear in exactly one of "
    "ALLOWED_CONFIG_VALUES / ALLOWED_CONFIG_TYPES, never both."
)
ALLOWED_CONFIG_KEYS: set[str] = (
    ALLOWED_CONFIG_VALUES.keys() | ALLOWED_CONFIG_TYPES.keys()
)

log = structlog.get_logger(__name__)


def _resolve_request_id(request: Request) -> UUID:
    """Return ``request.state.request_id`` or synthesize a fallback ``uuid4()``.

    Always returns a UUID so audit logging never silently skips sensitive
    endpoints (NFR-007) when the request-id middleware misbehaves. Emits a
    structlog warning on the fallback path so misconfiguration is observable.
    """
    rid: UUID | None = getattr(request.state, "request_id", None)
    if rid is not None:
        return rid
    fallback = uuid4()
    log.warning("request_id_middleware_missing_fallback", fallback=str(fallback))
    return fallback


router = APIRouter()
_bearer = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------------------
# Helper: "any valid Bearer token" (no specific scope required)
# ---------------------------------------------------------------------------


async def _require_any_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> ApiKeyInfo:
    """Dependency: validates the Bearer token but does not enforce a scope."""
    if credentials is None:
        err = auth_invalid_token()
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())

    token = credentials.credentials
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        raise HTTPException(status_code=500, detail="ProviderRegistry not initialized")

    try:
        key_store: KeyStoreProvider = registry.get("key_store", "default")
    except ValueError:
        raise HTTPException(status_code=500, detail="Key store not configured")

    info = await key_store.lookup_by_token(token)
    if info is None:
        err = auth_invalid_token()
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())

    # Expose key_id for AuditMiddleware (same pattern as require_scope in auth.py)
    request.state.key_id = info.key_id

    return info


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class CreateKeyRequest(BaseModel):
    label: str | None = None
    scopes: list[str] | None = None  # defaults to ["admin"] if not provided
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _expires_at_must_be_timezone_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("expires_at must include timezone info")
        return v


class CreateKeyResponse(BaseModel):
    id: UUID
    key: str  # plaintext - returned ONCE
    key_preview: str
    label: str | None
    scopes: list[str]
    created_at: datetime


class KeyListItem(BaseModel):
    id: UUID
    key_preview: str
    label: str | None
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None
    revoked: bool
    expires_at: datetime | None = None


# ---------------------------------------------------------------------------
# Health endpoints (REQ-025)
# ---------------------------------------------------------------------------


@router.get("/health", response_model=None)
async def health(
    request: Request,
    detail: str | None = None,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Any:
    """Two-tier health check.

    - Unauthenticated (no ?detail): returns {status, timestamp} (shallow).
    - Authenticated (?detail=full): returns full component breakdown.

    HTTP 200 for healthy/degraded, 503 for unhealthy (ARCH-022).
    """
    registry = getattr(request.app.state, "registry", None)
    version = getattr(request.app.state, "version", "unknown")

    shallow, deep = await _health.check_all(registry, version)

    if detail == "full":
        # Require any valid Bearer token for deep view
        if credentials is None:
            err = auth_invalid_token()
            raise HTTPException(
                status_code=http_status_for(err), detail=err.to_envelope()
            )
        # Validate token; fail closed if registry/key_store unavailable
        if registry is None:
            raise HTTPException(status_code=503, detail="Service initializing")
        try:
            key_store = registry.get("key_store", "default")
            info = await key_store.lookup_by_token(credentials.credentials)
            if info is None:
                err = auth_invalid_token()
                raise HTTPException(
                    status_code=http_status_for(err), detail=err.to_envelope()
                )
            # Expose key_id for AuditMiddleware (CR55)
            request.state.key_id = info.key_id
        except ValueError:
            raise HTTPException(status_code=503, detail="Service initializing")

        status_code = 503 if deep.status == "unhealthy" else 200
        return Response(
            content=deep.model_dump_json(),
            status_code=status_code,
            media_type="application/json",
        )

    # Shallow - unauthenticated
    status_code = 503 if shallow.status == "unhealthy" else 200
    return Response(
        content=shallow.model_dump_json(),
        status_code=status_code,
        media_type="application/json",
    )


@router.get("/health/memory", response_model=_health.MemoryHealthResponse)
async def health_memory(
    _key: ApiKeyInfo = Depends(_require_any_token),
) -> _health.MemoryHealthResponse:
    """Return process memory stats (ARCH-027). Requires any valid Bearer token."""
    return _health.check_memory()


@router.get("/health/{component}", response_model=_health.ComponentHealth)
async def health_component(
    component: str,
    request: Request,
    _key: ApiKeyInfo = Depends(_require_any_token),
) -> _health.ComponentHealth:
    """Return health status for a single component. Requires any valid Bearer token."""
    registry = getattr(request.app.state, "registry", None)
    return await _health.check_component(registry, component)


# ---------------------------------------------------------------------------
# API key management (REQ-020, REQ-023)
# ---------------------------------------------------------------------------


@router.post("/api/v1/api-keys", response_model=CreateKeyResponse, status_code=201)
async def create_api_key(
    body: CreateKeyRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> CreateKeyResponse:
    """Create an API key.

    Authentication: bootstrap key (first use only) OR admin-scoped Bearer token.
    """
    from vektra_admin.models import ApiKeyOrm  # late import

    if credentials is None:
        err = auth_invalid_token()
        raise HTTPException(status_code=http_status_for(err), detail=err.to_envelope())

    token = credentials.credentials
    registry = getattr(request.app.state, "registry", None)

    key_info: ApiKeyInfo | None = None
    is_bootstrap = _bootstrap.is_bootstrap_key(token)

    # --- Authenticate: bootstrap or admin key ---
    if is_bootstrap:
        # Check consumption before proceeding
        consumed = await _bootstrap.is_bootstrap_consumed(session)
        if consumed:
            err = auth_invalid_token()
            raise HTTPException(
                status_code=http_status_for(err), detail=err.to_envelope()
            )
        # Bootstrap key will be consumed after successful DB write (below)
    else:
        # Must be a valid admin-scoped key
        if registry is None:
            raise HTTPException(
                status_code=500, detail="ProviderRegistry not initialized"
            )
        try:
            key_store = registry.get("key_store", "default")
            key_info = await key_store.lookup_by_token(token)
        except ValueError:
            key_info = None

        if key_info is None:
            err = auth_invalid_token()
            raise HTTPException(
                status_code=http_status_for(err), detail=err.to_envelope()
            )

        if "admin" not in key_info.scopes:
            err = ErrorResponse(
                category=ErrorCategory.PERMANENT,
                code="ERR-AUTH-003",
                message="Creating API keys requires 'admin' scope.",
                remediation="Use an API key with 'admin' scope.",
            )
            raise HTTPException(status_code=403, detail=err.to_envelope())

    # --- Validate scopes ---
    valid_scopes = {"admin", "ingest", "query"}
    requested_scopes = body.scopes if body.scopes is not None else ["admin"]
    invalid = set(requested_scopes) - valid_scopes
    if invalid:
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code="ERR-ADMIN-001",
            message=f"Invalid scopes: {sorted(invalid)}",
            remediation=f"Use only valid scopes: {sorted(valid_scopes)}.",
        )
        raise HTTPException(status_code=422, detail=err.to_envelope())

    # --- Validate expires_at ---
    if body.expires_at is not None and body.expires_at <= datetime.now(UTC):
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code="ERR-ADMIN-004",
            message="expires_at must be in the future.",
            remediation="Provide a future UTC timestamp or omit expires_at.",
        )
        raise HTTPException(status_code=422, detail=err.to_envelope())

    # --- Generate and persist key ---
    plaintext, key_hash, key_preview = generate_key()

    new_key = ApiKeyOrm(
        key_hash=key_hash,
        key_preview=key_preview,
        label=body.label,
        scopes=requested_scopes,
        expires_at=body.expires_at,
    )
    session.add(new_key)

    if _bootstrap.is_bootstrap_key(token):
        await _bootstrap.consume_bootstrap_key(session)

    await session.commit()
    await session.refresh(new_key)

    # --- Update in-memory cache immediately ---
    if registry is not None:
        try:
            key_store = registry.get("key_store", "default")
            await key_store.add_key(
                key_id=new_key.id,
                key_hash=key_hash,
                key_preview=key_preview,
                scopes=requested_scopes,
                expires_at=new_key.expires_at,
            )
        except ValueError:
            pass  # key_store not yet registered (e.g. during tests)

    # --- Audit log (async background task; log_event creates its own session) ---
    # NFR-007: log ALL authenticated requests, including bootstrap key usage.
    # AuditLogOrm.key_id is NOT a FK, so UUID(int=0) is safe as sentinel.
    _BOOTSTRAP_SENTINEL = UUID(int=0)
    request_id = _resolve_request_id(request)
    background_tasks.add_task(
        _audit.log_event,
        key_id=key_info.key_id if key_info else _BOOTSTRAP_SENTINEL,
        endpoint="/api/v1/api-keys",
        method="POST",
        status_code=201,
        request_id=request_id,
        action="apikey_created_bootstrap" if is_bootstrap else "apikey_created",
        log_metadata={"new_key_id": str(new_key.id)},
    )

    log.info("api_key_created", key_id=str(new_key.id), scopes=requested_scopes)

    return CreateKeyResponse(
        id=new_key.id,
        key=plaintext,
        key_preview=key_preview,
        label=new_key.label,
        scopes=new_key.scopes,
        created_at=new_key.created_at,
    )


@router.get("/api/v1/api-keys", response_model=list[KeyListItem])
async def list_api_keys(
    session: AsyncSession = Depends(get_session),
    _key: ApiKeyInfo = Depends(require_scope("admin")),
) -> list[KeyListItem]:
    """List all API keys (metadata only, never plaintext). Requires admin scope."""
    from vektra_admin.models import ApiKeyOrm  # late import

    result = await session.execute(select(ApiKeyOrm))
    rows = result.scalars().all()
    return [
        KeyListItem(
            id=row.id,
            key_preview=row.key_preview,
            label=row.label,
            scopes=row.scopes,
            created_at=row.created_at,
            last_used_at=row.last_used_at,
            revoked=row.revoked_at is not None,
            expires_at=row.expires_at,
        )
        for row in rows
    ]


@router.delete("/api/v1/api-keys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    key_info: ApiKeyInfo = Depends(require_scope("admin")),
) -> None:
    """Soft-delete (revoke) an API key. Requires admin scope."""
    from vektra_admin.models import ApiKeyOrm  # late import

    result = await session.execute(select(ApiKeyOrm).where(ApiKeyOrm.id == key_id))
    row = result.scalar_one_or_none()
    if row is None:
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code="ERR-ADMIN-002",
            message="API key not found.",
            remediation="Verify the key ID and try again.",
        )
        raise HTTPException(status_code=404, detail=err.to_envelope())
    if row.revoked_at is not None:
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code="ERR-ADMIN-003",
            message="API key already revoked.",
            remediation="No action needed; the key is already inactive.",
        )
        raise HTTPException(status_code=409, detail=err.to_envelope())

    row.revoked_at = datetime.now(UTC)
    await session.commit()

    # Update in-memory cache immediately (do not wait for TTL)
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        try:
            key_store = registry.get("key_store", "default")
            await key_store.revoke_key_by_id(key_id)
        except ValueError:
            pass

    # Audit log (async background task; log_event creates its own session)
    request_id = _resolve_request_id(request)
    background_tasks.add_task(
        _audit.log_event,
        key_id=key_info.key_id,
        endpoint=f"/api/v1/api-keys/{key_id}",
        method="DELETE",
        status_code=204,
        request_id=request_id,
        action="apikey_revoked",
        log_metadata={"revoked_key_id": str(key_id)},
    )

    log.info("api_key_revoked", key_id=str(key_id), revoked_by=str(key_info.key_id))


# ---------------------------------------------------------------------------
# Admin HTML dashboard (REQ-006)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Admin conversation turns (DEBT-011)
# ---------------------------------------------------------------------------


class ConversationTurnDetail(BaseModel):
    """Decrypted conversation turn with full metadata."""

    turn_number: int
    question: str
    answer: str | None
    response_id: UUID | None
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    created_at: datetime


@router.get(
    "/api/v1/admin/conversations/{conversation_id}/turns",
    response_model=list[ConversationTurnDetail],
)
async def get_conversation_turns(
    conversation_id: UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    _key: ApiKeyInfo = Depends(require_scope("admin")),
) -> Any:
    """Return decrypted conversation turns with full metadata (admin only)."""
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        raise HTTPException(status_code=500, detail="ProviderRegistry not initialized")

    try:
        conv_store = registry.get("conversation_store", "default")
    except ValueError:
        raise HTTPException(status_code=503, detail="Conversation store not available")

    if not hasattr(conv_store, "get_turns_detail"):
        raise HTTPException(
            status_code=501,
            detail="Conversation decryption not available (in-memory store)",
        )

    turns = await conv_store.get_turns_detail(conversation_id)
    if turns is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Audit log: sensitive content access
    request_id = _resolve_request_id(request)
    background_tasks.add_task(
        _audit.log_event,
        key_id=_key.key_id,
        endpoint=f"/api/v1/admin/conversations/{conversation_id}/turns",
        method="GET",
        status_code=200,
        request_id=request_id,
        action="conversation_turns_read",
        log_metadata={
            "namespace": getattr(request.state, "rls_namespace", None),
            "conversation_id": str(conversation_id),
            "turn_count": len(turns),
        },
    )

    return turns


# ---------------------------------------------------------------------------
# Namespace configuration (WI-3, FEAT-020 write path)
# ---------------------------------------------------------------------------


class NamespaceConfigResponse(BaseModel):
    """Full namespace config after a PATCH merge."""

    namespace_id: str
    config: dict[str, Any]


class NamespaceConfigDetailResponse(BaseModel):
    """Full namespace config plus the effective values after env-default fallback.

    *config* mirrors what is physically stored in ``namespaces.config``.
    *resolved* is what queries actually see at runtime, after the resolvers
    in :mod:`vektra_shared.namespace` apply the env-var fallback chain
    (namespace JSONB > env var > hardcoded default). Consumers like the
    Moodle block edit form use *resolved* to render the "Use default" /
    "Override" toggle without having to re-implement the chain client-side.
    """

    namespace_id: str
    config: dict[str, Any]
    resolved: dict[str, Any]


def _resolved_from_stored(
    stored: dict[str, Any],
    defaults: dict[str, Any],
) -> dict[str, Any]:
    """Apply the namespace > env > hardcoded-default chain to a loaded JSONB.

    Operates on an already-loaded config dict so the GET endpoint can avoid
    opening extra DB sessions (the standalone resolvers in
    :mod:`vektra_shared.namespace` remain for callers that hold only a
    namespace id).

    Validation is derived from the module-level ``ALLOWED_CONFIG_VALUES`` /
    ``ALLOWED_CONFIG_TYPES`` maps and the result is built by iterating
    ``ALLOWED_CONFIG_KEYS``, so adding a new whitelist key automatically
    flows through here without further edits — the only requirement is that
    *defaults* provides an entry for the new key (assertion below).
    """
    assert ALLOWED_CONFIG_KEYS <= defaults.keys(), (
        "defaults must provide a fallback for every key in "
        "ALLOWED_CONFIG_KEYS; missing: "
        f"{sorted(ALLOWED_CONFIG_KEYS - defaults.keys())}"
    )
    resolved: dict[str, Any] = {}
    for key in ALLOWED_CONFIG_KEYS:
        raw = stored.get(key)
        allowed_values = ALLOWED_CONFIG_VALUES.get(key)
        allowed_type = ALLOWED_CONFIG_TYPES.get(key)
        if allowed_values is not None and raw in allowed_values:
            resolved[key] = raw
        elif allowed_type is not None and isinstance(raw, allowed_type):
            resolved[key] = raw
        else:
            resolved[key] = defaults[key]
    return resolved


@router.get(
    "/api/v1/admin/namespaces/{namespace_id}/config",
    response_model=NamespaceConfigDetailResponse,
)
async def get_namespace_config(
    namespace_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    key_info: ApiKeyInfo = Depends(require_scope("admin")),
) -> NamespaceConfigDetailResponse:
    """Return the stored namespace config and the effective resolved values.

    Behavior:
      - 404 with ``ERR-ADMIN-005`` if the namespace does not exist.
      - ``config`` is the raw JSONB (``{}`` when no key has ever been set).
      - ``resolved`` is the value that queries observe, computed via the
        same resolvers used by the runtime path. Always includes every
        key in ``ALLOWED_CONFIG_KEYS``.
    """
    from vektra_admin.models import NamespaceOrm  # late import

    result = await session.execute(
        select(NamespaceOrm).where(NamespaceOrm.id == namespace_id)
    )
    ns = result.scalar_one_or_none()
    if ns is None:
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code="ERR-ADMIN-005",
            message=f"Namespace '{namespace_id}' not found.",
            remediation="Verify the namespace id or create it via the admin UI.",
        )
        raise HTTPException(status_code=404, detail=err.to_envelope())

    stored = dict(ns.ns_config or {})
    resolved = _resolved_from_stored(
        stored,
        defaults={
            "grounding_mode": getattr(
                request.app.state, "grounding_mode_default", "strict"
            ),
            "show_sources": getattr(
                request.app.state, "learn_show_sources_default", True
            ),
        },
    )
    return NamespaceConfigDetailResponse(
        namespace_id=namespace_id,
        config=stored,
        resolved=resolved,
    )


@router.patch(
    "/api/v1/admin/namespaces/{namespace_id}/config",
    response_model=NamespaceConfigResponse,
)
async def patch_namespace_config(
    namespace_id: str,
    body: dict[str, Any],
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    key_info: ApiKeyInfo = Depends(require_scope("admin")),
) -> NamespaceConfigResponse:
    """Partially update a namespace's config JSONB.

    Behavior:
      - Flat body, one entry per config key (e.g. ``{"grounding_mode": "hybrid"}``).
      - Partial update: keys not present in the body are preserved.
      - ``null`` value removes the key from config (falls back to env default
        when resolved downstream, see :mod:`vektra_shared.namespace`).
      - Unknown keys are rejected with 400 (not silently ignored) to surface
        typos from upstream clients (e.g. Moodle plugin) during development.
    """
    from vektra_admin.models import NamespaceOrm  # late import

    # --- Validate body: unknown keys rejected (ERR-ADMIN-006) ---
    unknown_keys = set(body.keys()) - ALLOWED_CONFIG_KEYS
    if unknown_keys:
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code="ERR-ADMIN-006",
            message=f"Unknown config keys: {sorted(unknown_keys)}.",
            remediation=(
                f"Use only allowed keys: {sorted(ALLOWED_CONFIG_KEYS)}. "
                "Unknown keys are rejected to surface typos early."
            ),
        )
        raise HTTPException(status_code=400, detail=err.to_envelope())

    # --- Validate each value: enum or runtime type per key, or null (ERR-ADMIN-007) ---
    for key, value in body.items():
        if value is None:
            continue  # null = remove the key
        allowed_values = ALLOWED_CONFIG_VALUES.get(key)
        allowed_type = ALLOWED_CONFIG_TYPES.get(key)
        if allowed_values is not None and value not in allowed_values:
            err = ErrorResponse(
                category=ErrorCategory.PERMANENT,
                code="ERR-ADMIN-007",
                message=f"Invalid value for '{key}': {value!r}.",
                remediation=(
                    f"Use one of: {sorted(allowed_values)}, or null to unset."
                ),
            )
            raise HTTPException(status_code=400, detail=err.to_envelope())
        if allowed_type is not None and not isinstance(value, allowed_type):
            # isinstance(True, bool) is True and isinstance(1, bool) is False,
            # so a JSON integer cannot impersonate a bool here.
            err = ErrorResponse(
                category=ErrorCategory.PERMANENT,
                code="ERR-ADMIN-007",
                message=f"Invalid value for '{key}': {value!r}.",
                remediation=(
                    f"Value must be of type {allowed_type.__name__}, or null to unset."
                ),
            )
            raise HTTPException(status_code=400, detail=err.to_envelope())

    # --- Load namespace (ERR-ADMIN-005 if missing) ---
    result = await session.execute(
        select(NamespaceOrm).where(NamespaceOrm.id == namespace_id)
    )
    ns = result.scalar_one_or_none()
    if ns is None:
        err = ErrorResponse(
            category=ErrorCategory.PERMANENT,
            code="ERR-ADMIN-005",
            message=f"Namespace '{namespace_id}' not found.",
            remediation="Verify the namespace id or create it via the admin UI.",
        )
        raise HTTPException(status_code=404, detail=err.to_envelope())

    # --- Merge: null removes, non-null sets ---
    current_config: dict[str, Any] = dict(ns.ns_config or {})
    for key, value in body.items():
        if value is None:
            current_config.pop(key, None)
        else:
            current_config[key] = value

    ns.ns_config = current_config
    ns.updated_at = datetime.now(UTC)
    # JSONB mutation tracking: reassignment above is sufficient, but be explicit
    flag_modified(ns, "ns_config")
    await session.commit()

    # --- Audit log (NFR-007): config change is an administrative event ---
    request_id = _resolve_request_id(request)
    background_tasks.add_task(
        _audit.log_event,
        key_id=key_info.key_id,
        endpoint=f"/api/v1/admin/namespaces/{namespace_id}/config",
        method="PATCH",
        status_code=200,
        request_id=request_id,
        action="namespace_config_updated",
        log_metadata={
            "namespace_id": namespace_id,
            "updated_keys": sorted(body.keys()),
        },
    )

    log.info(
        "namespace_config_updated",
        namespace_id=namespace_id,
        updated_keys=sorted(body.keys()),
    )

    return NamespaceConfigResponse(namespace_id=namespace_id, config=current_config)


# ---------------------------------------------------------------------------
# Admin HTML dashboard (REQ-006)
# ---------------------------------------------------------------------------


@router.get("/admin")
async def admin_dashboard_redirect() -> Response:
    """Redirect legacy /admin to the new HTMX dashboard (ADR-0024)."""
    return Response(status_code=308, headers={"Location": "/admin/"})
