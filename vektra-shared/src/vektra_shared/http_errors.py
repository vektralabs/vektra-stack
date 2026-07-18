"""FastAPI exception handlers for the REQ-010 error envelope (DEBT-034).

Kept separate from errors.py (the pure envelope model) so the wire-shape
wiring lives in one place and every app registers identical behavior: the
assembled application in vektra-app and the minimal router apps built in each
package's tests. Without a single registrar the two drift — the real app
unwraps the envelope to the document root while a bare test app leaves it
nested under ``detail``, and the tests then assert a shape production never
emits.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response


def register_error_handlers(app: FastAPI) -> None:
    """Register the REQ-010 envelope exception handlers on a FastAPI app.

    HTTPException-based errors carry the REQ-010 envelope in ``exc.detail``
    (raised as ``detail=err.to_envelope()``). FastAPI's default handler would
    serialize that as ``{"detail": {"error": {...}}}``, one level below where
    the docs and the uncaught-500 handler put it. This unwraps it so every
    error path carries the envelope at the document root ``{"error": {...}}``.
    Non-envelope details (the admin UI's string messages, FastAPI's own
    404/405) fall through to the default handler unchanged. ``exc.headers`` is
    preserved so the rate-limit raise keeps its ``X-RateLimit-*`` headers.
    """

    @app.exception_handler(StarletteHTTPException)
    async def _envelope_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> Response:
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            return JSONResponse(
                status_code=exc.status_code,
                content=detail,
                headers=exc.headers,
            )
        return await http_exception_handler(request, exc)
