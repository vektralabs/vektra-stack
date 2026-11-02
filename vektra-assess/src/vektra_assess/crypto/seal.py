"""X25519 sealing of a payload to a recipient public key (plan assess-data-crypto 4.3).

HPKE-style: an ephemeral X25519 key, HKDF-SHA256 over the shared secret with
``salt = eph_pub | recipient_pub`` and ``info = b"vektra-assess/seal/v1/" + purpose + b"/" +
kid``, then AES-256-GCM with the envelope AAD of ``purpose`` and the row binding.

Wire format::

    version(1) | kid_len(1) | kid | eph_pub(32) | nonce(12) | ciphertext+tag

The construction is built from primitives on purpose, even though the locked
``cryptography`` ships an HPKE module: one stable, documented wire format that does not
depend on a library API for its on-disk shape (so a library bump is never a data migration).
Two purposes use it: ``handoff`` (the API's public key sealing a DEK) and ``teacher-config``
(the worker's public key sealing the solution or pasted rubric).
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from vektra_assess.crypto.envelope import (
    NONCE_BYTES,
    VERSION,
    aad,
    read_header,
)
from vektra_assess.crypto.kek import key_for
from vektra_assess.keys import AssessKeyring

_INFO = b"vektra-assess/seal/v1/"
_PUB = 32


def _derive(
    shared: bytes, eph_pub: bytes, recipient_pub: bytes, purpose: str, kid: str
) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=eph_pub + recipient_pub,
        info=_INFO + purpose.encode("ascii") + b"/" + kid.encode("utf-8"),
    ).derive(shared)


def _seal(
    eph_priv: X25519PrivateKey,
    nonce: bytes,
    recipient_pub: bytes,
    kid: str,
    plaintext: bytes,
    purpose: str,
    binding: Sequence[object],
) -> bytes:
    kid_b = kid.encode("utf-8")
    if not kid_b or len(kid_b) > 255:  # validate before any DH or encryption work
        raise ValueError("kid must be 1..255 bytes")
    eph_pub = eph_priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    shared = eph_priv.exchange(X25519PublicKey.from_public_bytes(recipient_pub))
    key = _derive(shared, eph_pub, recipient_pub, purpose, kid)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad(purpose, binding))
    return bytes([VERSION, len(kid_b)]) + kid_b + eph_pub + nonce + ct


def seal(
    recipient_pub: bytes,
    kid: str,
    plaintext: bytes,
    *,
    purpose: str,
    binding: Sequence[object],
) -> bytes:
    """Seal ``plaintext`` so that only the holder of the matching private key can open it."""
    return _seal(
        X25519PrivateKey.generate(),
        os.urandom(NONCE_BYTES),
        recipient_pub,
        kid,
        plaintext,
        purpose,
        binding,
    )


def public_from_private(raw_private: bytes) -> bytes:
    return (
        X25519PrivateKey.from_private_bytes(raw_private)
        .public_key()
        .public_bytes(Encoding.Raw, PublicFormat.Raw)
    )


def open_sealed(
    private_keyring: AssessKeyring,
    blob: bytes,
    *,
    purpose: str,
    binding: Sequence[object],
) -> bytes:
    """Open a sealed blob with the private key its kid names. ``InvalidTag`` on tampering."""
    kid, start = read_header(blob, _PUB + NONCE_BYTES + 16)
    eph_pub = blob[start : start + _PUB]
    nonce = blob[start + _PUB : start + _PUB + NONCE_BYTES]
    ct = blob[start + _PUB + NONCE_BYTES :]
    raw_priv = key_for(private_keyring, kid)
    priv = X25519PrivateKey.from_private_bytes(raw_priv)
    recipient_pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    shared = priv.exchange(X25519PublicKey.from_public_bytes(eph_pub))
    key = _derive(shared, eph_pub, recipient_pub, purpose, kid)
    return AESGCM(key).decrypt(nonce, ct, aad(purpose, binding))


__all__ = ["open_sealed", "public_from_private", "seal"]
