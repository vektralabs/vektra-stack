"""Audit log writer for vektra-admin (REQ-022, NFR-007).

This module implements the concrete audit log writer. It is NOT imported
directly by other vektra_* components (ADR-0005 boundary). Instead, at
startup infra-app-entrypoint injects this function into vektra_shared:

    from vektra_admin.audit import log_event
    from vektra_shared import audit as shared_audit
    shared_audit.set_log_fn(log_event)

After injection, all components call vektra_shared.audit.log_event(**kwargs)
which delegates here. Before injection, log_event in vektra_shared is a no-op.

Writes are fire-and-forget (FastAPI BackgroundTasks). On write failure, a
structured ERROR is logged to the application log — the exception is NOT
propagated (NFR-007: audit integrity is a hard gate, but a write failure
must not bring down the API response).

Never write query text, response content, or conversation data (REQ-051).
"""
from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)


async def _write_audit_row(session: AsyncSession, **kwargs: Any) -> None:
    """Insert a single audit_log row. Called from within an async context."""
    from vektra_admin.models import AuditLogOrm  # late import: avoids ORM load at module init

    entry = AuditLogOrm(**kwargs)
    session.add(entry)
    await session.commit()


def log_event(
    *,
    session: AsyncSession,
    key_id: UUID,
    endpoint: str,
    method: str,
    status_code: int,
    request_id: UUID,
    action: str | None = None,
    log_metadata: dict[str, Any] | None = None,
) -> None:
    """Write an audit log entry for an authenticated request.

    This function is registered into vektra_shared.audit at startup.
    It schedules the DB write as a coroutine on the running event loop;
    the caller does not await it (fire-and-forget).

    Args:
        session: An AsyncSession bound to the current request context.
        key_id: UUID of the API key that made the request.
        endpoint: Request path (e.g. '/api/v1/ingest').
        method: HTTP method (GET, POST, etc.).
        status_code: HTTP response status code.
        request_id: Correlation UUID from the X-Request-ID header.
        action: Optional named event (e.g. 'apikey_created', 'apikey_revoked').
        log_metadata: Optional extra JSONB payload (never contains PII).
    """
    row_kwargs: dict[str, Any] = {
        "key_id": key_id,
        "endpoint": endpoint,
        "method": method,
        "status_code": status_code,
        "request_id": request_id,
        "action": action,
        "log_metadata": log_metadata or {},
    }

    async def _fire() -> None:
        try:
            await _write_audit_row(session, **row_kwargs)
        except Exception:
            log.error(
                "audit_log_write_failed",
                key_id=str(key_id),
                endpoint=endpoint,
                action=action,
                exc_info=True,
            )

    # Schedule on the running event loop without blocking the caller.
    loop = asyncio.get_event_loop()
    loop.create_task(_fire())
