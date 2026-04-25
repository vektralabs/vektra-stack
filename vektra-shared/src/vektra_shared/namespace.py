"""Namespace configuration utilities shared across packages (FEAT-020, FEAT-014).

Uses raw SQL to avoid importing ORM models from other packages (ADR-0005).
Both vektra-core and vektra-learn can import from vektra-shared.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text

log = structlog.get_logger(__name__)

_VALID_GROUNDING_MODES = {"strict", "hybrid"}


async def resolve_grounding_mode(
    namespace: str,
    session_factory: Any,
    default_mode: str = "strict",
) -> str:
    """Resolve grounding_mode: namespace JSONB config > default.

    The *default_mode* parameter should incorporate the env var resolution
    (``VEKTRA_PROMPT_GROUNDING_MODE``) done by the caller.

    Returns *default_mode* on any error (namespace not found, DB error,
    invalid value in config).
    """
    try:
        async with session_factory() as session:
            result = await session.execute(
                text("SELECT config FROM namespaces WHERE id = :ns"),
                {"ns": namespace},
            )
            row = result.scalar_one_or_none()
            if row and isinstance(row, dict):
                ns_mode = row.get("grounding_mode")
                if ns_mode in _VALID_GROUNDING_MODES:
                    return str(ns_mode)
    except Exception as exc:
        log.debug(
            "grounding_mode_resolution_fallback",
            namespace=namespace,
            error=str(exc),
        )
    return default_mode


async def resolve_show_sources(
    namespace: str,
    session_factory: Any,
    default_value: bool = True,
) -> bool:
    """Resolve show_sources: namespace JSONB config > default (FEAT-014).

    The *default_value* parameter should incorporate the env var resolution
    (``VEKTRA_LEARN_SHOW_SOURCES``) done by the caller.

    Returns *default_value* on any error (namespace not found, DB error,
    non-boolean value in config).
    """
    try:
        async with session_factory() as session:
            result = await session.execute(
                text("SELECT config FROM namespaces WHERE id = :ns"),
                {"ns": namespace},
            )
            row = result.scalar_one_or_none()
            if row and isinstance(row, dict):
                ns_value = row.get("show_sources")
                if isinstance(ns_value, bool):
                    return ns_value
    except Exception as exc:
        log.debug(
            "show_sources_resolution_fallback",
            namespace=namespace,
            error=str(exc),
        )
    return default_value
