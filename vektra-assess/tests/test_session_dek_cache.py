"""M1.7/M1.9: the API's DEK cache per (sid, epoch): eviction on epoch bump, CLOSED and TTL."""

from __future__ import annotations

import uuid

from vektra_assess.api.material import DekCache


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_epoch_bump_evicts_the_old_key() -> None:
    c = DekCache(ttl_s=100, clock=Clock())
    sid = uuid.uuid4()
    c.put(sid, 1, b"k" * 32)
    assert c.get(sid, 1) == b"k" * 32
    c.put(sid, 2, b"n" * 32)
    assert c.get(sid, 1) is None and c.get(sid, 2) == b"n" * 32
    assert len(c) == 1


def test_evict_and_ttl() -> None:
    clock = Clock()
    c = DekCache(ttl_s=10, clock=clock)
    a, b = uuid.uuid4(), uuid.uuid4()
    c.put(a, 1, b"a" * 32)
    c.put(b, 1, b"b" * 32)
    c.evict(a)
    assert c.get(a, 1) is None and c.get(b, 1) is not None
    clock.t = 10
    assert c.get(b, 1) is None and len(c) == 0


def test_repr_never_shows_key_bytes() -> None:
    c = DekCache()
    c.put(uuid.uuid4(), 1, b"SECRETKEYBYTES-" * 2 + b"xx")
    assert "SECRET" not in repr(c) and repr(c) == "DekCache(size=1)"
