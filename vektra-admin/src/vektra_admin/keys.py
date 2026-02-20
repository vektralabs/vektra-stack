"""API key generation, hashing, and verification (REQ-023, ARCH-023).

Key lifecycle:
- Generation: 32 random bytes → URL-safe base64 (43 chars + '=' padding stripped).
- Preview: last 4 characters of the plaintext key (stored, returned in listings).
- Hash: argon2id via argon2-cffi (never store plaintext after generation).
- Verification: argon2id verify on every authenticated request; LRU cache avoids
  per-request hashing overhead for recently seen keys.
"""

from __future__ import annotations

import secrets
from base64 import urlsafe_b64encode
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# Single PasswordHasher instance (argon2id, default time_cost=3, memory_cost=65536)
_ph = PasswordHasher()

# LRU cache: maps (key_hash, plaintext_key) → True (verified) or raises on miss.
# Cache size 512 is generous for a single-process deployment. Cache entries are
# keyed by (hash, plaintext) so a different hash for the same plaintext won't
# produce a false positive.
_CACHE_SIZE = 512


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


def verify_key(plaintext: str, key_hash: str) -> bool:
    """Verify a plaintext key against its stored hash.

    Uses an LRU cache to avoid re-running argon2id on every request for
    recently verified keys. Returns False on any verification failure;
    never raises (callers treat False as 401).
    """
    return _cached_verify(key_hash, plaintext)


@lru_cache(maxsize=_CACHE_SIZE)
def _cached_verify(key_hash: str, plaintext: str) -> bool:
    """Internal cached verification. Keyed by (hash, plaintext)."""
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
