"""transcript.hashing: one turn-hash implementation, re-verification, the footer digest."""

from __future__ import annotations

import pytest

from vektra_assess.api import turns as api_turns
from vektra_assess.crypto import hashing as crypto_hashing
from vektra_assess.transcript import hashing as th
from vektra_assess.worker import preflight, reply_reconcile


def _chain(n: int, genesis: str = "g" * 64) -> list[th.ChainTurn]:
    out, prev = [], genesis
    for i in range(1, n + 1):
        role = "student" if i % 2 == 0 else "bot"
        pt = f"turn {i}".encode()
        h = th.turn_hash(prev, i, role, "normal", pt)
        out.append(th.ChainTurn(i, role, "normal", pt, prev, h))
        prev = h
    return out


def test_one_turn_hash_implementation_for_every_writer_and_the_generator() -> None:
    assert th.turn_hash is crypto_hashing.turn_hash
    for mod in (api_turns, preflight, reply_reconcile):
        assert mod.hashing.turn_hash is crypto_hashing.turn_hash


def test_verify_turns_accepts_an_intact_chain() -> None:
    assert th.verify_turns("g" * 64, _chain(5)) == 5


def test_a_tampered_turn_is_found_at_its_number() -> None:
    turns = _chain(4)
    bad = turns[2]
    turns[2] = th.ChainTurn(
        bad.turn_no, bad.role, bad.kind, b"edited", bad.prev_hash, bad.turn_hash
    )
    with pytest.raises(th.ChainError) as exc:
        th.verify_turns("g" * 64, turns)
    assert exc.value.turn_no == 3


def test_turns_digest_is_order_and_framing_sensitive() -> None:
    a, b = "a" * 64, "b" * 64
    assert th.turns_digest([a, b]) != th.turns_digest([b, a])
    assert th.turns_digest(["ab", "c"]) != th.turns_digest(["a", "bc"])
    assert len(th.turns_digest([])) == 64
