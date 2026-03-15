"""Unit tests: sliding window rate limiter (EX-013).

Tests cover under-limit allows, at-limit rejects, window sliding,
NULL rpm (unlimited), and response header correctness.
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

from vektra_admin.rate_limit import RateLimiter


class TestRateLimiterBasic:
    def test_first_request_allowed(self):
        """First request under the limit should be allowed."""
        rl = RateLimiter()
        key_id = uuid4()
        allowed, _headers = rl.check(key_id, rpm_limit=10)
        assert allowed is True

    def test_under_limit_allows_all(self):
        """Multiple requests under the limit should all be allowed."""
        rl = RateLimiter()
        key_id = uuid4()
        for _ in range(5):
            allowed, _ = rl.check(key_id, rpm_limit=10)
            assert allowed is True

    def test_at_limit_rejects(self):
        """Requests at exactly the limit should be rejected."""
        rl = RateLimiter()
        key_id = uuid4()
        rpm = 3

        # 3 allowed requests
        for _ in range(rpm):
            allowed, _ = rl.check(key_id, rpm_limit=rpm)
            assert allowed is True

        # 4th request rejected
        allowed, _headers = rl.check(key_id, rpm_limit=rpm)
        assert allowed is False

    def test_different_keys_independent(self):
        """Rate limits are per-key, not global."""
        rl = RateLimiter()
        key1 = uuid4()
        key2 = uuid4()
        rpm = 2

        # Exhaust key1
        rl.check(key1, rpm_limit=rpm)
        rl.check(key1, rpm_limit=rpm)
        allowed, _ = rl.check(key1, rpm_limit=rpm)
        assert allowed is False

        # key2 should still work
        allowed, _ = rl.check(key2, rpm_limit=rpm)
        assert allowed is True


class TestRateLimiterNullRpm:
    def test_null_rpm_means_unlimited(self):
        """When rpm_limit is None, all requests are allowed."""
        rl = RateLimiter()
        key_id = uuid4()
        for _ in range(100):
            allowed, headers = rl.check(key_id, rpm_limit=None)
            assert allowed is True
            assert headers == {}

    def test_null_rpm_returns_empty_headers(self):
        """Unlimited keys should not return rate limit headers."""
        rl = RateLimiter()
        _, headers = rl.check(uuid4(), rpm_limit=None)
        assert "X-RateLimit-Limit" not in headers
        assert "X-RateLimit-Remaining" not in headers
        assert "X-RateLimit-Reset" not in headers


class TestRateLimiterHeaders:
    def test_headers_present_on_allowed(self):
        """Allowed requests should include X-RateLimit-* headers."""
        rl = RateLimiter()
        _, headers = rl.check(uuid4(), rpm_limit=10)
        assert "X-RateLimit-Limit" in headers
        assert "X-RateLimit-Remaining" in headers
        assert "X-RateLimit-Reset" in headers

    def test_limit_header_matches_rpm(self):
        """X-RateLimit-Limit should match the configured rpm_limit."""
        rl = RateLimiter()
        _, headers = rl.check(uuid4(), rpm_limit=42)
        assert headers["X-RateLimit-Limit"] == "42"

    def test_remaining_decreases(self):
        """X-RateLimit-Remaining should decrease with each request."""
        rl = RateLimiter()
        key_id = uuid4()
        rpm = 5

        _, h1 = rl.check(key_id, rpm_limit=rpm)
        _, h2 = rl.check(key_id, rpm_limit=rpm)

        r1 = int(h1["X-RateLimit-Remaining"])
        r2 = int(h2["X-RateLimit-Remaining"])
        assert r2 < r1

    def test_remaining_zero_on_rejection(self):
        """X-RateLimit-Remaining should be 0 when rejected."""
        rl = RateLimiter()
        key_id = uuid4()

        rl.check(key_id, rpm_limit=1)
        allowed, headers = rl.check(key_id, rpm_limit=1)
        assert allowed is False
        assert headers["X-RateLimit-Remaining"] == "0"

    def test_reset_header_is_integer(self):
        """X-RateLimit-Reset should be a parseable integer (Unix timestamp)."""
        rl = RateLimiter()
        _, headers = rl.check(uuid4(), rpm_limit=10)
        int(headers["X-RateLimit-Reset"])  # should not raise


class TestRateLimiterSlidingWindow:
    def test_old_requests_expire(self):
        """After the window slides past old requests, new ones are allowed."""
        rl = RateLimiter()
        key_id = uuid4()
        rpm = 2

        # Exhaust limit
        rl.check(key_id, rpm_limit=rpm)
        rl.check(key_id, rpm_limit=rpm)
        allowed, _ = rl.check(key_id, rpm_limit=rpm)
        assert allowed is False

        # Simulate time passing past the window (61 seconds)
        with patch("vektra_admin.rate_limit.time") as mock_time:
            # Set monotonic to a time 61 seconds in the future
            mock_time.monotonic.return_value = 1e9 + 61.0

            # Manually adjust window entries to be in the past
            window = rl._windows[key_id]
            # Replace entries with old timestamps
            window.clear()
            window.append(1e9 - 60.0)  # 121 seconds ago from mock time
            window.append(1e9 - 59.0)  # 120 seconds ago from mock time

            allowed, _ = rl.check(key_id, rpm_limit=rpm)
            assert allowed is True


class TestRateLimiterCleanup:
    def test_cleanup_removes_stale_windows(self):
        """cleanup() should remove windows with no recent requests."""
        rl = RateLimiter()
        key1 = uuid4()
        key2 = uuid4()

        rl.check(key1, rpm_limit=10)
        rl.check(key2, rpm_limit=10)
        assert len(rl._windows) == 2

        # Make key1's window stale
        rl._windows[key1].clear()

        removed = rl.cleanup()
        assert removed == 1
        assert key1 not in rl._windows
        assert key2 in rl._windows

    def test_cleanup_empty_returns_zero(self):
        """cleanup() on empty limiter returns 0."""
        rl = RateLimiter()
        assert rl.cleanup() == 0
