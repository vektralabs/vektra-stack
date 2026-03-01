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
            request.state.rls_namespace = namespace

            # Set the PostgreSQL session variable via the session dependency
            # The actual SET LOCAL is executed by the session factory hook
            # registered in infra-phase2 app lifespan.
            request.state.rls_namespace = namespace

        return await call_next(request)


async def _resolve_namespace(request: Request) -> str | None:
    """Extract namespace from request body or query params."""
    # Query param (for GET requests)
    namespace = request.query_params.get("namespace")
    if namespace:
        return namespace

    # JSON body (for POST requests)
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            body = await request.body()
            if body:
                data = json.loads(body)
                return data.get("namespace")
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    return None


async def set_rls_namespace(session: object, namespace: str) -> None:
    """Execute SET LOCAL to bind RLS for the current transaction.

    Called by the session factory hook in infra-phase2 when
    request.state.rls_namespace is set.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    if isinstance(session, AsyncSession):
        await session.execute(
            text("SET LOCAL app.current_namespace = :ns"),
            {"ns": namespace},
        )
