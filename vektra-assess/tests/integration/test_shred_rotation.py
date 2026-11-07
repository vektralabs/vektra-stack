"""Shredding, purge and rotation on real roles (plan assess-data-crypto 5.6)."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

from vektra_assess.crypto import envelope, rotation, shred, teacher_config, transcript
from vektra_assess.crypto.kek import KeyNotAvailable, unwrap_dek
from vektra_assess.crypto.seal import open_sealed
from vektra_assess.keys import AssessKeyring

from .conftest import CANARY, dump_data, make_real_session, sym, x_ring

pytestmark = pytest.mark.integration


async def wipe(pg) -> None:
    """Rotation helpers sweep every row, so each rotation test starts from an empty set."""
    await pg.run("super", "TRUNCATE assess_sites CASCADE")


async def count(pg, table: str, col: str, val) -> int:
    return (
        await pg.run(
            "super", f"SELECT count(*) FROM {table} WHERE {col}=:v", {"v": val}
        )
    )[0][0]


SESSION_TABLES = (
    ("assess_sessions", "id"),
    ("assess_snapshots", "session_id"),
    ("assess_turns", "session_id"),
    ("assess_session_keys", "session_id"),
    ("assess_session_handoff", "session_id"),
)


async def test_shred_session_leaves_no_row_no_id_and_no_plaintext(pg):
    kek, (api_pub, _) = sym("k1"), x_ring("a1")
    sid = await make_real_session(pg, kek, api_pub, close=True)
    assert CANARY not in dump_data(pg)  # encrypted at rest even before the shred
    async with pg.conn("worker") as c:
        report = await shred.shred_session(c, sid)
        await c.commit()
    assert (
        report.deleted["assess_sessions"] == 1 and report.deleted["assess_turns"] == 1
    )
    for table, col in SESSION_TABLES:
        assert await count(pg, table, col, sid) == 0, table
    dump = dump_data(pg)
    assert str(sid) not in dump and CANARY not in dump


async def test_shred_refuses_a_live_session_and_a_session_with_a_transcript(pg):
    kek, (api_pub, _) = sym("k1"), x_ring("a1")
    live = await make_real_session(pg, kek, api_pub)
    async with pg.conn("worker") as c:
        with pytest.raises(shred.ShredRefused):
            await shred.shred_session(c, live)
        await c.rollback()
    assert await count(pg, "assess_sessions", "id", live) == 1

    done = await make_real_session(pg, kek, api_pub, close=True)
    tkek = sym("t1")
    ct, wrapped, tkid = transcript.encrypt_transcript(tkek, done, CANARY.encode())
    tid = uuid.uuid4()
    async with pg.conn("worker") as c:
        await c.execute(
            text(
                "INSERT INTO assess_transcripts (id, session_id, ciphertext, dek_wrapped, "
                "transcript_kid, content_hash, turns_digest, filename, byte_size) "
                "VALUES (:t, :s, :c, :w, :k, 'h', 'd', 'f.html', 1)"
            ),
            {"t": tid, "s": done, "c": ct, "w": wrapped, "k": tkid},
        )
        await c.execute(
            text(
                "INSERT INTO assess_deliveries (transcript_id, recipient_kind, recipient_userid) "
                "VALUES (:t, 'grader', 12)"
            ),
            {"t": tid},
        )
        await c.commit()
    await pg.run(
        "super",
        "INSERT INTO assess_transcript_download_tokens (jti_hash, transcript_id, subject_userid, expires_at, audience_role) VALUES ('j', :t, 9, now(), 'owner')",
        {"t": tid},
    )
    async with pg.conn("worker") as c:
        with pytest.raises(shred.ShredRefused):
            await shred.shred_session(c, done)
        await c.rollback()
        report = await shred.shred_session(c, done, with_transcript=True)
        await c.commit()
    assert report.deleted["assess_transcripts"] == 1
    assert report.deleted["assess_deliveries"] == 1
    assert report.deleted["assess_transcript_download_tokens"] == 1
    for table, col in SESSION_TABLES:
        assert await count(pg, table, col, done) == 0, table
    assert await count(pg, "assess_transcripts", "session_id", done) == 0
    assert await count(pg, "assess_transcripts", "id", tid) == 0
    assert await count(pg, "assess_deliveries", "transcript_id", tid) == 0
    assert (
        await count(pg, "assess_transcript_download_tokens", "transcript_id", tid) == 0
    )
    assert CANARY not in dump_data(pg)


async def test_shred_is_idempotent(pg):
    async with pg.conn("worker") as c:
        assert (await shred.shred_session(c, uuid.uuid4())).deleted == {}
        assert (await shred.shred_transcript(c, uuid.uuid4())).deleted == {}


async def test_rewrap_session_keys_resumably_then_old_kid_can_go(pg):
    await wipe(pg)
    old = sym("k1")
    api_pub = x_ring("a1")[0]
    sids = [await make_real_session(pg, old, api_pub) for _ in range(5)]
    new = AssessKeyring("assess_kek", "k2", {**old.keys, "k2": os.urandom(32)})
    remaining = []
    async with pg.conn("worker") as c:
        while True:
            left = await rotation.rewrap_session_keys(c, new, "k1", batch=2)
            await c.commit()
            remaining.append(left)
            if left == 0:
                break
    assert remaining[0] >= 3 and remaining[-1] == 0 and len(remaining) >= 2
    only_new = AssessKeyring(
        "assess_kek", "k2", {"k2": new.keys["k2"]}
    )  # old kid removed
    for sid in sids:
        row = (
            await pg.run(
                "super",
                "SELECT kek_wrapped, kek_kid, key_version FROM assess_session_keys "
                "WHERE session_id=:s",
                {"s": sid},
            )
        )[0]
        assert row.kek_kid == "k2" and row.key_version == 2
        dek = unwrap_dek(only_new, bytes(row.kek_wrapped), sid, row.key_version)
        snap = (
            await pg.run(
                "super",
                "SELECT ciphertext FROM assess_snapshots WHERE session_id=:s",
                {"s": sid},
            )
        )[0][0]
        assert envelope.decrypt_snapshot(dek, sid, bytes(snap)) == CANARY.encode()


async def test_reseal_handoffs_skips_closed_sessions_and_new_key_opens(pg):
    await wipe(pg)
    kek = sym("k1")
    old_pub, old_priv = x_ring("a1")
    live = await make_real_session(pg, kek, old_pub)
    closed = await make_real_session(pg, kek, old_pub)
    await pg.run(
        "worker",
        "UPDATE assess_sessions SET state='CLOSED', close_reason='finished' "
        "WHERE id=:s",
        {"s": closed},
    )
    new_pub, new_priv = x_ring("a2")
    async with pg.conn("worker") as c:
        n = await rotation.reseal_handoffs(c, kek, new_pub.active_key, "a2", limit=50)
        await c.commit()
    assert n == 0  # nothing live is left on the old kid
    row = (
        await pg.run(
            "super",
            "SELECT sealed_dek, handoff_kid FROM assess_session_handoff "
            "WHERE session_id=:s",
            {"s": live},
        )
    )[0]
    assert row.handoff_kid == "a2"
    dek = open_sealed(
        new_priv, bytes(row.sealed_dek), purpose="handoff", binding=[live, "a2"]
    )
    snap = (
        await pg.run(
            "super",
            "SELECT ciphertext FROM assess_snapshots WHERE session_id=:s",
            {"s": live},
        )
    )[0][0]
    assert envelope.decrypt_snapshot(dek, live, bytes(snap)) == CANARY.encode()
    with pytest.raises(KeyNotAvailable):  # the old private key cannot open the new seal
        open_sealed(
            old_priv, bytes(row.sealed_dek), purpose="handoff", binding=[live, "a2"]
        )
    assert await count(pg, "assess_session_handoff", "session_id", closed) == 0


async def test_reseal_teacher_configs_and_rewrap_transcript_deks(pg):
    await wipe(pg)
    site = uuid.uuid4()
    await pg.run(
        "super",
        "INSERT INTO assess_sites (id, moodle_site_uuid, base_url) VALUES (:i, :u, 'https://m')",
        {"i": site, "u": str(site)},
    )
    old_pub, old_priv = x_ring("w1")
    blob, kid = teacher_config.seal_teacher_config(
        old_pub, site, 3, rotation.TEACHER_CONFIG_FIELD, "solution: 42"
    )
    await pg.run(
        "super",
        "INSERT INTO assess_activity_config (site_id, cmid, activity_type, "
        "teacher_config_sealed, teacher_config_kid) VALUES (:s, 3, 'quiz', :b, :k)",
        {"s": site, "b": blob, "k": kid},
    )
    new_pub, new_priv = x_ring("w2")
    async with pg.conn("worker") as c:
        assert await rotation.reseal_teacher_configs(c, old_priv, new_pub) == 0
        await c.commit()
        assert (
            await rotation.reseal_teacher_configs(c, old_priv, new_pub) == 0
        )  # nothing left
    row = (
        await pg.run(
            "super",
            "SELECT teacher_config_sealed, teacher_config_kid FROM assess_activity_config "
            "WHERE site_id=:s",
            {"s": site},
        )
    )[0]
    assert row.teacher_config_kid == "w2"
    assert (
        teacher_config.open_teacher_config(
            new_priv,
            bytes(row.teacher_config_sealed),
            site,
            3,
            rotation.TEACHER_CONFIG_FIELD,
        )
        == "solution: 42"
    )

    kek1 = sym("t1")
    sid = await make_real_session(pg, sym("k1"), x_ring("a1")[0], close=True)
    tid = uuid.uuid4()
    ct, wrapped, tkid = transcript.encrypt_transcript(kek1, tid, CANARY.encode())
    await pg.run(
        "worker",
        "INSERT INTO assess_transcripts (id, session_id, ciphertext, dek_wrapped, "
        "transcript_kid, content_hash, turns_digest, filename, byte_size) "
        "VALUES (:t, :s, :c, :w, :k, 'h', 'd', 'f.html', 1)",
        {"t": tid, "s": sid, "c": ct, "w": wrapped, "k": tkid},
    )
    kek2 = AssessKeyring(
        "assess_transcript_kek", "t2", {**kek1.keys, "t2": os.urandom(32)}
    )
    async with pg.conn("worker") as c:
        assert await rotation.rewrap_transcript_deks(c, kek2, "t1") == 0
        await c.commit()
    row = (
        await pg.run(
            "super",
            "SELECT ciphertext, dek_wrapped, transcript_kid FROM assess_transcripts "
            "WHERE id=:t",
            {"t": tid},
        )
    )[0]
    only_new = AssessKeyring("k", "t2", {"t2": kek2.keys["t2"]})
    assert row.transcript_kid == "t2"
    assert (
        transcript.decrypt_transcript(
            only_new, tid, bytes(row.ciphertext), bytes(row.dek_wrapped)
        )
        == CANARY.encode()
    )
