"""Safeguard factory for selecting SafeguardHook implementations.

Phase 1: PassthroughSafeguard (no-op, from vektra_shared).
Phase 2: PresidioPIISafeguard (Presidio-based PII anonymization).
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
    normalized = mode.strip().lower()

    if normalized == "passthrough":
        return PassthroughSafeguard()

    if normalized == "presidio":
        from vektra_core.safeguards.presidio import PresidioPIISafeguard

        return PresidioPIISafeguard(pii_chunk_threshold=pii_chunk_threshold)

    raise ValueError(
        f"Unsupported safeguard mode: '{mode}'. Valid modes: 'passthrough', 'presidio'."
    )
