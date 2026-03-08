"""HTMX + Jinja2 admin UI router (ADR-0024, ARCH-062).

Serves the admin dashboard pages at /admin/*. All pages require admin scope
via HttpOnly cookie. Route handlers call service functions directly (no HTTP
round-trips) and pass plain dicts to Jinja2 templates. No business logic
lives in templates.

Authentication flow:
- GET /admin/login renders a form with token input
- POST /admin/login validates token, sets HttpOnly cookie, redirects
- All /admin/* routes read token from cookie only (no query param)
- HTMX requests send cookie automatically (same-origin XHR)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_ as sa_and
from sqlalchemy import or_ as sa_or
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vektra_admin import health as _health
from vektra_admin.models import ApiKeyOrm, AuditLogOrm, NamespaceOrm
from vektra_shared.auth import ApiKeyInfo, KeyStoreProvider
from vektra_shared.db import get_session

log = structlog.get_logger(__name__)

_BASE_DIR = Path(__file__).parent
_TEMPLATES_DIR = _BASE_DIR / "templates"
_STATIC_DIR = _BASE_DIR / "static"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

ui_router = APIRouter(prefix="/admin", tags=["admin-ui"])


# ---------------------------------------------------------------------------
# Auth dependency: cookie or query parameter
# ---------------------------------------------------------------------------

_COOKIE_NAME = "vektra_admin_token"


class _AdminRedirect(Exception):
    """Raised in auth dependencies to trigger a browser redirect.

    HTTPException with 303 doesn't produce a proper redirect response that
    browsers follow. This custom exception is caught by an exception handler
    registered on the app via ``register_admin_exception_handlers()``.
    """

    def __init__(
        self, url: str = "/admin/login", *, clear_cookie: bool = False
    ) -> None:
        self.url = url
        self.clear_cookie = clear_cookie


async def _handle_admin_redirect(
    request: Request, exc: _AdminRedirect
) -> RedirectResponse:
    response = RedirectResponse(url=exc.url, status_code=303)
    if exc.clear_cookie:
        response.delete_cookie(_COOKIE_NAME)
    return response


async def _get_admin_token(request: Request) -> str:
    """Extract admin token from HttpOnly cookie.

    Query parameter auth was removed to prevent token leakage via browser
    history, server logs, and Referer headers. Use POST /admin/login instead.
    """
    token = request.cookies.get(_COOKIE_NAME)
    if not token:
        raise _AdminRedirect()
    return token


async def _require_admin_ui(
    request: Request,
    token: str = Depends(_get_admin_token),
) -> ApiKeyInfo:
    """Validate admin token and return key info.

    Redirects to login page if token is missing or invalid.
    """
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="Service initializing")

    try:
        key_store: KeyStoreProvider = registry.get("key_store", "default")
    except ValueError:
        raise HTTPException(status_code=503, detail="Key store not configured")

    info = await key_store.lookup_by_token(token)
    if info is None or "admin" not in info.scopes:
        raise _AdminRedirect(clear_cookie=True)

    request.state.key_id = info.key_id
    return info


def _render(
    request: Request,
    template_name: str,
    *,
    active: str = "",
    **context: Any,
) -> HTMLResponse:
    """Render a Jinja2 template with common context variables."""
    version = getattr(request.app.state, "version", "unknown")
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "version": version,
            "active": active,
            **context,
        },
    )


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------


@ui_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    """Render the login form."""
    version = getattr(request.app.state, "version", "unknown")
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"version": version},
    )


@ui_router.post("/login")
async def login_submit(request: Request) -> Response:
    """Validate token from form, set cookie, redirect to dashboard."""
    form = await request.form()
    token = form.get("token", "")
    if not token or not isinstance(token, str):
        return RedirectResponse(url="/admin/login?error=missing", status_code=303)

    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return RedirectResponse(url="/admin/login?error=unavailable", status_code=303)

    try:
        key_store: KeyStoreProvider = registry.get("key_store", "default")
    except ValueError:
        return RedirectResponse(url="/admin/login?error=unavailable", status_code=303)

    info = await key_store.lookup_by_token(token)
    if info is None or "admin" not in info.scopes:
        return RedirectResponse(url="/admin/login?error=invalid", status_code=303)

    response = RedirectResponse(url="/admin/", status_code=303)
    response.set_cookie(
        key=_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        max_age=86400,  # 24 hours
    )
    return response


@ui_router.get("/logout")
async def logout(request: Request) -> Response:
    """Clear cookie and redirect to login."""
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(_COOKIE_NAME)
    return response


# ---------------------------------------------------------------------------
# Health dashboard (GET /admin/)
# ---------------------------------------------------------------------------


@ui_router.get("/", response_class=HTMLResponse)
async def health_dashboard(
    request: Request,
    auth: ApiKeyInfo = Depends(_require_admin_ui),
) -> HTMLResponse:
    """Render the health dashboard page."""
    _info = auth
    registry = getattr(request.app.state, "registry", None)
    version = getattr(request.app.state, "version", "unknown")

    _shallow, deep = await _health.check_all(registry, version)
    memory = _health.check_memory()

    return _render(
        request,
        "health.html",
        active="health",
        health=deep,
        memory=memory,
    )


@ui_router.get("/partials/health", response_class=HTMLResponse)
async def health_partial(
    request: Request,
    auth: ApiKeyInfo = Depends(_require_admin_ui),
) -> HTMLResponse:
    """HTMX partial: health table for polling refresh."""
    _info = auth
    registry = getattr(request.app.state, "registry", None)
    version = getattr(request.app.state, "version", "unknown")

    _shallow, deep = await _health.check_all(registry, version)

    return templates.TemplateResponse(
        request=request,
        name="partials/health_table.html",
        context={"health": deep},
    )


# ---------------------------------------------------------------------------
# API keys page (GET /admin/keys)
# ---------------------------------------------------------------------------


@ui_router.get("/keys", response_class=HTMLResponse)
async def keys_page(
    request: Request,
    auth: ApiKeyInfo = Depends(_require_admin_ui),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the API keys management page."""
    _info = auth
    result = await session.execute(select(ApiKeyOrm))
    rows = result.scalars().all()
    keys = [
        {
            "id": str(row.id),
            "key_preview": row.key_preview,
            "label": row.label,
            "scopes": row.scopes,
            "created_at": row.created_at,
            "last_used_at": row.last_used_at,
            "revoked": row.revoked_at is not None,
            "expires_at": row.expires_at,
        }
        for row in rows
    ]
    return _render(request, "keys.html", active="keys", keys=keys)


@ui_router.post("/keys/create", response_class=HTMLResponse)
async def keys_create(
    request: Request,
    auth: ApiKeyInfo = Depends(_require_admin_ui),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Create a new API key, return updated keys table partial with flash."""
    _info = auth
    form = await request.form()

    label = form.get("label", "") or None
    scopes = form.getlist("scopes") or ["admin"]

    from vektra_admin.keys import generate_key

    plaintext, key_hash, key_preview = generate_key()

    new_key = ApiKeyOrm(
        key_hash=key_hash,
        key_preview=key_preview,
        label=label,
        scopes=scopes,
    )
    session.add(new_key)
    await session.commit()
    await session.refresh(new_key)

    # Update in-memory cache
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        try:
            key_store = registry.get("key_store", "default")
            await key_store.add_key(
                key_id=new_key.id,
                key_hash=key_hash,
                key_preview=key_preview,
                scopes=scopes,
            )
        except ValueError:
            pass

    # Reload keys for table
    result = await session.execute(select(ApiKeyOrm))
    rows = result.scalars().all()
    keys = [
        {
            "id": str(row.id),
            "key_preview": row.key_preview,
            "label": row.label,
            "scopes": row.scopes,
            "created_at": row.created_at,
            "last_used_at": row.last_used_at,
            "revoked": row.revoked_at is not None,
            "expires_at": row.expires_at,
        }
        for row in rows
    ]

    return templates.TemplateResponse(
        request=request,
        name="partials/keys_table.html",
        context={
            "keys": keys,
            "flash": {
                "type": "success",
                "message": f"Key created. Save this now: {plaintext}",
            },
        },
    )


@ui_router.delete("/keys/{key_id}", response_class=HTMLResponse)
async def keys_revoke(
    key_id: str,
    request: Request,
    auth: ApiKeyInfo = Depends(_require_admin_ui),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Revoke an API key, return updated keys table partial."""
    _info = auth
    from datetime import UTC, datetime
    from uuid import UUID

    uid = UUID(key_id)
    result = await session.execute(select(ApiKeyOrm).where(ApiKeyOrm.id == uid))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Key not found")
    if row.revoked_at is not None:
        raise HTTPException(status_code=409, detail="Key already revoked")

    row.revoked_at = datetime.now(UTC)
    await session.commit()

    # Update in-memory cache
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        try:
            key_store = registry.get("key_store", "default")
            await key_store.revoke_key_by_id(uid)
        except ValueError:
            pass

    # Reload keys for table
    result = await session.execute(select(ApiKeyOrm))
    rows = result.scalars().all()
    keys = [
        {
            "id": str(row.id),
            "key_preview": row.key_preview,
            "label": row.label,
            "scopes": row.scopes,
            "created_at": row.created_at,
            "last_used_at": row.last_used_at,
            "revoked": row.revoked_at is not None,
            "expires_at": row.expires_at,
        }
        for row in rows
    ]

    return templates.TemplateResponse(
        request=request,
        name="partials/keys_table.html",
        context={
            "keys": keys,
            "flash": {"type": "success", "message": "Key revoked."},
        },
    )


# ---------------------------------------------------------------------------
# Namespaces page (GET /admin/namespaces)
# ---------------------------------------------------------------------------


@ui_router.get("/namespaces", response_class=HTMLResponse)
async def namespaces_page(
    request: Request,
    auth: ApiKeyInfo = Depends(_require_admin_ui),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the namespace management page."""
    _info = auth
    namespaces = await _load_namespaces(session)
    return _render(
        request, "namespaces.html", active="namespaces", namespaces=namespaces
    )


@ui_router.post("/namespaces/create", response_class=HTMLResponse)
async def namespaces_create(
    request: Request,
    auth: ApiKeyInfo = Depends(_require_admin_ui),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Create a new namespace, return updated table partial."""
    _info = auth
    form = await request.form()
    name = str(form.get("name", "")).strip()
    display_name = str(form.get("display_name", "")).strip() or None

    if not name:
        raise HTTPException(status_code=422, detail="Namespace name is required")

    ns = NamespaceOrm(id=name, display_name=display_name)
    session.add(ns)
    await session.commit()

    namespaces = await _load_namespaces(session)
    return templates.TemplateResponse(
        request=request,
        name="partials/namespaces_table.html",
        context={
            "namespaces": namespaces,
            "flash": {"type": "success", "message": f"Namespace '{name}' created."},
        },
    )


@ui_router.delete("/namespaces/{ns_id}", response_class=HTMLResponse)
async def namespaces_delete(
    ns_id: str,
    request: Request,
    auth: ApiKeyInfo = Depends(_require_admin_ui),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Delete a namespace, return updated table partial."""
    _info = auth
    result = await session.execute(select(NamespaceOrm).where(NamespaceOrm.id == ns_id))
    ns = result.scalar_one_or_none()
    if ns is None:
        raise HTTPException(status_code=404, detail="Namespace not found")

    await session.delete(ns)
    await session.commit()

    namespaces = await _load_namespaces(session)
    return templates.TemplateResponse(
        request=request,
        name="partials/namespaces_table.html",
        context={
            "namespaces": namespaces,
            "flash": {"type": "success", "message": f"Namespace '{ns_id}' deleted."},
        },
    )


async def _load_namespaces(session: AsyncSession) -> list[dict[str, Any]]:
    """Load namespaces with document counts.

    Uses raw SQL join to source_documents to avoid cross-module ORM import
    (ADR-0005: vektra_admin must not import from vektra_ingest).
    """
    from sqlalchemy import text

    result = await session.execute(
        text(
            "SELECT n.id, n.display_name, n.created_at, "
            "COUNT(sd.id) AS doc_count "
            "FROM namespaces n "
            "LEFT JOIN source_documents sd ON sd.namespace_id = n.id AND sd.deleted_at IS NULL "
            "GROUP BY n.id "
            "ORDER BY n.created_at DESC"
        )
    )
    rows = result.all()
    return [
        {
            "id": row.id,
            "display_name": row.display_name,
            "doc_count": row.doc_count,
            "created_at": row.created_at,
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Audit log page (GET /admin/audit)
# ---------------------------------------------------------------------------

_AUDIT_PAGE_SIZE = 50


@ui_router.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request,
    auth: ApiKeyInfo = Depends(_require_admin_ui),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the audit log page."""
    _info = auth
    entries, has_older = await _load_audit_entries(session, request.query_params)
    return _render(
        request,
        "audit.html",
        active="audit",
        entries=entries,
        has_older=has_older,
        params=dict(request.query_params),
    )


@ui_router.get("/partials/audit", response_class=HTMLResponse)
async def audit_partial(
    request: Request,
    auth: ApiKeyInfo = Depends(_require_admin_ui),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX partial: audit log rows for pagination."""
    _info = auth
    entries, has_older = await _load_audit_entries(session, request.query_params)
    return templates.TemplateResponse(
        request=request,
        name="partials/audit_rows.html",
        context={
            "entries": entries,
            "has_older": has_older,
            "params": dict(request.query_params),
        },
    )


async def _load_audit_entries(
    session: AsyncSession,
    params: Any,
) -> tuple[list[dict[str, Any]], bool]:
    """Load audit log entries with cursor-based pagination and filters."""
    from datetime import datetime

    stmt = select(AuditLogOrm).order_by(
        AuditLogOrm.created_at.desc(), AuditLogOrm.id.desc()
    )

    # Cursor-based pagination using created_at + id tiebreaker
    cursor_ts = params.get("cursor_ts")
    cursor_id = params.get("cursor_id")
    direction = params.get("direction", "older")
    if cursor_ts and cursor_id:
        from uuid import UUID

        cursor_dt = datetime.fromisoformat(cursor_ts)
        cursor_uuid = UUID(cursor_id)
        if direction == "older":
            stmt = stmt.where(
                sa_or(
                    AuditLogOrm.created_at < cursor_dt,
                    sa_and(
                        AuditLogOrm.created_at == cursor_dt,
                        AuditLogOrm.id < cursor_uuid,
                    ),
                )
            )
        else:
            stmt = stmt.where(
                sa_or(
                    AuditLogOrm.created_at > cursor_dt,
                    sa_and(
                        AuditLogOrm.created_at == cursor_dt,
                        AuditLogOrm.id > cursor_uuid,
                    ),
                )
            )

    # Filters
    endpoint_filter = params.get("endpoint")
    if endpoint_filter:
        stmt = stmt.where(AuditLogOrm.endpoint == endpoint_filter)

    status_filter = params.get("status_code")
    if status_filter:
        try:
            stmt = stmt.where(AuditLogOrm.status_code == int(status_filter))
        except ValueError:
            pass  # ignore invalid status_code filter

    date_start = params.get("date_start")
    if date_start:
        try:
            stmt = stmt.where(
                AuditLogOrm.created_at >= datetime.fromisoformat(date_start)
            )
        except ValueError:
            pass  # ignore invalid date format

    date_end = params.get("date_end")
    if date_end:
        try:
            stmt = stmt.where(
                AuditLogOrm.created_at <= datetime.fromisoformat(date_end)
            )
        except ValueError:
            pass  # ignore invalid date format

    # Fetch one extra to detect if there are more pages
    stmt = stmt.limit(_AUDIT_PAGE_SIZE + 1)
    result = await session.execute(stmt)
    rows = list(result.scalars().all())

    has_older = len(rows) > _AUDIT_PAGE_SIZE
    if has_older:
        rows = rows[:_AUDIT_PAGE_SIZE]

    entries = [
        {
            "id": str(row.id),
            "key_id": str(row.key_id),
            "endpoint": row.endpoint,
            "method": row.method,
            "status_code": row.status_code,
            "action": row.action,
            "created_at": row.created_at,
        }
        for row in rows
    ]
    return entries, has_older


# ---------------------------------------------------------------------------
# Config page (GET /admin/config)
# ---------------------------------------------------------------------------

_SECRET_KEYWORDS = {
    "KEY",
    "SECRET",
    "PASSWORD",
    "TOKEN",
    "DATABASE",
    "DSN",
    "CREDENTIAL",
}


def _mask_value(key: str, value: str) -> str:
    """Mask env var values that likely contain secrets."""
    upper_key = key.upper()
    if any(kw in upper_key for kw in _SECRET_KEYWORDS):
        return "****"
    return value


@ui_router.get("/config", response_class=HTMLResponse)
async def config_page(
    request: Request,
    auth: ApiKeyInfo = Depends(_require_admin_ui),
) -> HTMLResponse:
    """Render the system config page (read-only)."""
    _info = auth

    # Collect VEKTRA_* env vars
    env_vars = sorted(
        [
            {"key": k, "value": _mask_value(k, v)}
            for k, v in os.environ.items()
            if k.startswith("VEKTRA_")
        ],
        key=lambda x: x["key"],
    )

    # Collect active providers from registry
    registry = getattr(request.app.state, "registry", None)
    providers: list[dict[str, str]] = []
    if registry is not None:
        for category in sorted(registry._store.keys()):
            for name in registry.list(category):
                providers.append({"category": category, "name": name})

    return _render(
        request,
        "config.html",
        active="config",
        env_vars=env_vars,
        providers=providers,
    )


# ---------------------------------------------------------------------------
# Static files mount helper
# ---------------------------------------------------------------------------


def get_static_files() -> StaticFiles:
    """Return the StaticFiles app for /admin/static."""
    return StaticFiles(directory=str(_STATIC_DIR))


def register_ui_exception_handlers(app: Any) -> None:
    """Register custom exception handlers needed by the admin UI.

    Call this after ``app.include_router(ui_router)``.
    """
    app.add_exception_handler(_AdminRedirect, _handle_admin_redirect)
