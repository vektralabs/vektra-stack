"""Audit log interface for the Vektra platform.

vektra_shared defines the interface; the implementation is injected at startup
by vektra_admin via set_log_fn(). Before injection, calls are silent no-ops.

Usage by components (vektra_ingest, vektra_core, etc.):
    from vektra_shared.audit import log_event
    log_event(key_id=..., endpoint=..., action="ingest_complete", ...)

Injection at startup (infra-app-entrypoint lifespan):
    import vektra_shared.audit
    from vektra_admin.audit import log_event as _impl
    vektra_shared.audit.set_log_fn(_impl)
"""

from collections.abc import Callable
from typing import Any

_log_fn: Callable[..., None] | None = None


def set_log_fn(fn: Callable[..., None]) -> None:
    """Register the audit log writer. Called once at startup by infra-app-entrypoint."""
    global _log_fn
    _log_fn = fn


def log_event(**kwargs: Any) -> None:
    """Write an audit event. No-op if called before startup injection."""
    if _log_fn is not None:
        _log_fn(**kwargs)
