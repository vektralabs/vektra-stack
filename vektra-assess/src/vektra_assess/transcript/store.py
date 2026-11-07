"""Transcript bytes at rest: per-transcript DEK under the transcript key (ARCH-071, ADR-0031).

Both processes hold the transcript keyring (``assess_transcript_kek``): the worker seals at
generation, the API opens at download. ``seal`` returns the envelope, the wrapped DEK, the kid
and the SHA-256 of the plaintext; ``open_verified`` decrypts and refuses bytes that do not
re-hash to the stored value (``TranscriptIntegrityError``), so a download is never served from a
row that does not match what was generated. Purge crypto-shreds by deleting ``dek_wrapped``.

The API imports this module and ``crypto.transcript`` only; nothing here touches SMTP.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag

from vektra_assess.crypto.envelope import EnvelopeError
from vektra_assess.crypto.kek import KeyNotAvailable
from vektra_assess.crypto.transcript import decrypt_transcript, encrypt_transcript
from vektra_assess.keys import AssessKeyring

KEYRING = "assess_transcript_kek"


class TranscriptIntegrityError(ValueError):
    """The stored transcript cannot be opened or does not match its hash (content-free)."""


@dataclass(frozen=True)
class Sealed:
    ciphertext: bytes
    dek_wrapped: bytes
    kid: str
    content_sha256: str
    byte_size: int


def seal(kek: AssessKeyring, transcript_id: object, rendered: bytes) -> Sealed:
    ciphertext, wrapped, kid = encrypt_transcript(kek, transcript_id, rendered)
    return Sealed(
        ciphertext, wrapped, kid, hashlib.sha256(rendered).hexdigest(), len(rendered)
    )


def open_verified(
    kek: AssessKeyring,
    transcript_id: object,
    ciphertext: bytes,
    dek_wrapped: bytes,
    content_sha256: str,
) -> bytes:
    try:
        plain = decrypt_transcript(kek, transcript_id, ciphertext, dek_wrapped)
    except (InvalidTag, EnvelopeError, KeyNotAvailable):
        raise TranscriptIntegrityError("decrypt_failed") from None
    if hashlib.sha256(plain).hexdigest() != content_sha256:
        raise TranscriptIntegrityError("hash_mismatch")
    return plain
