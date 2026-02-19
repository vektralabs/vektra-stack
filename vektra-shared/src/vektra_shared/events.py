"""NoOpEventEmitter: Phase 1 default EventEmitter implementation (ARCH-038).

All events are silently discarded with <1ms overhead.

Emission points:
    document.indexed    - successful document ingest
    document.failed     - document ingest failure
    query.completed     - query pipeline execution complete
    safeguard.triggered - a safeguard hook blocked or modified a request
    apikey.created      - new API key created
    apikey.revoked      - API key revoked

Phase 2: WebhookEventEmitter with HMAC-SHA256 signatures.
"""

from __future__ import annotations

from typing import Any


class NoOpEventEmitter:
    """EventEmitter that discards all events with no side effects.

    Implements the EventEmitter Protocol. Safe to use in all environments.
    """

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Discard the event. No I/O, no side effects."""
        return
