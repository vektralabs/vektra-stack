"""Crypto startup self-test (ARCH-078, ADR-0031 amended): fail closed.

``cryptography`` is a hard dependency with no weaker fallback. The assess API and worker run
this before serving: one AES-256-GCM round trip, one X25519 exchange and one HKDF derivation.
Import alone does not exercise the OpenSSL code paths that would fault on a CPU lacking an
instruction (BUG-027), so the operations are executed, and their results checked.
"""

from __future__ import annotations

import os

from vektra_shared.startup import StartupValidationError

STEP = "assess_crypto_selftest"
_PLAINTEXT = b"vektra-assess crypto self-test"
_AAD = b"selftest"


def run_crypto_selftest() -> None:
    """Raise ``StartupValidationError`` unless the crypto primitives work in this process."""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        key = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(12)
        ct = AESGCM(key).encrypt(nonce, _PLAINTEXT, _AAD)
        if ct == _PLAINTEXT or AESGCM(key).decrypt(nonce, ct, _AAD) != _PLAINTEXT:
            raise ValueError("AES-256-GCM round trip mismatch")

        a, b = X25519PrivateKey.generate(), X25519PrivateKey.generate()
        shared = a.exchange(b.public_key())
        if len(shared) != 32 or shared != b.exchange(a.public_key()):
            raise ValueError("X25519 exchange mismatch")

        okm = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_AAD).derive(
            shared
        )
        if len(okm) != 32:
            raise ValueError("HKDF output length mismatch")
    except StartupValidationError:
        raise
    except Exception as exc:  # any failure, import included, refuses startup
        raise StartupValidationError(
            step=STEP,
            detail=f"cryptography self-test failed: {type(exc).__name__}: {exc}",
            remediation=(
                "The assess services require a working 'cryptography' (AES-GCM, X25519, "
                "HKDF); there is no fallback. Rebuild the image so the wheel loads on this "
                "CPU (see the BUG-027 step in .github/workflows/integration.yml)."
            ),
        ) from exc
