"""Audit middleware for vektra-admin (NFR-007, REQ-022).

AuditMiddleware intercepts every request after auth completes and writes
a fire-and-forget audit_log entry via vektra_shared.audit.log_event().

Registration: exported as a class; infra-app-entrypoint calls
    app.add_middleware(AuditMiddleware)

This module does NOT register the middleware on any FastAPI app. That is
exclusively the responsibility of infra-app-entrypoint (Wave 4).

Excluded paths: /health, /metrics (unauthenticated endpoints — no key_id).
"""
from __future__ import annotations

from typing import Any, Callable, Awaitable
from uuid import UUID

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

import vektra_shared.audit as _shared_audit

log = structlog.get_logger(__name__)

_EXCLUDED_PREFIXES = ("/health", "/metrics")


class AuditMiddleware(BaseHTTPMiddleware):
    """Writes one audit_log row per authenticated request (NFR-007).

    Runs after auth middleware so request.state already has key_id resolved.
    Skips /health and /metrics (no Bearer token expected on those paths).
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

        # key_id must be present in request.state (set by auth middleware)
        key_id: UUID | None = getattr(request.state, "key_id", None)
        request_id: UUID | None = getattr(request.state, "request_id", None)

        if key_id is None or request_id is None:
            # Auth middleware did not resolve a key (e.g. 401 path); skip audit
            return response

        # Session must be available from request state
        session = getattr(request.state, "session", None)
        if session is None:
            log.warning("audit_middleware_no_session", path=path)
            return response

        _shared_audit.log_event(
            session=session,
            key_id=key_id,
            endpoint=path,
            method=request.method,
            status_code=response.status_code,
            request_id=request_id,
        )

        return response
