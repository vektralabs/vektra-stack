"""API key generation, hashing, and verification (REQ-023, ARCH-023).

Key lifecycle:
- Generation: 32 random bytes -> URL-safe base64 (43 chars + '=' padding stripped).
- Preview: last 4 characters of the plaintext key (stored, returned in listings).
- Hash: argon2id via argon2-cffi (never store plaintext after generation).
- Verification: argon2id verify on every authenticated request; TTLCache avoids
  per-request hashing overhead for recently seen keys (DEBT-008: max 300s exposure).
"""

from __future__ import annotations

import asyncio
import secrets
import threading
from base64 import urlsafe_b64encode

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cachetools import TTLCache

# Single PasswordHasher instance (argon2id, default time_cost=3, memory_cost=65536)
_ph = PasswordHasher()

# TTLCache: maps (key_hash, plaintext_key) -> True (verified) or False (failed).
# Max 300s TTL limits plaintext key exposure in memory (DEBT-008).
# Cache size 512 is generous for a single-process deployment.
_CACHE_SIZE = 512
_CACHE_TTL = 300  # seconds

_verify_cache: TTLCache[tuple[str, str], bool] = TTLCache(
    maxsize=_CACHE_SIZE, ttl=_CACHE_TTL
)
_cache_lock = threading.Lock()


def generate_key() -> tuple[str, str, str]:
    """Generate a new API key.

    Returns:
        (plaintext_key, key_hash, key_preview) tuple.
        plaintext_key is returned ONCE; store only key_hash and key_preview.
    """
    raw = secrets.token_bytes(32)
    plaintext = urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    key_hash = _ph.hash(plaintext)
    key_preview = plaintext[-4:]
    return plaintext, key_hash, key_preview


def hash_key(plaintext: str) -> str:
    """Hash a plaintext key with argon2id. Used only for re-hashing if needed."""
    return _ph.hash(plaintext)


async def verify_key(plaintext: str, key_hash: str) -> bool:
    """Verify a plaintext key against its stored hash.

    Uses a TTLCache (300s) to avoid re-running argon2id on every request for
    recently verified keys. Returns False on any verification failure;
    never raises (callers treat False as 401).

    argon2id verification is CPU-bound; cache misses are offloaded to a
    thread via asyncio.to_thread to avoid blocking the event loop.
    """
    cache_key = (key_hash, plaintext)

    with _cache_lock:
        cached = _verify_cache.get(cache_key)
        if cached is not None:
            return bool(cached)

    # Cache miss: run argon2id verification in a thread (CPU-bound)
    result = await asyncio.to_thread(_verify_sync, key_hash, plaintext)

    if result:
        with _cache_lock:
            _verify_cache[cache_key] = True

    return result


def _verify_sync(key_hash: str, plaintext: str) -> bool:
    """Synchronous argon2id verify (runs in thread pool)."""
    try:
        _ph.verify(key_hash, plaintext)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(key_hash: str) -> bool:
    """Return True if the hash uses outdated argon2 parameters.

    Useful for future parameter upgrades; check on successful login and
    update key_hash in the database if True.
    """
    return _ph.check_needs_rehash(key_hash)
