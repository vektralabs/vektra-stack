"""The rewrap driver on real session keys (plan assess-hardening-release 13a).

Sessions are written by the worker role with real ciphertext under ``k1``. The driver, in
batches of 2 and as the worker role, leaves no row under ``k1``. A keyring holding only ``k2``
then still opens every snapshot.
"""

from __future__ import annotations

import os

import pytest

from vektra_assess import keys_rewrap
from vektra_assess.crypto import envelope, rotation
from vektra_assess.crypto.kek import unwrap_dek
from vektra_assess.keys import AssessKeyring

from .conftest import CANARY, make_real_session, sym, x_ring

pytestmark = pytest.mark.integration


async def test_the_driver_rewraps_every_session_key_then_the_old_kid_can_go(pg) -> None:  # type: ignore[no-untyped-def]
    await pg.run("super", "TRUNCATE assess_sites CASCADE")
    old = sym("k1")
    sids = [await make_real_session(pg, old, x_ring("a1")[0]) for _ in range(5)]
    new = AssessKeyring("assess_kek", "k2", {**old.keys, "k2": os.urandom(32)})

    left = await rotation.rewrap_to_done(
        lambda: pg.conn("worker"),
        keys_rewrap.steps_for("session-keys", new, "k1", 2),
    )
    assert left == 0
    under_old = (
        await pg.run(
            "super", "SELECT count(*) FROM assess_session_keys WHERE kek_kid = 'k1'"
        )
    )[0][0]
    assert under_old == 0
    only_new = AssessKeyring("assess_kek", "k2", {"k2": new.keys["k2"]})
    for sid in sids:
        row = (
            await pg.run(
                "super",
                "SELECT k.kek_wrapped, k.key_version, s.ciphertext FROM assess_session_keys k "
                "JOIN assess_snapshots s ON s.session_id = k.session_id WHERE k.session_id = :s",
                {"s": sid},
            )
        )[0]
        dek = unwrap_dek(only_new, bytes(row[0]), sid, row[1])
        assert envelope.decrypt_snapshot(dek, sid, bytes(row[2])) == CANARY.encode()
