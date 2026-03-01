"""EventEmitter implementations (ARCH-038).

Phase 1: NoOpEventEmitter (events silently discarded).
Phase 2: WebhookEventEmitter (HMAC-SHA256 signed HTTP POST).

Emission points:
    document.indexed    - successful document ingest
    document.failed     - document ingest failure
    query.completed     - query pipeline execution complete
    safeguard.triggered - a safeguard hook blocked or modified a request
    apikey.created      - new API key created
    apikey.revoked      - API key revoked
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from vektra_shared.config import WebhookConfig

__all__ = ["NoOpEventEmitter", "WebhookEventEmitter"]

logger = structlog.get_logger(__name__)


class NoOpEventEmitter:
    """EventEmitter that discards all events with no side effects.

    Implements the EventEmitter Protocol. Safe to use in all environments.
    """

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Discard the event. No I/O, no side effects."""
        return


class WebhookEventEmitter:
    """EventEmitter that sends HMAC-SHA256 signed HTTP POST to a webhook URL.

    HTTP failures are logged but never raised to the caller. The ``emit``
    method awaits the HTTP call (bounded by ``timeout_seconds``), so callers
    should account for network latency. No retries in Phase 2 (retry queue
    deferred to Phase 3).
    """

    def __init__(self, config: WebhookConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = (
            httpx.AsyncClient(timeout=config.timeout_seconds) if config.url else None
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None:
            await self._client.aclose()

    def _sign(self, body: bytes, secret: str) -> str:
        """Compute HMAC-SHA256 hex digest of the request body."""
        return hmac.new(
            secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Send event as HTTP POST. Failures are logged, never raised."""
        url = self._config.url
        if self._client is None or url is None:
            return

        body_dict = {
            "event_type": event_type,
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": payload,
        }
        body = json.dumps(body_dict, default=str).encode()

        headers: dict[str, str] = {"Content-Type": "application/json"}
        secret = self._config.secret
        if secret:
            headers["X-Vektra-Signature-256"] = f"sha256={self._sign(body, secret)}"

        try:
            response = await self._client.post(
                url,
                content=body,
                headers=headers,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "webhook_delivery_failed",
                event_type=event_type,
                status_code=exc.response.status_code,
                url=url,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "webhook_delivery_error",
                event_type=event_type,
                error=str(exc),
                url=url,
            )
