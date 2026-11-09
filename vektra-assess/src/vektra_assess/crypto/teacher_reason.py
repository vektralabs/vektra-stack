"""Teacher-entered reasons of a reset or a termination (plan assess-teacher-ops 11b, REQ-087).

The reason is the teacher's own text: it appears only in the grader list (REQ-076) and is
reachable from the audit row through the reset / termination id, never written into the audit
table, a transcript, a metric or a log. It is encrypted directly under the transcript KEK
(``assess_transcript_kek``, held by the API, which serves the list, and the worker), bound to its
table and row id, so a ciphertext copied to another row or table fails authentication. The KEK
rotation re-encrypts it (``crypto.rotation.rewrap_teacher_reasons``).
"""

from __future__ import annotations

from typing import Literal

from vektra_assess.crypto.envelope import Purpose, aead_decrypt, aead_encrypt, peek_kid
from vektra_assess.crypto.kek import key_for
from vektra_assess.keys import AssessKeyring

#: the only tables holding a teacher reason
Table = Literal["assess_reset_grants", "assess_terminations"]
TABLES: tuple[Table, ...] = ("assess_reset_grants", "assess_terminations")


def encrypt_reason(
    kek: AssessKeyring, table: Table, row_id: object, reason: str
) -> tuple[bytes, str]:
    """``(ciphertext, kid)`` under the active transcript KEK."""
    if table not in TABLES:
        raise ValueError("not a teacher-reason table")
    blob = aead_encrypt(
        kek.active_key,
        kek.active,
        reason.encode("utf-8"),
        purpose=Purpose.TEACHER_REASON,
        binding=[table, row_id],
    )
    return blob, kek.active


def decrypt_reason(
    kek: AssessKeyring, table: Table, row_id: object, blob: bytes
) -> str:
    """Raises ``InvalidTag`` for a ciphertext of another row or table (or tampered)."""
    if table not in TABLES:
        raise ValueError("not a teacher-reason table")
    plain = aead_decrypt(
        key_for(kek, peek_kid(blob)),
        blob,
        purpose=Purpose.TEACHER_REASON,
        binding=[table, row_id],
    )
    return plain.decode("utf-8")
