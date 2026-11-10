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


def _worker_secrets(d, kek: AssessKeyring) -> None:  # type: ignore[no-untyped-def]
    """The worker's secret set, with ``assess_kek`` replaced by ``kek`` (all its kids)."""
    import base64
    import json

    from vektra_assess.keys import KEY_MATRIX, Need, generate

    from .conftest import ROLE_PASSWORD

    full = d.parent / f"full-{d.name}"
    generate(full)
    d.mkdir()
    for name, spec in KEY_MATRIX.items():
        if spec.worker is not Need.REQUIRED:
            continue
        if name == "assess_kek":
            body = json.dumps(
                {
                    "active": kek.active,
                    "keys": {
                        k: base64.b64encode(v).decode() for k, v in kek.keys.items()
                    },
                }
            )
        elif name == "assess_db_worker_password":
            body = ROLE_PASSWORD + "\n"
        else:
            body = (full / name).read_text()
        (d / name).write_text(body)
        os.chmod(d / name, 0o400)


def _env(monkeypatch, pg, secrets) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy.engine import URL, make_url

    u = make_url(
        pg.url("worker")
    )  # the role DSN without its password (it comes from a file)
    dsn = URL.create(u.drivername, u.username, None, u.host, u.port, u.database)
    dsn = dsn.render_as_string(hide_password=False)
    monkeypatch.setenv("VEKTRA_ENV", "development")
    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setenv("VEKTRA_ASSESS_SECRETS_DIR", str(secrets))
    monkeypatch.setenv("VEKTRA_ASSESS_WORKER_DATABASE_URL", dsn)


async def _under(pg, kid: str) -> int:  # type: ignore[no-untyped-def]
    return int(
        (
            await pg.run(
                "super",
                "SELECT count(*) FROM assess_session_keys WHERE kek_kid = :k",
                {"k": kid},
            )
        )[0][0]
    )


async def test_the_command_rewraps_end_to_end_with_a_two_kid_keyring(  # type: ignore[no-untyped-def]
    pg, tmp_path, monkeypatch
) -> None:
    """Through ``run()``: settings, the key matrix, the role DSN, the keyring, the driver."""
    import io

    await pg.run("super", "TRUNCATE assess_sites CASCADE")
    old = sym("k1")
    sids = [await make_real_session(pg, old, x_ring("a1")[0]) for _ in range(3)]
    two = AssessKeyring("assess_kek", "k2", {**old.keys, "k2": os.urandom(32)})
    _worker_secrets(tmp_path / "secrets", two)
    _env(monkeypatch, pg, tmp_path / "secrets")
    out = io.StringIO()
    code = await keys_rewrap.run("session-keys", "k1", 2, out)
    assert code == 0, out.getvalue()
    assert (await _under(pg, "k1"), await _under(pg, "k2")) == (0, len(sids))
    assert "0 row(s) still under k1" in out.getvalue()
