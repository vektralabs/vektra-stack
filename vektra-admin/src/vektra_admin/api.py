"""vektra-admin FastAPI router (REQ-004, REQ-006, REQ-019 to REQ-025).

Mounts at application root (/ prefix). Provides:
  - Health endpoints (two-tier model, REQ-025)
  - API key CRUD (REQ-020, REQ-023)
  - Health dashboard HTML (GET /admin)
  - Prometheus metrics are mounted by infra-app-entrypoint (starlette-prometheus).

Bootstrap auth (REQ-021, REQ-036): POST /api-keys accepts the bootstrap env-var key
as authentication (once only) or a valid admin-scoped Bearer token.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
)
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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

log = structlog.get_logger(__name__)

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
        # Validate token (best-effort; use same registry pattern)
        if registry is not None:
            try:
                key_store = registry.get("key_store", "default")
                info = await key_store.lookup_by_token(credentials.credentials)
                if info is None:
                    err = auth_invalid_token()
                    raise HTTPException(
                        status_code=http_status_for(err), detail=err.to_envelope()
                    )
            except ValueError:
                pass  # key_store not yet registered during startup probe

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

    # --- Authenticate: bootstrap or admin key ---
    if _bootstrap.is_bootstrap_key(token):
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
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        is_bootstrap = _bootstrap.is_bootstrap_key(token)
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
    request_id = getattr(request.state, "request_id", None)
    if request_id:
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


@router.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    _key: ApiKeyInfo = Depends(require_scope("admin")),
) -> HTMLResponse:
    """Minimal HTML dashboard showing current health status. Requires admin scope."""
    registry = getattr(request.app.state, "registry", None)
    version = getattr(request.app.state, "version", "unknown")

    _, deep = await _health.check_all(registry, version)

    status_color = {
        "healthy": "#2d8a4e",
        "degraded": "#b8860b",
        "unhealthy": "#cc3333",
    }.get(deep.status, "#666")

    rows = "".join(
        f"<tr>"
        f"<td>{c.name}</td>"
        f"<td style='color:{status_color if c.status == deep.status else '#666'}'>{c.status}</td>"
        f"<td>{c.latency_ms if c.latency_ms is not None else '-'} ms</td>"
        f"<td>{c.message or ''}</td>"
        f"</tr>"
        for c in deep.components
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Vektra health</title>
<style>
  body {{ font-family: monospace; margin: 2rem; background: #f5f5f5; color: #222; }}
  h1 {{ font-size: 1.2rem; }}
  .status {{ color: {status_color}; font-weight: bold; font-size: 1.1rem; }}
  table {{ border-collapse: collapse; width: 100%; max-width: 700px; }}
  th, td {{ border: 1px solid #ccc; padding: 0.4rem 0.8rem; text-align: left; }}
  th {{ background: #e0e0e0; }}
</style>
</head>
<body>
<h1>Vektra {version}</h1>
<p>Status: <span class="status">{deep.status}</span> &mdash; {deep.timestamp}</p>
<table>
<tr><th>Component</th><th>Status</th><th>Latency</th><th>Message</th></tr>
{rows if rows else "<tr><td colspan='4'>No health checks registered</td></tr>"}
</table>
</body>
</html>"""

    return HTMLResponse(content=html, status_code=200)
