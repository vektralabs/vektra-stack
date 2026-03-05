"""Safeguard factory for selecting SafeguardHook implementations.

Phase 1: PassthroughSafeguard (no-op, from vektra_shared).
Phase 2: PresidioPIISafeguard (Presidio-based PII anonymization).
"""

from __future__ import annotations

import structlog

from vektra_shared.protocols import SafeguardHook
from vektra_shared.safeguards import PassthroughSafeguard

log = structlog.get_logger(__name__)


def create_safeguard(mode: str) -> SafeguardHook:
    """Create a SafeguardHook implementation based on the configured mode.

    Args:
        mode: "passthrough" or "presidio".

    Returns:
        A SafeguardHook implementation.
    """
    if mode == "presidio":
        try:
            from vektra_core.safeguards.presidio import PresidioPIISafeguard

            return PresidioPIISafeguard()
        except Exception as exc:
            log.warning(
                "safeguard_presidio_unavailable",
                error=str(exc),
                fallback="passthrough",
            )
            return PassthroughSafeguard()

    if mode != "passthrough":
        log.warning("safeguard_unknown_mode", mode=mode, fallback="passthrough")

    return PassthroughSafeguard()
