"""Versioned AEAD envelope (ARCH-077/079, plan assess-data-crypto 4.1).

Wire format of every ciphertext column::

    version(1) | kid_len(1) | kid | nonce(12) | ciphertext+tag

AES-256-GCM, 96-bit random nonce. The AAD is always
``b"vektra-assess/v1/" + purpose + b"/" + binding`` so a ciphertext copied to another row
or column fails authentication (row-swap defence against an actor with DB write access).

=================  ==================================
purpose            row binding
=================  ==================================
``snapshot``       session_id
``turn``           session_id, turn_no
``dek-wrap``       session_id, key_version
``handoff``        session_id, handoff kid
``transcript``     transcript_id
``transcript-dek`` transcript_id
``teacher-config`` site_id, cmid, field
=================  ==================================

Nothing here logs, and no object defined here has a repr containing key bytes.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = [
    "VERSION",
    "EnvelopeError",
    "InvalidTag",
    "Purpose",
    "aad",
    "aead_decrypt",
    "aead_encrypt",
    "new_dek",
    "peek_kid",
]

VERSION: Final = 1
NONCE_BYTES: Final = 12
KEY_BYTES: Final = 32
_PREFIX: Final = b"vektra-assess/v1/"
#: the kid written on envelopes encrypted directly with a session or transcript DEK
DEK_KID: Final = "dek"


class EnvelopeError(ValueError):
    """The blob is not a well-formed envelope (never raised for a bad tag: see InvalidTag)."""


class Purpose:
    SNAPSHOT: Final = "snapshot"
    TURN: Final = "turn"
    DEK_WRAP: Final = "dek-wrap"
    HANDOFF: Final = "handoff"
    TRANSCRIPT: Final = "transcript"
    TRANSCRIPT_DEK: Final = "transcript-dek"
    TEACHER_CONFIG: Final = "teacher-config"
    ALL: Final = frozenset(
        {
            "snapshot",
            "turn",
            "dek-wrap",
            "handoff",
            "transcript",
            "transcript-dek",
            "teacher-config",
        }
    )


def aad(purpose: str, binding: Sequence[object]) -> bytes:
    """The additional authenticated data for ``purpose`` bound to a row."""
    if purpose not in Purpose.ALL:
        raise ValueError(f"unknown envelope purpose {purpose!r}")
    parts = [str(p) for p in binding]
    if not parts or any(not p or "/" in p for p in parts):
        raise ValueError("binding parts must be non-empty and must not contain '/'")
    return _PREFIX + purpose.encode("ascii") + b"/" + "/".join(parts).encode("utf-8")


def new_dek() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def _check_key(key: bytes) -> None:
    if len(key) != KEY_BYTES:
        raise ValueError(f"AES-256-GCM key must be {KEY_BYTES} bytes")


def pack(kid: str, nonce: bytes, ciphertext: bytes) -> bytes:
    kid_b = kid.encode("utf-8")
    if not kid_b or len(kid_b) > 255:
        raise ValueError("kid must be 1..255 bytes")
    return bytes([VERSION, len(kid_b)]) + kid_b + nonce + ciphertext


def unpack(blob: bytes) -> tuple[str, bytes, bytes]:
    if len(blob) < 2 or blob[0] != VERSION:
        raise EnvelopeError("unsupported envelope version")
    kid_len = blob[1]
    start = 2 + kid_len
    if kid_len == 0 or len(blob) < start + NONCE_BYTES + 16:
        raise EnvelopeError("truncated envelope")
    try:
        kid = blob[2:start].decode("utf-8")
    except UnicodeDecodeError:
        raise EnvelopeError("kid is not UTF-8") from None
    return kid, blob[start : start + NONCE_BYTES], blob[start + NONCE_BYTES :]


def peek_kid(blob: bytes) -> str:
    """The kid an envelope claims (unauthenticated: use it only to pick a key)."""
    return unpack(blob)[0]


def aead_encrypt(
    key: bytes,
    kid: str,
    plaintext: bytes,
    *,
    purpose: str,
    binding: Sequence[object],
) -> bytes:
    _check_key(key)
    nonce = os.urandom(NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad(purpose, binding))
    return pack(kid, nonce, ct)


def aead_decrypt(
    key: bytes, blob: bytes, *, purpose: str, binding: Sequence[object]
) -> bytes:
    """Decrypt ``blob``; raises ``InvalidTag`` for any tampering or wrong binding."""
    _check_key(key)
    _kid, nonce, ct = unpack(blob)
    return AESGCM(key).decrypt(nonce, ct, aad(purpose, binding))


# Session data, encrypted directly with the session DEK ---------------------------------


def encrypt_snapshot(dek: bytes, session_id: object, plaintext: bytes) -> bytes:
    return aead_encrypt(
        dek, DEK_KID, plaintext, purpose=Purpose.SNAPSHOT, binding=[session_id]
    )


def decrypt_snapshot(dek: bytes, session_id: object, blob: bytes) -> bytes:
    return aead_decrypt(dek, blob, purpose=Purpose.SNAPSHOT, binding=[session_id])


def encrypt_turn(
    dek: bytes, session_id: object, turn_no: int, plaintext: bytes
) -> bytes:
    return aead_encrypt(
        dek, DEK_KID, plaintext, purpose=Purpose.TURN, binding=[session_id, turn_no]
    )


def decrypt_turn(dek: bytes, session_id: object, turn_no: int, blob: bytes) -> bytes:
    return aead_decrypt(dek, blob, purpose=Purpose.TURN, binding=[session_id, turn_no])
