"""Session material in the API: the hand-off DEK and the decrypted turns (ARCH-079).

The API has no privilege on snapshots, turns, keys or hand-off rows: it reads them only through
``get_session_material(sid, epoch)``, which returns zero rows unless the session is ACTIVE or
OUTAGE with that very epoch. The DEK is unsealed with the hand-off private key and kept in an
in-process cache keyed by (sid, epoch); an epoch bump (every revocation) makes the old key
unreachable, ``evict`` drops a session on CLOSED, and entries expire after a TTL anyway.

``load_material`` is the ONLY place the API calls ``get_session_material``: the gate-order tests
spy on it to prove that no earlier gate failure ever reaches a decrypt.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text

from vektra_assess.crypto import envelope
from vektra_assess.crypto.seal import open_sealed
from vektra_assess.keys import AssessKeyring

DEK_TTL_S = (
    6 * 3600.0
)  # longer than any session (cap 120 min + extra, total <= 240 min)

_MATERIAL = text("SELECT * FROM get_session_material(:sid, :epoch)")


@dataclass(frozen=True)
class StoredTurn:
    turn_no: int
    role: str
    kind: str
    ciphertext: bytes
    turn_hash: str


@dataclass(frozen=True)
class Material:
    session_id: UUID
    epoch: int
    dek: bytes
    snapshot_ciphertext: bytes
    snapshot_hash: str
    seq: int
    turns: tuple[StoredTurn, ...]

    @property
    def last_hash(self) -> str:
        return self.turns[-1].turn_hash if self.turns else self.snapshot_hash

    @property
    def next_turn_no(self) -> int:
        return (self.turns[-1].turn_no if self.turns else 0) + 1

    def text_of(self, turn: StoredTurn) -> str:
        return envelope.decrypt_turn(
            self.dek, self.session_id, turn.turn_no, turn.ciphertext
        ).decode("utf-8")


class DekCache:
    """(sid, epoch) -> DEK with a TTL. Never logged; ``repr`` shows only the size."""

    def __init__(
        self, ttl_s: float = DEK_TTL_S, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._items: dict[tuple[UUID, int], tuple[bytes, float]] = {}

    def get(self, sid: UUID, epoch: int) -> bytes | None:
        item = self._items.get((sid, epoch))
        if item is None:
            return None
        dek, expires = item
        if self._clock() >= expires:
            del self._items[(sid, epoch)]
            return None
        return dek

    def put(self, sid: UUID, epoch: int, dek: bytes) -> None:
        # a new epoch for the session makes every older entry unreachable: drop them now
        self.evict(sid)
        self._items[(sid, epoch)] = (dek, self._clock() + self._ttl)

    def sweep(self) -> int:
        """Drop every expired entry (the opportunistic housekeeping); returns how many."""
        now = self._clock()
        expired = [k for k, (_dek, expires) in self._items.items() if now >= expires]
        for key in expired:
            del self._items[key]
        return len(expired)

    def evict(self, sid: UUID) -> None:
        for key in [k for k in self._items if k[0] == sid]:
            del self._items[key]

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"DekCache(size={len(self._items)})"


class MaterialUnavailable(Exception):
    """``get_session_material`` returned no row: the session is not live at this epoch."""


async def load_material(
    conn: Any,
    sid: UUID,
    epoch: int,
    handoff_private: AssessKeyring,
    cache: DekCache,
) -> Material:
    row = (await conn.execute(_MATERIAL, {"sid": sid, "epoch": epoch})).first()
    if row is None:
        cache.evict(sid)
        raise MaterialUnavailable()
    dek = cache.get(sid, epoch)
    if dek is None:
        dek = open_sealed(
            handoff_private,
            bytes(row.sealed_dek),
            purpose=envelope.Purpose.HANDOFF,
            binding=[sid, row.handoff_kid],
        )
        cache.put(sid, epoch, dek)
    turns = tuple(
        StoredTurn(int(no), str(role), str(kind), bytes(ct), str(h))
        for no, role, kind, ct, h in zip(
            row.turn_nos,
            row.turn_roles,
            row.turn_kinds,
            row.turn_ciphertexts,
            row.turn_hashes,
            strict=True,
        )
    )
    return Material(
        session_id=sid,
        epoch=epoch,
        dek=dek,
        snapshot_ciphertext=bytes(row.snapshot_ciphertext),
        snapshot_hash=str(row.snapshot_hash),
        seq=int(row.seq),
        turns=turns,
    )
