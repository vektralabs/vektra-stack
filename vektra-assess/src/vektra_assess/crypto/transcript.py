"""Per-transcript DEK, wrapped by the transcript KEK keyring (plan assess-data-crypto 4.5).

The transcript is encrypted with a fresh DEK; the DEK is wrapped under the active key of
``assess_transcript_kek`` (held by API and worker, the API serves the download). Shredding a
transcript deletes the wrapped DEK and the row, after which the ciphertext is unreadable.
"""

from __future__ import annotations

from vektra_assess.crypto.envelope import (
    DEK_KID,
    Purpose,
    aead_decrypt,
    aead_encrypt,
    new_dek,
    peek_kid,
)
from vektra_assess.crypto.kek import key_for
from vektra_assess.keys import AssessKeyring


def encrypt_transcript(
    kek: AssessKeyring, transcript_id: object, plaintext: bytes
) -> tuple[bytes, bytes, str]:
    """Returns ``(ciphertext, dek_wrapped, kek_kid)``."""
    dek = new_dek()
    ciphertext = aead_encrypt(
        dek, DEK_KID, plaintext, purpose=Purpose.TRANSCRIPT, binding=[transcript_id]
    )
    dek_wrapped = aead_encrypt(
        kek.active_key,
        kek.active,
        dek,
        purpose=Purpose.TRANSCRIPT_DEK,
        binding=[transcript_id],
    )
    return ciphertext, dek_wrapped, kek.active


def unwrap_transcript_dek(
    kek: AssessKeyring, transcript_id: object, dek_wrapped: bytes
) -> bytes:
    return aead_decrypt(
        key_for(kek, peek_kid(dek_wrapped)),
        dek_wrapped,
        purpose=Purpose.TRANSCRIPT_DEK,
        binding=[transcript_id],
    )


def decrypt_transcript(
    kek: AssessKeyring, transcript_id: object, ciphertext: bytes, dek_wrapped: bytes
) -> bytes:
    dek = unwrap_transcript_dek(kek, transcript_id, dek_wrapped)
    return aead_decrypt(
        dek, ciphertext, purpose=Purpose.TRANSCRIPT, binding=[transcript_id]
    )
