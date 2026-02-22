"""Audit middleware for vektra-admin (NFR-007, REQ-022).

AuditMiddleware intercepts every authenticated request after the response
is generated and writes one audit_log row using its own managed AsyncSession.

Registration: exported as a class; infra-app-entrypoint calls
    app.add_middleware(AuditMiddleware)

This module does NOT register the middleware on any FastAPI app. That is
exclusively the responsibility of infra-app-entrypoint (Wave 4).

Excluded paths: /health, /metrics (unauthenticated — no key_id).

Session lifecycle: the middleware creates a fresh AsyncSession per write
rather than relying on request.state.session. This decouples the audit
write from the request's main session and ensures the write completes even
if the main session was already closed by the time the middleware runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

log = structlog.get_logger(__name__)

_EXCLUDED_PREFIXES = ("/health", "/metrics")

# Strong references to prevent GC of pending audit tasks
_audit_tasks: set[asyncio.Task[None]] = set()


class AuditMiddleware(BaseHTTPMiddleware):
    """Writes one audit_log row per authenticated request (NFR-007).

    Runs after auth middleware so request.state.key_id is already set
    (vektra_shared.auth.require_scope sets it on successful validation).
    Skips /health and /metrics (unauthenticated).
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)

        # Skip excluded paths
        path = request.url.path
        if any(path.startswith(prefix) for prefix in _EXCLUDED_PREFIXES):
            return response

        # key_id is set by require_scope() in vektra_shared.auth after validation
        key_id: UUID | None = getattr(request.state, "key_id", None)
        request_id: UUID | None = getattr(request.state, "request_id", None)

        if key_id is None or request_id is None:
            # Unauthenticated request (e.g. 401 response from auth dep) — skip
            return response

        # Write audit log with a dedicated session (independent of request lifecycle)
        task = asyncio.create_task(
            _write_audit(
                key_id=key_id,
                endpoint=path,
                method=request.method,
                status_code=response.status_code,
                request_id=request_id,
            )
        )
        _audit_tasks.add(task)
        task.add_done_callback(_audit_tasks.discard)

        return response


async def _write_audit(
    *,
    key_id: UUID,
    endpoint: str,
    method: str,
    status_code: int,
    request_id: UUID,
) -> None:
    """Write one audit_log row using an independent session.

    Errors are logged as ERROR and swallowed (audit write must not
    affect the API response — NFR-007 integrity principle).
    """
    from vektra_admin.models import AuditLogOrm  # late import
    from vektra_shared.db import get_session_factory

    try:
        session_factory = get_session_factory()
    except RuntimeError:
        log.warning("audit_middleware_no_session_factory")
        return

    try:
        async with session_factory() as session:
            entry = AuditLogOrm(
                key_id=key_id,
                endpoint=endpoint,
                method=method,
                status_code=status_code,
                request_id=request_id,
            )
            session.add(entry)
            await session.commit()
    except Exception:
        log.error(
            "audit_middleware_write_failed",
            key_id=str(key_id),
            endpoint=endpoint,
            exc_info=True,
        )
