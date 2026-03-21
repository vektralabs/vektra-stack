"""Unit tests: TTLCache replacement for key verification (DEBT-008).

Verifies cache hit, miss, configuration, and thread safety.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from vektra_admin.keys import (
    _CACHE_TTL,
    _cache_lock,
    _verify_cache,
    generate_key,
    verify_key,
)


def _clear_cache() -> None:
    """Clear the module-level TTLCache between tests."""
    with _cache_lock:
        _verify_cache.clear()


class TestTTLCacheHitMiss:
    def setup_method(self) -> None:
        _clear_cache()

    def teardown_method(self) -> None:
        _clear_cache()

    @pytest.mark.asyncio
    async def test_cache_populated_after_verify(self):
        """After verify_key, the cache should contain the (hash, plaintext) entry."""
        plaintext, key_hash, _ = generate_key()
        await verify_key(plaintext, key_hash)

        with _cache_lock:
            assert (key_hash, plaintext) in _verify_cache
            assert _verify_cache[(key_hash, plaintext)] is True

    @pytest.mark.asyncio
    async def test_failed_verify_not_cached(self):
        """Failed verifications should NOT be cached (prevents cache poisoning)."""
        _, key_hash, _ = generate_key()
        wrong_token = "wrong_token_abc"
        await verify_key(wrong_token, key_hash)

        with _cache_lock:
            assert (key_hash, wrong_token) not in _verify_cache

    @pytest.mark.asyncio
    async def test_cache_hit_returns_same_result(self):
        """Repeated verify_key calls return the same result from cache."""
        plaintext, key_hash, _ = generate_key()
        result1 = await verify_key(plaintext, key_hash)
        result2 = await verify_key(plaintext, key_hash)
        assert result1 is True
        assert result2 is True

    @pytest.mark.asyncio
    async def test_different_keys_cached_independently(self):
        """Each (hash, plaintext) pair has its own cache entry."""
        pt1, hash1, _ = generate_key()
        pt2, hash2, _ = generate_key()

        await verify_key(pt1, hash1)
        await verify_key(pt2, hash2)

        with _cache_lock:
            assert (hash1, pt1) in _verify_cache
            assert (hash2, pt2) in _verify_cache
            assert len(_verify_cache) >= 2

    @pytest.mark.asyncio
    async def test_cache_cleared_means_miss(self):
        """After clearing, the next verify_key call is a cache miss."""
        plaintext, key_hash, _ = generate_key()
        await verify_key(plaintext, key_hash)

        with _cache_lock:
            assert (key_hash, plaintext) in _verify_cache

        _clear_cache()

        with _cache_lock:
            assert (key_hash, plaintext) not in _verify_cache

        # Re-verify should repopulate
        await verify_key(plaintext, key_hash)
        with _cache_lock:
            assert (key_hash, plaintext) in _verify_cache


class TestTTLCacheConfig:
    def test_cache_ttl_is_300_seconds(self):
        """TTL should be 300 seconds per DEBT-008 spec."""
        assert _CACHE_TTL == 300

    def test_cache_maxsize_is_512(self):
        """Cache maxsize should be 512."""
        assert _verify_cache.maxsize == 512

    def test_cache_ttl_matches_config(self):
        """Cache TTL value should match the module constant."""
        assert _verify_cache.ttl == _CACHE_TTL


class TestTTLCacheThreadSafety:
    def setup_method(self) -> None:
        _clear_cache()

    def teardown_method(self) -> None:
        _clear_cache()

    def test_concurrent_verify_same_key(self):
        """Multiple threads verifying the same key should not raise."""
        plaintext, key_hash, _ = generate_key()
        results: dict[int, bool] = {}
        errors: list[Exception] = []

        def worker(idx: int) -> None:
            try:
                results[idx] = asyncio.run(verify_key(plaintext, key_hash))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Threads raised exceptions: {errors}"
        assert all(v is True for v in results.values())
        assert len(results) == 10

    def test_concurrent_verify_different_keys(self):
        """Multiple threads verifying different keys should not corrupt cache."""
        keys = [generate_key() for _ in range(10)]
        results: dict[int, bool] = {}
        errors: list[Exception] = []

        def worker(idx: int, pt: str, kh: str) -> None:
            try:
                results[idx] = asyncio.run(verify_key(pt, kh))
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i, pt, kh))
            for i, (pt, kh, _) in enumerate(keys)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors
        assert all(v is True for v in results.values())
        assert len(results) == 10
