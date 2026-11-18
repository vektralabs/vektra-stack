"""The read-back of one sealed teacher-config field, "Mostra" (FEAT-030, OQ-034 resolved 2026-11-18).

The API still holds NO private key of the teacher config (ARCH-071): for one read-back request
it generates an EPHEMERAL X25519 keypair in memory, sends the worker the public half (job args),
and the worker re-seals the opened field to it (``seal_reveal``). Only that request's process
can open the answer (``EphemeralKey.open``); the private half is never written, logged or sent,
and is gone when the request ends, so a row nobody read is undecryptable forever.

Same wire format and construction as every sealed value (``crypto.seal``), purpose
``config-reveal``, binding ``[site_id, cmid, field, request_id]``: an answer copied to another
site, activity, field or request fails authentication.
"""

from __future__ import annotations

import os
import string
from dataclasses import dataclass, field

from vektra_assess.crypto.envelope import Purpose
from vektra_assess.crypto.seal import open_sealed, public_from_private, seal
from vektra_assess.keys import AssessKeyring

#: the kid written on a read-back answer (the keyring that opens it has this one key only)
REVEAL_KID = "reveal"
_PUB_HEX = 64
_HEX = frozenset(string.hexdigits.lower())


def _binding(
    site_id: object, cmid: int, field_: str, request_id: object
) -> list[object]:
    return [site_id, int(cmid), field_, request_id]


def public_from_hex(value: object) -> bytes | None:
    """The 32-byte public key a job carries as 64 lowercase hex digits; None otherwise."""
    if not isinstance(value, str) or len(value) != _PUB_HEX or not set(value) <= _HEX:
        return None
    return bytes.fromhex(value)


@dataclass(frozen=True, slots=True)
class EphemeralKey:
    """One read-back's keypair, in memory only. The repr shows nothing of either half."""

    _private: bytes = field(repr=False)
    public: bytes = field(repr=False)

    @classmethod
    def generate(cls) -> EphemeralKey:
        raw = os.urandom(32)  # X25519 clamps any 32 bytes
        return cls(raw, public_from_private(raw))

    @property
    def public_hex(self) -> str:
        return self.public.hex()

    def open(
        self, blob: bytes, site_id: object, cmid: int, field_: str, request_id: object
    ) -> bytes:
        """The plaintext of the worker's answer; ``InvalidTag`` on any other binding or key."""
        ring = AssessKeyring(
            "config_reveal_ephemeral", REVEAL_KID, {REVEAL_KID: self._private}
        )
        return open_sealed(
            ring,
            blob,
            purpose=Purpose.CONFIG_REVEAL,
            binding=_binding(site_id, cmid, field_, request_id),
        )

    def __repr__(self) -> str:
        return "EphemeralKey(<redacted>)"


def seal_reveal(
    public: bytes,
    plaintext: bytes,
    site_id: object,
    cmid: int,
    field_: str,
    request_id: object,
) -> bytes:
    """Worker side: seal one opened field to the request's ephemeral public key."""
    return seal(
        public,
        REVEAL_KID,
        plaintext,
        purpose=Purpose.CONFIG_REVEAL,
        binding=_binding(site_id, cmid, field_, request_id),
    )


__all__ = ["REVEAL_KID", "EphemeralKey", "public_from_hex", "seal_reveal"]
