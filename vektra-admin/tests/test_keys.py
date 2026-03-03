"""Unit tests: API key generation and hash verification (REQ-023, ARCH-023)."""

import pytest

from vektra_admin.keys import generate_key, needs_rehash, verify_key


def test_generate_key_returns_three_tuple():
    plaintext, key_hash, key_preview = generate_key()
    assert isinstance(plaintext, str)
    assert isinstance(key_hash, str)
    assert isinstance(key_preview, str)


def test_key_preview_is_last_four_chars():
    plaintext, _, key_preview = generate_key()
    assert key_preview == plaintext[-4:]
    assert len(key_preview) == 4


@pytest.mark.asyncio
async def test_verify_key_valid():
    plaintext, key_hash, _ = generate_key()
    assert await verify_key(plaintext, key_hash) is True


@pytest.mark.asyncio
async def test_verify_key_wrong_plaintext():
    _, key_hash, _ = generate_key()
    assert await verify_key("wrong_token", key_hash) is False


@pytest.mark.asyncio
async def test_verify_key_does_not_raise():
    # Should return False rather than raising on a malformed hash
    assert await verify_key("token", "not_a_valid_hash") is False


def test_needs_rehash_fresh_hash():
    _, key_hash, _ = generate_key()
    # A freshly generated hash with current parameters should not need rehash
    assert needs_rehash(key_hash) is False


def test_plaintext_uniqueness():
    keys = [generate_key()[0] for _ in range(5)]
    assert len(set(keys)) == 5, "Generated keys must be unique"


@pytest.mark.asyncio
async def test_lru_cache_hit_on_second_verify():
    """Verify that repeated lookups use the LRU cache (no exception, same result)."""
    plaintext, key_hash, _ = generate_key()
    result1 = await verify_key(plaintext, key_hash)
    result2 = await verify_key(plaintext, key_hash)
    assert result1 is True
    assert result2 is True
