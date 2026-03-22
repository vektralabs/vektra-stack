"""Safeguard factory for selecting SafeguardHook implementations.

PassthroughSafeguard (no-op, from vektra_shared).
PresidioPIISafeguard (Presidio-based PII anonymization).
"""

from __future__ import annotations

import structlog

from vektra_shared.protocols import SafeguardHook
from vektra_shared.safeguards import PassthroughSafeguard

log = structlog.get_logger(__name__)


def create_safeguard(mode: str, *, pii_chunk_threshold: int = 3) -> SafeguardHook:
    """Create a SafeguardHook implementation based on the configured mode.

    Args:
        mode: "passthrough" or "presidio".
        pii_chunk_threshold: PII entity count threshold for post_retrieval filtering.

    Returns:
        A SafeguardHook implementation.
    """
    if mode == "presidio":
        try:
            from vektra_core.safeguards.presidio import PresidioPIISafeguard

            return PresidioPIISafeguard(pii_chunk_threshold=pii_chunk_threshold)
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
