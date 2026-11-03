"""Canonical hashes and the turn chain (plan assess-data-crypto 4.6, BR-015, REQ-102).

- snapshot hash: SHA-256 over the canonical plaintext (sorted-key JSON, UTF-8);
- turn hash: ``SHA-256(prev_hash | turn_no | role | kind | plaintext)``, each field
  length-prefixed so no two different tuples share a preimage; the genesis hash is the
  snapshot hash, so the chain is anchored to the immutable snapshot;
- transcript hash: SHA-256 over the rendered bytes.

Hashes are stored in the clear (REQ-102 and BR-015 audit anchoring): an equality oracle on
short, guessable turns, mitigated by the ``turn_no`` and ``prev_hash`` salt.
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


class ChainError(ValueError):
    """The turn chain does not verify. ``turn_no`` is the first broken turn."""

    def __init__(self, turn_no: int, reason: str) -> None:
        super().__init__(f"turn {turn_no}: {reason}")
        self.turn_no = turn_no


def canonical_json(doc: Mapping[str, Any] | list[Any]) -> bytes:
    return json.dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def snapshot_hash(plaintext_doc: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(plaintext_doc)).hexdigest()


def _frame(*parts: bytes) -> bytes:
    return b"".join(struct.pack(">I", len(p)) + p for p in parts)


def turn_hash(
    prev_hash: str, turn_no: int, role: str, kind: str, plaintext: bytes
) -> str:
    return hashlib.sha256(
        _frame(
            prev_hash.encode("ascii"),
            str(turn_no).encode("ascii"),
            role.encode("utf-8"),
            kind.encode("utf-8"),
            plaintext,
        )
    ).hexdigest()


def transcript_hash(rendered: bytes) -> str:
    return hashlib.sha256(rendered).hexdigest()


@dataclass(frozen=True)
class ChainTurn:
    turn_no: int
    role: str
    kind: str
    plaintext: bytes
    prev_hash: str
    turn_hash: str


def verify_chain(genesis_hash: str, turns: Iterable[ChainTurn]) -> int:
    """Verify turns 1..N in order. Returns N; raises ``ChainError`` at the first break."""
    prev, expected_no = genesis_hash, 1
    for t in turns:
        if t.turn_no != expected_no:
            raise ChainError(t.turn_no, f"expected turn number {expected_no}")
        if t.prev_hash != prev:
            raise ChainError(t.turn_no, "prev_hash does not match the previous turn")
        if (
            turn_hash(t.prev_hash, t.turn_no, t.role, t.kind, t.plaintext)
            != t.turn_hash
        ):
            raise ChainError(t.turn_no, "turn_hash does not match the content")
        prev, expected_no = t.turn_hash, expected_no + 1
    return expected_no - 1
