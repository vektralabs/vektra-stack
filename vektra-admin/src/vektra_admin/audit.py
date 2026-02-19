"""Audit log writer for vektra-admin (REQ-022, NFR-007).

This module implements the concrete audit log writer. It is NOT imported
directly by other vektra_* components (ADR-0005 boundary). Instead, at
startup infra-app-entrypoint injects this function into vektra_shared:

    from vektra_admin.audit import log_event
    from vektra_shared import audit as shared_audit
    shared_audit.set_log_fn(log_event)

After injection, all components call vektra_shared.audit.log_event(**kwargs)
which delegates here. Before injection, log_event in vektra_shared is a no-op.

log_event is async and creates its own AsyncSession from the module-level
factory. This means:
- It can be used directly from async contexts
- It can be registered as a FastAPI BackgroundTask (FastAPI awaits async tasks)
- It does NOT rely on a session passed from the caller (which may be closed
  by the time a background task runs)

Never write query text, response content, or conversation data (REQ-051).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

log = structlog.get_logger(__name__)


async def log_event(
    *,
    key_id: UUID,
    endpoint: str,
    method: str,
    status_code: int,
    request_id: UUID,
    action: str | None = None,
    log_metadata: dict[str, Any] | None = None,
) -> None:
    """Write an audit log entry.

    Creates its own AsyncSession from the module-level factory so it can be
    used safely as a FastAPI BackgroundTask (caller's session may be closed).

    Errors are swallowed and logged as ERROR (NFR-007: write failures must not
    affect the API response).

    Args:
        key_id: UUID of the API key that made the request.
        endpoint: Request path (e.g. '/api/v1/ingest').
        method: HTTP method (GET, POST, etc.).
        status_code: HTTP response status code.
        request_id: Correlation UUID from the X-Request-ID header.
        action: Optional named event (e.g. 'apikey_created', 'apikey_revoked').
        log_metadata: Optional extra JSONB payload (never contains PII).
    """
    from vektra_admin.models import AuditLogOrm  # late import
    from vektra_shared.db import _session_factory  # module-level factory

    if _session_factory is None:
        log.warning("audit_log_no_session_factory", endpoint=endpoint)
        return

    try:
        async with _session_factory() as session:
            entry = AuditLogOrm(
                key_id=key_id,
                endpoint=endpoint,
                method=method,
                status_code=status_code,
                request_id=request_id,
                action=action,
                log_metadata=log_metadata or {},
            )
            session.add(entry)
            await session.commit()
    except Exception:
        log.error(
            "audit_log_write_failed",
            key_id=str(key_id),
            endpoint=endpoint,
            action=action,
            exc_info=True,
        )
