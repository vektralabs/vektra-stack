"""Turn-hash re-verification and the transcript's turns digest (REQ-102, BR-015).

There is ONE canonical turn serialisation: ``vektra_assess.crypto.hashing.turn_hash``, used at
turn time by the API TurnDriver, the worker pre-flight and ``reconcile_reply``, and re-exported
here so the transcript generator can only ever recompute with the same function (no second
implementation can drift).

``verify_turns`` re-hashes every decrypted turn against the hash persisted at turn time, anchored
on the snapshot hash; the first break raises ``ChainError`` and the generator refuses to write a
transcript. ``turns_digest`` is the SHA-256 printed in the transcript footer: over the ordered
turn hashes, length-framed, so the footer pins the whole chain.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterable, Sequence

from vektra_assess.crypto.hashing import ChainError, ChainTurn, turn_hash, verify_chain

__all__ = ["ChainError", "ChainTurn", "turn_hash", "turns_digest", "verify_turns"]

_DOMAIN = b"vektra-assess/turns-digest/v1"


def verify_turns(genesis_hash: str, turns: Iterable[ChainTurn]) -> int:
    """Re-hash every turn (1..N, in order) and compare with the stored hash; returns N."""
    return verify_chain(genesis_hash, turns)


def turns_digest(turn_hashes: Sequence[str]) -> str:
    h = hashlib.sha256(_DOMAIN)
    for value in turn_hashes:
        raw = value.encode("ascii")
        h.update(struct.pack(">I", len(raw)) + raw)
    return h.hexdigest()
