"""The crypto self-test passes on a working build and refuses startup otherwise."""

from __future__ import annotations

import sys

import pytest

from vektra_assess.crypto import selftest
from vektra_shared.startup import StartupValidationError


def test_selftest_passes() -> None:
    selftest.run_crypto_selftest()


def test_missing_cryptography_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    # None in sys.modules makes the import raise ImportError: a build without the wheel.
    monkeypatch.setitem(
        sys.modules, "cryptography.hazmat.primitives.ciphers.aead", None
    )
    with pytest.raises(StartupValidationError) as exc:
        selftest.run_crypto_selftest()
    assert exc.value.step == "assess_crypto_selftest"
    assert "ModuleNotFoundError" in exc.value.detail


def test_broken_aead_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.hazmat.primitives.ciphers import aead

    real = aead.AESGCM

    class BrokenAESGCM:  # the Rust class is final: wrap it instead of subclassing
        generate_key = staticmethod(real.generate_key)

        def __init__(self, key: bytes) -> None:
            self._inner = real(key)

        def encrypt(self, nonce, data, associated_data):  # type: ignore[no-untyped-def]
            return self._inner.encrypt(nonce, data, associated_data)

        def decrypt(self, nonce, data, associated_data):  # type: ignore[no-untyped-def]
            return b"wrong"

    monkeypatch.setattr(aead, "AESGCM", BrokenAESGCM)
    with pytest.raises(StartupValidationError, match="AES-256-GCM round trip mismatch"):
        selftest.run_crypto_selftest()


def test_broken_x25519_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.hazmat.primitives.asymmetric import x25519

    real = x25519.X25519PrivateKey.generate
    calls = {"n": 0}

    class Fake:
        def __init__(self) -> None:
            self._k = real()

        def public_key(self):  # type: ignore[no-untyped-def]
            return self._k.public_key()

        def exchange(self, peer):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            return bytes(32) if calls["n"] == 1 else self._k.exchange(peer)

    monkeypatch.setattr(x25519.X25519PrivateKey, "generate", staticmethod(Fake))
    with pytest.raises(StartupValidationError, match="X25519 exchange mismatch"):
        selftest.run_crypto_selftest()
