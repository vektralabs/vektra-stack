"""End-to-end data tests on real roles (plan assess-data-crypto 6.2 - 6.6)."""

from __future__ import annotations

import asyncio
import subprocess
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from vektra_assess.crypto import envelope, hashing, teacher_config
from vektra_assess.crypto.seal import open_sealed

from .conftest import CANARY, dump_data, make_real_session, sym, x_ring

pytestmark = pytest.mark.integration

TEACHER_CANARY = "CANARY-teacher-solution-55aa"


async def one(pg, sql, **p):
    return (await pg.run("super", sql, p))[0][0]


async def make_site(pg) -> uuid.UUID:
    site = uuid.uuid4()
    await pg.run(
        "super",
        "INSERT INTO assess_sites (id, lms_site_uuid, base_url) "
        "VALUES (:i, :u, 'https://m')",
        {"i": site, "u": str(site)},
    )
    return site


# --- 6.2 race --------------------------------------------------------------------------


async def test_twenty_concurrent_starts_leave_exactly_one_open_session(pg):
    site = await make_site(pg)
    insert = (
        "INSERT INTO assess_sessions (site_id, cmid, student_userid, state) "
        "VALUES (:s, 77, 88, 'STARTING')"
    )

    async def attempt():
        try:
            await pg.run("api", insert, {"s": site})
            return "ok"
        except DBAPIError as exc:
            return getattr(exc.orig, "sqlstate", "?")

    results = await asyncio.gather(*[attempt() for _ in range(20)])
    assert results.count("ok") == 1 and results.count("23505") == 19, results
    assert (
        await one(pg, "SELECT count(*) FROM assess_sessions WHERE site_id=:s", s=site)
        == 1
    )


# --- 6.3 lifecycle with real ciphertext ----------------------------------------------------


async def test_lifecycle_walk_open_converse_close(pg):
    kek, (api_pub, api_priv) = sym("k1"), x_ring("a1")
    sid = await make_real_session(pg, kek, api_pub)

    # the API opens the session material with its private key and nothing else
    row = (
        await pg.run("api", "SELECT * FROM get_session_material(:s, 1)", {"s": sid})
    )[0]
    dek = open_sealed(
        api_priv,
        bytes(row.sealed_dek),
        purpose="handoff",
        binding=[sid, row.handoff_kid],
    )
    assert (
        envelope.decrypt_snapshot(dek, sid, bytes(row.snapshot_ciphertext))
        == CANARY.encode()
    )
    assert (
        envelope.decrypt_turn(dek, sid, 1, bytes(row.turn_ciphertexts[0]))
        == CANARY.encode()
    )

    prev = row.turn_hashes[-1]
    for n, (role, msg) in enumerate(
        [("student", b"q1"), ("bot", b"a1"), ("student", b"q2")], 2
    ):
        h = hashing.turn_hash(prev, n, role, "normal", msg)
        seq = n - 2
        got = (
            await pg.run(
                "api",
                "SELECT assess_append_turn(:s, 1, :q, :r, 'normal', :c, :h, :p)",
                {
                    "s": sid,
                    "q": seq,
                    "r": role,
                    "c": envelope.encrypt_turn(dek, sid, n, msg),
                    "h": h,
                    "p": prev,
                },
            )
        )[0][0]
        assert got == n
        prev = h

    final = (
        await pg.run("api", "SELECT * FROM get_session_material(:s, 1)", {"s": sid})
    )[0]
    chain = [
        hashing.ChainTurn(
            no, role, kind, envelope.decrypt_turn(dek, sid, no, bytes(ct)), pv, th
        )
        for no, role, kind, ct, pv, th in zip(
            final.turn_nos,
            final.turn_roles,
            final.turn_kinds,
            final.turn_ciphertexts,
            [final.snapshot_hash, *final.turn_hashes[:-1]],
            final.turn_hashes,
            strict=True,
        )
    ]
    assert hashing.verify_chain(final.snapshot_hash, chain) == 4

    assert (
        await pg.run(
            "api", "SELECT assess_close_session(:s, 1, 'finished')", {"s": sid}
        )
    )[0][0] is True
    assert (
        await one(
            pg, "SELECT count(*) FROM assess_session_handoff WHERE session_id=:s", s=sid
        )
        == 0
    )
    assert (
        await one(
            pg,
            "SELECT count(*) FROM assess_jobs WHERE dedupe_key=:k",
            k=f"transcript_generate:{sid}",
        )
        == 1
    )
    assert (
        await one(
            pg, "SELECT count(*) FROM assess_audit_events WHERE session_id=:s", s=sid
        )
        == 1
    )
    assert (
        await pg.run("api", "SELECT * FROM get_session_material(:s, 1)", {"s": sid})
        == []
    )


# --- 6.4 rollback walk and the deferred opening-turn constraint ---------------------------------


async def test_rollback_walk_leaves_one_start_attempt_and_no_session_data(pg):
    site = await make_site(pg)
    sid = uuid.uuid4()
    async with pg.conn("worker") as c:
        await c.execute(
            text(
                "INSERT INTO assess_sessions (id, site_id, cmid, student_userid, state) "
                "VALUES (:s, :site, 1, 2, 'STARTING')"
            ),
            {"s": sid, "site": site},
        )
        await c.execute(
            text("INSERT INTO assess_snapshots VALUES (:s, '\\x01', 'h', now())"),
            {"s": sid},
        )
        await c.execute(
            text(
                "INSERT INTO assess_session_keys (session_id, kek_wrapped, kek_kid) "
                "VALUES (:s, '\\x02', 'k1')"
            ),
            {"s": sid},
        )
        await c.commit()
    async with pg.conn(
        "worker"
    ) as c:  # the opening message failed: roll the start back
        await c.execute(text("DELETE FROM assess_sessions WHERE id=:s"), {"s": sid})
        await c.execute(
            text(
                "INSERT INTO assess_start_attempts (site_id, cmid, student_userid, outcome, "
                "reason_code) VALUES (:site, 1, 2, 'rolled_back', 'llm_unavailable')"
            ),
            {"site": site},
        )
        await c.commit()
    for table, col in (
        ("assess_sessions", "id"),
        ("assess_snapshots", "session_id"),
        ("assess_session_keys", "session_id"),
    ):
        assert (
            await one(pg, f"SELECT count(*) FROM {table} WHERE {col}=:s", s=sid) == 0
        ), table
    assert (
        await one(
            pg, "SELECT count(*) FROM assess_start_attempts WHERE site_id=:s", s=site
        )
        == 1
    )


async def test_committing_active_without_an_opening_turn_fails_at_commit(pg):
    site = await make_site(pg)
    async with pg.conn("worker") as c:
        await c.execute(
            text(
                "INSERT INTO assess_sessions (site_id, cmid, student_userid, state) "
                "VALUES (:site, 3, 4, 'ACTIVE')"
            ),
            {"site": site},
        )
        with pytest.raises(IntegrityError, match="without an opening turn"):
            await c.commit()
    sid = uuid.uuid4()
    async with pg.conn("worker") as c:  # STARTING -> ACTIVE needs the opening turn too
        await c.execute(
            text(
                "INSERT INTO assess_sessions (id, site_id, cmid, student_userid, state) "
                "VALUES (:s, :site, 5, 6, 'STARTING')"
            ),
            {"s": sid, "site": site},
        )
        await c.commit()
    async with pg.conn("worker") as c:
        await c.execute(
            text("UPDATE assess_sessions SET state='ACTIVE' WHERE id=:s"), {"s": sid}
        )
        with pytest.raises(IntegrityError, match="without an opening turn"):
            await c.commit()
    assert (
        await one(pg, "SELECT state FROM assess_sessions WHERE id=:s", s=sid)
        == "STARTING"
    )


# --- 6.5 plaintext canary scan -------------------------------------------------------------------


async def test_no_plaintext_canary_in_any_table_dump_or_log(pg):
    kek, (api_pub, _) = sym("k1"), x_ring("a1")
    site = await make_site(pg)
    worker_pub, _ = x_ring("w1")
    await pg.run(
        "api",
        "INSERT INTO assess_activity_config (site_id, cmid, activity_type) "
        "VALUES (:s, 9, 'quiz')",
        {"s": site},
    )
    async with pg.conn("api") as c:
        await teacher_config.store_sealed_field(
            c,
            worker_pub,
            site,
            9,
            teacher_config.SOLUTION,
            TEACHER_CANARY,
            actor_userid=5,
        )
        await c.commit()
    await make_real_session(pg, kek, api_pub)

    tables = [
        r[0]
        for r in await pg.run(
            "super", "SELECT tablename FROM pg_tables WHERE tablename LIKE 'assess\\_%'"
        )
    ]
    assert tables
    for table in tables:
        rows = await pg.run("super", f"SELECT t::text FROM {table} t")
        for (row,) in rows:
            assert CANARY not in row and TEACHER_CANARY not in row, table
    dump = dump_data(pg)
    assert "assess_turns" in dump and CANARY not in dump and TEACHER_CANARY not in dump
    logs = subprocess.run(
        ["docker", "logs", pg.container_id], capture_output=True, text=True
    )
    assert CANARY not in logs.stdout + logs.stderr
    assert TEACHER_CANARY not in logs.stdout + logs.stderr


# --- 6.6 snapshot immutability --------------------------------------------------------------------


async def test_snapshot_is_immutable_and_forced_mutation_is_detected(pg):
    kek, (api_pub, api_priv) = sym("k1"), x_ring("a1")
    sid = await make_real_session(pg, kek, api_pub)
    with pytest.raises(DBAPIError) as exc:  # the worker role has no UPDATE grant at all
        await pg.run(
            "worker",
            "UPDATE assess_snapshots SET content_hash='x' WHERE session_id=:s",
            {"s": sid},
        )
    assert exc.value.orig.sqlstate == "42501"
    with pytest.raises(
        DBAPIError, match="immutable"
    ):  # even a superuser hits the trigger
        await pg.run(
            "super",
            "UPDATE assess_snapshots SET content_hash='x' WHERE session_id=:s",
            {"s": sid},
        )

    row = (
        await pg.run("api", "SELECT * FROM get_session_material(:s, 1)", {"s": sid})
    )[0]
    dek = open_sealed(
        api_priv,
        bytes(row.sealed_dek),
        purpose="handoff",
        binding=[sid, row.handoff_kid],
    )

    def verify(snapshot_ct: bytes, stored_hash: str) -> bool:
        doc = envelope.decrypt_snapshot(dek, sid, snapshot_ct)
        return hashing.snapshot_hash({"canary": doc.decode()}) == stored_hash

    assert verify(bytes(row.snapshot_ciphertext), row.snapshot_hash)

    forged = envelope.encrypt_snapshot(
        dek, sid, b"a rewritten snapshot"
    )  # needs the DEK
    await pg.run(
        "super",
        "ALTER TABLE assess_snapshots DISABLE TRIGGER trg_assess_snapshots_immutable",
    )
    try:
        await pg.run(
            "super",
            "UPDATE assess_snapshots SET ciphertext=:c WHERE session_id=:s",
            {"c": forged, "s": sid},
        )
    finally:
        await pg.run(
            "super",
            "ALTER TABLE assess_snapshots ENABLE TRIGGER trg_assess_snapshots_immutable",
        )
    mutated = (
        await pg.run(
            "super",
            "SELECT ciphertext, content_hash FROM assess_snapshots WHERE session_id=:s",
            {"s": sid},
        )
    )[0]
    assert not verify(
        bytes(mutated.ciphertext), mutated.content_hash
    )  # detected by re-hashing


# --- audit is fail-closed ---------------------------------------------------------------------


async def test_an_audit_insert_failure_aborts_the_business_transaction(pg):
    kek, (api_pub, _) = sym("k1"), x_ring("a1")
    sid = await make_real_session(pg, kek, api_pub)
    async with pg.conn("super") as c:
        await c.execute(
            text("REVOKE INSERT ON assess_audit_events FROM vektra_assess_definer")
        )
        await c.execute(text("SAVEPOINT before_close"))
        await c.execute(text("SET LOCAL ROLE vektra_assess_api"))
        with pytest.raises(DBAPIError) as exc:
            await c.execute(
                text("SELECT assess_close_session(:s, 1, 'finished')"), {"s": sid}
            )
        assert exc.value.orig.sqlstate == "42501"
        await c.execute(text("ROLLBACK TO SAVEPOINT before_close"))
        await c.execute(text("RESET ROLE"))
        state = (
            await c.execute(
                text("SELECT state FROM assess_sessions WHERE id=:s"), {"s": sid}
            )
        ).scalar()
        jobs = (
            await c.execute(
                text("SELECT count(*) FROM assess_jobs WHERE dedupe_key=:k"),
                {"k": f"transcript_generate:{sid}"},
            )
        ).scalar()
        hand = (
            await c.execute(
                text("SELECT count(*) FROM assess_session_handoff WHERE session_id=:s"),
                {"s": sid},
            )
        ).scalar()
        assert (state, jobs, hand) == ("ACTIVE", 0, 1)  # the close left no trace
        await c.rollback()  # also undoes the REVOKE


# --- NFR-018: the read path stays far inside its budget ---------------------------------------------


async def test_session_material_read_and_decrypt_p95_under_50ms(pg):
    import os
    import time

    kek, (api_pub, api_priv) = sym("k1"), x_ring("a1")
    sid = await make_real_session(pg, kek, api_pub)
    # grow the session to the NFR-018 shape: ~200 KB snapshot, 40 turns of ~1 KB
    row = (
        await pg.run("api", "SELECT * FROM get_session_material(:s, 1)", {"s": sid})
    )[0]
    dek = open_sealed(
        api_priv,
        bytes(row.sealed_dek),
        purpose="handoff",
        binding=[sid, row.handoff_kid],
    )
    await pg.run(
        "super",
        "ALTER TABLE assess_snapshots DISABLE TRIGGER trg_assess_snapshots_immutable",
    )
    await pg.run(
        "super",
        "UPDATE assess_snapshots SET ciphertext=:c WHERE session_id=:s",
        {
            "c": envelope.encrypt_snapshot(
                dek, sid, os.urandom(100_000).hex().encode()
            ),
            "s": sid,
        },
    )
    await pg.run(
        "super",
        "ALTER TABLE assess_snapshots ENABLE TRIGGER trg_assess_snapshots_immutable",
    )
    prev = row.turn_hashes[-1]
    for n in range(2, 42):
        msg = os.urandom(512).hex().encode()
        # student and bot alternate: one reply at a time (revision 0023)
        role = "student" if n % 2 == 0 else "bot"
        h = hashing.turn_hash(prev, n, role, "normal", msg)
        got = (
            await pg.run(
                "api",
                "SELECT assess_append_turn(:s, 1, :q, :r, 'normal', :c, :h, :p)",
                {
                    "s": sid,
                    "r": role,
                    "q": n - 2,
                    "c": envelope.encrypt_turn(dek, sid, n, msg),
                    "h": h,
                    "p": prev,
                },
            )
        )[0][0]
        assert got == n
        prev = h

    samples = []
    async with pg.conn("api") as c:
        for _ in range(500):
            t0 = time.perf_counter()
            r = (
                await c.execute(
                    text("SELECT * FROM get_session_material(:s, 1)"), {"s": sid}
                )
            ).one()
            d = open_sealed(
                api_priv,
                bytes(r.sealed_dek),
                purpose="handoff",
                binding=[sid, r.handoff_kid],
            )
            envelope.decrypt_snapshot(d, sid, bytes(r.snapshot_ciphertext))
            for no, ct in zip(r.turn_nos, r.turn_ciphertexts, strict=True):
                envelope.decrypt_turn(d, sid, no, bytes(ct))
            samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    p95 = samples[int(len(samples) * 0.95)]
    print(f"get_session_material + open + decrypt p95={p95:.1f} ms (n=500)")
    assert p95 <= 50, f"p95 {p95:.1f} ms > 50 ms"


async def test_every_session_row_is_consumed_iff_it_has_an_opening_turn(pg):
    """REQ-067 consumed rule over the whole test corpus: STARTING <=> no opening turn."""
    rows = await pg.run(
        "super",
        "SELECT s.state, EXISTS (SELECT 1 FROM assess_turns t WHERE t.session_id = s.id "
        "AND t.kind = 'opening') FROM assess_sessions s",
    )
    assert rows, "empty corpus: the rule would hold vacuously"
    assert all((state != "STARTING") == has_opening for state, has_opening in rows)


async def test_reset_session_is_numbered_by_the_database(pg):
    site = await make_site(pg)
    ins = (
        "INSERT INTO assess_sessions (id, site_id, cmid, student_userid, state, close_reason) "
        "VALUES (:i, :s, 31, 41, 'CLOSED', 'finished')"
    )
    # CLOSED rows need an opening turn at commit: seed one in the same transaction
    for n in (1, 2):
        sid = uuid.uuid4()
        async with pg.conn("super") as c:
            await c.execute(text(ins), {"i": sid, "s": site})
            await c.execute(
                text(
                    "INSERT INTO assess_turns (session_id, turn_no, role, kind, ciphertext, "
                    "turn_hash, prev_hash) VALUES (:i, 1, 'bot', 'opening', '\\x01', 'h', 'g')"
                ),
                {"i": sid},
            )
            await c.commit()
        assert (
            await one(pg, "SELECT session_no FROM assess_sessions WHERE id=:i", i=sid)
            == n
        )
    await pg.run(
        "api",
        "INSERT INTO assess_sessions (site_id, cmid, student_userid, state) "
        "VALUES (:s, 31, 41, 'STARTING')",
        {"s": site},
    )
    assert (
        await one(
            pg,
            "SELECT max(session_no) FROM assess_sessions WHERE site_id=:s AND cmid=31",
            s=site,
        )
        == 3
    )


async def test_worker_close_is_audited_as_the_worker(pg):
    kek, (api_pub, _) = sym("k1"), x_ring("a1")
    sid = await make_real_session(pg, kek, api_pub)
    assert (
        await pg.run(
            "worker", "SELECT assess_close_session(:s, 1, 'timed_out')", {"s": sid}
        )
    )[0][0] is True
    assert (
        await one(
            pg, "SELECT actor_kind FROM assess_audit_events WHERE session_id=:s", s=sid
        )
        == "worker"
    )
