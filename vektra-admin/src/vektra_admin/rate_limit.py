"""In-memory sliding window rate limiter (EX-013).

Per-key rate limiting using a sliding window counter. Each API key
has a deque of request timestamps; requests older than 60 seconds
are evicted on each check.

For single-process deployments (Phase 1-2 target), in-memory is sufficient.
Multi-process deployments (Phase 3) would need Redis-backed rate limiting.
"""

from __future__ import annotations

import time
from collections import deque
from uuid import UUID

import structlog

log = structlog.get_logger(__name__)

_WINDOW_SECONDS = 60.0


class RateLimiter:
    """Sliding window rate limiter keyed by API key UUID."""

    def __init__(self) -> None:
        self._windows: dict[UUID, deque[float]] = {}

    def check(self, key_id: UUID, rpm_limit: int | None) -> tuple[bool, dict[str, str]]:
        """Check if a request is within the rate limit.

        Args:
            key_id: the API key UUID
            rpm_limit: requests per minute limit. None means unlimited.

        Returns:
            (allowed, headers) tuple. headers contains X-RateLimit-* values.
        """
        if rpm_limit is None:
            return True, {}

        now = time.monotonic()
        window_start = now - _WINDOW_SECONDS

        # Get or create window
        if key_id not in self._windows:
            self._windows[key_id] = deque()

        window = self._windows[key_id]

        # Evict expired entries
        while window and window[0] < window_start:
            window.popleft()

        current_count = len(window)
        remaining = max(0, rpm_limit - current_count)

        # Calculate reset time (when the oldest entry expires)
        if window:
            reset_at = window[0] + _WINDOW_SECONDS
        else:
            reset_at = now + _WINDOW_SECONDS

        # Convert monotonic reset_at to a Unix timestamp for the header.
        reset_unix = int(time.time() + (reset_at - now))

        headers = {
            "X-RateLimit-Limit": str(rpm_limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset_unix),
        }

        if current_count >= rpm_limit:
            log.info(
                "rate_limit_exceeded",
                key_id=str(key_id),
                rpm_limit=rpm_limit,
                current=current_count,
            )
            return False, headers

        # Record this request
        window.append(now)
        # Update remaining after recording
        headers["X-RateLimit-Remaining"] = str(remaining - 1)

        return True, headers

    def cleanup(self) -> int:
        """Remove windows for keys with no recent requests. Returns count removed."""
        now = time.monotonic()
        window_start = now - _WINDOW_SECONDS
        stale_keys = [
            kid
            for kid, window in self._windows.items()
            if not window or window[-1] < window_start
        ]
        for kid in stale_keys:
            del self._windows[kid]
        return len(stale_keys)
