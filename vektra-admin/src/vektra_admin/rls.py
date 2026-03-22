"""RLS namespace isolation middleware (ADR-0009, ARCH-025).

When VEKTRA_MULTI_TENANT=true, this middleware executes
    SET LOCAL app.current_namespace = :namespace_id
at the start of each request's DB session, enabling PostgreSQL
row-level security policies.

When VEKTRA_MULTI_TENANT=false (default), this middleware is a no-op.

Registration: infra-app-entrypoint calls app.add_middleware(RLSMiddleware)
and sets app.state.multi_tenant = True/False based on config.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

log = structlog.get_logger(__name__)


class RLSMiddleware(BaseHTTPMiddleware):
    """Sets PostgreSQL session variable for RLS namespace isolation.

    The namespace is resolved from the request body (query or ingest payload).
    Only active when app.state.multi_tenant is True.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        multi_tenant = getattr(request.app.state, "multi_tenant", False)
        if not multi_tenant:
            return await call_next(request)

        # Resolve namespace from request body
        namespace = await _resolve_namespace(request)
        if namespace:
            # The actual SET LOCAL is executed by the session factory hook
            # registered in infra-phase2 app lifespan.
            request.state.rls_namespace = namespace

        return await call_next(request)


async def _resolve_namespace(request: Request) -> str | None:
    """Extract namespace from request body or query params.

    For write methods (POST/PUT/PATCH) the body takes precedence over
    query params to prevent a crafted ``?namespace=`` from overriding
    the payload namespace in multi-tenant mode.
    """
    # JSON body first for write methods (prevents query-param override)
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            body = await request.body()
            if body:
                data = json.loads(body)
                if isinstance(data, dict):
                    ns = data.get("namespace")
                    if isinstance(ns, str):
                        return ns
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    # Query param (safe for GET; fallback for POST without body namespace)
    namespace = request.query_params.get("namespace")
    if namespace:
        return namespace

    return None


async def set_rls_namespace(session: object, namespace: str) -> None:
    """Execute SET LOCAL to bind RLS for the current transaction.

    Called by the session factory hook in infra-phase2 when
    request.state.rls_namespace is set.

    Raises TypeError if *session* is not an AsyncSession, to catch
    misconfiguration early rather than silently skipping RLS binding.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    if not isinstance(session, AsyncSession):
        raise TypeError(
            f"set_rls_namespace requires AsyncSession, got {type(session).__name__}"
        )
    await session.execute(
        text("SET LOCAL app.current_namespace = :ns"),
        {"ns": namespace},
    )
