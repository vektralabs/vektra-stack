"""M1.5: the pre-flight handler on a real database, worker role, recorded Moodle responses.

Start goes through the real API route (``support.world``); the handler runs in-process with the
production ``MoodleStartSource`` over the 4.3 recordings and the in-process parser, a fake Part B
runner, and the real crypto. Every outcome is asserted by counting rows, and a committed session
is opened back through ``get_session_material`` as the API role.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from assess_moodle_support import Replay, make_client, manifest, uid
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakePartB, FakeSource, activate, deps, job_context
from support.world import configure_activity, default_settings, world
from test_context_capture import InProcessParser

from vektra_assess.context.capture.capture import Failed, FailReason
from vektra_assess.crypto import envelope, hashing
from vektra_assess.crypto.seal import open_sealed
from vektra_assess.llm.types import PartBRejectedError
from vektra_assess.worker.preflight import make_preflight_handler, rollback_start
from vektra_assess.worker.start_source import MoodleStartSource

pytestmark = pytest.mark.integration

BRANCH = "4.3"
M = manifest(BRANCH)
CMID = M["assigns"]["rubric_vis"]["cmid"]
STU = uid(BRANCH, "stu1")


async def n(pg, sql: str, **params) -> int:  # type: ignore[no-untyped-def]
    return int((await pg.run("super", sql, params))[0][0])


async def _reset(pg) -> None:  # type: ignore[no-untyped-def]
    await pg.run("super", "DELETE FROM assess_model_activations")
    await pg.run("super", "DELETE FROM assess_jobs")
    await pg.run("super", "DELETE FROM assess_session_handoff")
    await pg.run(
        "super",
        "UPDATE assess_sessions SET state='CLOSED', close_reason='finished' "
        "WHERE state IN ('ACTIVE','OUTAGE')",
    )
    await pg.run("super", "DELETE FROM assess_sessions WHERE state = 'STARTING'")


async def _start(w, sub: int = STU, cmid: int = CMID) -> uuid.UUID:  # type: ignore[no-untyped-def]
    await pg_release(w)
    r = await w.client.post(
        "/api/v1/assess/sessions",
        headers={"Authorization": f"Bearer {await w.presession(sub=sub, cmid=cmid)}"},
    )
    assert r.status_code == 202, r.text
    return uuid.UUID(r.json()["session_id"])


async def pg_release(w) -> None:  # type: ignore[no-untyped-def]
    await w.pg.run(
        "super",
        "UPDATE assess_sites SET release = :r WHERE id = :s",
        {"r": M["release"], "s": w.site},
    )


def moodle_source(worker_engine: Any) -> MoodleStartSource:
    return MoodleStartSource(
        make_client(Replay(BRANCH)), InProcessParser(), worker_engine.connect
    )


async def _counts(pg, sid: uuid.UUID) -> dict[str, int]:  # type: ignore[no-untyped-def]
    out = {}
    for table, col in (
        ("assess_sessions", "id"),
        ("assess_snapshots", "session_id"),
        ("assess_session_keys", "session_id"),
        ("assess_turns", "session_id"),
        ("assess_session_handoff", "session_id"),
    ):
        out[table] = await n(
            pg, f"SELECT count(*) FROM {table} WHERE {col} = :s", s=sid
        )
    return out


async def _rolled_back(pg, sid: uuid.UUID, reason: str) -> None:  # type: ignore[no-untyped-def]
    assert await _counts(pg, sid) == dict.fromkeys(
        (
            "assess_sessions",
            "assess_snapshots",
            "assess_session_keys",
            "assess_turns",
            "assess_session_handoff",
        ),
        0,
    )
    assert (
        await n(
            pg,
            "SELECT count(*) FROM assess_start_attempts WHERE session_id = :s "
            "AND outcome = 'rolled_back' AND reason_code = :r",
            s=sid,
            r=reason,
        )
        == 1
    )
    assert (
        await n(
            pg,
            "SELECT count(*) FROM assess_audit_events WHERE session_id = :s "
            "AND event = 'assess_start_rollback' AND reason_code = :r",
            s=sid,
            r=reason,
        )
        == 1
    )


@pytest.fixture
async def worker_engine(pg):  # type: ignore[no-untyped-def]
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


async def test_commit_writes_the_whole_session_and_the_api_can_open_it(  # type: ignore[no-untyped-def]
    pg, worker_engine
) -> None:
    await _reset(pg)
    await activate(pg)
    async with world(pg) as w:
        await configure_activity(pg, w.site, CMID, cap_minutes=30)
        await pg.run(
            "super",
            "INSERT INTO assess_extra_time (site_id, cmid, student_userid, extra_seconds) "
            "VALUES (:s, :c, :u, 600)",
            {"s": w.site, "c": CMID, "u": STU},
        )
        sid = await _start(w)
        handler = make_preflight_handler(
            deps(w.settings, w.handoff_public, moodle_source(worker_engine))
        )
        await handler(job_context(worker_engine, sid))
        assert await _counts(pg, sid) == {
            "assess_sessions": 1,
            "assess_snapshots": 1,
            "assess_session_keys": 1,
            "assess_turns": 2,
            "assess_session_handoff": 1,
        }
        row = (
            await pg.run(
                "super",
                "SELECT state, seq, duration_s, "
                "extract(epoch FROM deadline_at - committed_at)::int, "
                "frozen_policy IS NOT NULL, preflight_started_at IS NOT NULL "
                "FROM assess_sessions WHERE id = :s",
                {"s": sid},
            )
        )[0]
        assert tuple(row) == ("ACTIVE", 2, 30 * 60 + 600, 30 * 60 + 600, True, True)
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_start_attempts WHERE session_id=:s "
                "AND outcome='committed'",
                s=sid,
            )
            == 1
        )
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE session_id=:s "
                "AND event='assess_commit'",
                s=sid,
            )
            == 1
        )
        # the API role opens it: unseal the DEK, decrypt both opening turns, verify the chain
        async with w.engine.connect() as conn:
            mat = (
                await conn.execute(
                    text("SELECT * FROM get_session_material(:s, 1)"), {"s": sid}
                )
            ).one()
        dek = open_sealed(
            w.handoff_private,
            bytes(mat.sealed_dek),
            purpose="handoff",
            binding=[sid, mat.handoff_kid],
        )
        texts = [
            envelope.decrypt_turn(dek, sid, no, bytes(ct)).decode()
            for no, ct in zip(mat.turn_nos, mat.turn_ciphertexts, strict=True)
        ]
        assert list(mat.turn_kinds) == ["opening", "opening"]
        assert "Sessione #1" in texts[0] and "40 minuti" in texts[0]
        assert texts[1] == FakePartB().text
        turns = [
            hashing.ChainTurn(no, "bot", "opening", t.encode(), "", h)
            for no, t, h in zip(mat.turn_nos, texts, mat.turn_hashes, strict=True)
        ]
        prev, chain = mat.snapshot_hash, []
        for t in turns:
            chain.append(
                hashing.ChainTurn(
                    t.turn_no, t.role, t.kind, t.plaintext, prev, t.turn_hash
                )
            )
            prev = t.turn_hash
        assert hashing.verify_chain(mat.snapshot_hash, chain) == 2
        # a second run of the same job is a stale no-op
        await handler(job_context(worker_engine, sid))
        assert (
            await n(pg, "SELECT count(*) FROM assess_turns WHERE session_id=:s", s=sid)
            == 2
        )


async def test_unactivated_triple_is_refused_then_accepted_without_restart(  # type: ignore[no-untyped-def]
    pg, worker_engine
) -> None:
    await _reset(pg)
    async with world(pg) as w:
        await configure_activity(pg, w.site, CMID)
        handler = make_preflight_handler(
            deps(w.settings, w.handoff_public, moodle_source(worker_engine))
        )
        sid = await _start(w)
        await handler(job_context(worker_engine, sid))
        await _rolled_back(pg, sid, "model_not_activated")
        await activate(pg)  # the same handler object: no restart
        sid2 = await _start(w)
        await handler(job_context(worker_engine, sid2))
        assert (await _counts(pg, sid2))["assess_turns"] == 2


async def test_allow_unactivated_is_honoured_only_in_development(  # type: ignore[no-untyped-def]
    pg, worker_engine
) -> None:
    await _reset(pg)
    for env, committed in (("production", False), ("development", True)):
        settings = default_settings(
            VEKTRA_ENV=env, VEKTRA_ASSESS_LLM_ALLOW_UNACTIVATED=True
        )
        async with world(pg, settings) as w:
            await configure_activity(pg, w.site, CMID)
            sid = await _start(w)
            handler = make_preflight_handler(
                deps(settings, w.handoff_public, moodle_source(worker_engine))
            )
            await handler(job_context(worker_engine, sid))
            if committed:
                assert (await _counts(pg, sid))["assess_turns"] == 2
            else:
                await _rolled_back(pg, sid, "model_not_activated")
        await _reset(pg)


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("capture_failed", "capture_failed_policy_input_unreadable"),
        ("capture_raises", "internal_error"),
        ("part_b_rejected", "part_b_rejected"),
        ("not_configured", "activity_not_configured"),
        ("timeout", "start_timeout"),
        ("capacity", "capacity"),
    ],
)
async def test_every_failure_rolls_back_to_eligible(  # type: ignore[no-untyped-def]
    pg, worker_engine, case: str, reason: str
) -> None:
    await _reset(pg)
    await activate(pg)
    settings = default_settings(
        VEKTRA_ASSESS_ADMISSION_K=1 if case == "capacity" else 10
    )
    async with world(pg, settings) as w:
        await configure_activity(pg, w.site, CMID)
        source: Any = moodle_source(worker_engine)
        runner: Any = None
        if case == "capture_failed":
            source = FakeSource(Failed(FailReason.POLICY_INPUT_UNREADABLE))
        elif case == "capture_raises":
            source = FakeSource(raises=RuntimeError("CANARY-secret-detail"))
        elif case == "part_b_rejected":
            runner = FakePartB(fail=PartBRejectedError("part_b_value"))
        sid = await _start(w)
        if case == "not_configured":
            await configure_activity(pg, w.site, CMID, enabled=False)
        elif case == "timeout":
            await pg.run(
                "super",
                "UPDATE assess_sessions SET created_at = now() - interval '2 minutes' "
                "WHERE id = :s",
                {"s": sid},
            )
        elif case == "capacity":
            from integration.conftest import make_session

            await make_session(pg, "ACTIVE")  # another live session fills K = 1
        handler = make_preflight_handler(
            deps(settings, w.handoff_public, source, runner)
        )
        await handler(job_context(worker_engine, sid))
        await _rolled_back(pg, sid, reason)
        # the record is ELIGIBLE again: the next Start is accepted (nothing was consumed)
        if case not in ("not_configured", "capacity"):
            again = await _start(w)
            assert again != sid
        audit = await pg.run(
            "super", "SELECT data::text, reason_code FROM assess_audit_events"
        )
        assert "CANARY" not in str(audit)


async def test_a_stalled_part_b_is_cut_inside_t(pg, worker_engine) -> None:  # type: ignore[no-untyped-def]
    await _reset(pg)
    await activate(pg)
    settings = default_settings(VEKTRA_ASSESS_START_T_S=20)
    async with world(pg, settings) as w:
        await configure_activity(pg, w.site, CMID)
        sid = await _start(w)
        # created 14 s ago: 20 - 5 (margin) - 14 = ~1 s left for capture and Part B
        await pg.run(
            "super",
            "UPDATE assess_sessions SET created_at = now() - interval '14 seconds' "
            "WHERE id = :s",
            {"s": sid},
        )
        runner = FakePartB(delay_s=30)
        handler = make_preflight_handler(
            deps(settings, w.handoff_public, moodle_source(worker_engine), runner)
        )
        await handler(job_context(worker_engine, sid))
        await _rolled_back(pg, sid, "start_timeout")


async def test_rollback_deletes_snapshot_keys_and_turns_and_verifies(  # type: ignore[no-untyped-def]
    pg, worker_engine
) -> None:
    await _reset(pg)
    async with world(pg) as w:
        await configure_activity(pg, w.site, CMID)
        sid = await _start(w)
        async with worker_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO assess_snapshots (session_id, ciphertext, content_hash) "
                    "VALUES (:s, '\\x01', 'h')"
                ),
                {"s": sid},
            )
            await conn.execute(
                text(
                    "INSERT INTO assess_session_keys (session_id, kek_wrapped, kek_kid) "
                    "VALUES (:s, '\\x02', 'k')"
                ),
                {"s": sid},
            )
            await conn.execute(
                text(
                    "INSERT INTO assess_turns (session_id, turn_no, role, kind, ciphertext, "
                    "turn_hash, prev_hash) VALUES (:s, 1, 'bot', 'opening', '\\x03', 'a', 'h')"
                ),
                {"s": sid},
            )
        async with worker_engine.begin() as conn:
            assert await rollback_start(conn, sid, "start_timeout") is True
        await _rolled_back(pg, sid, "start_timeout")
        async with worker_engine.begin() as conn:
            assert await rollback_start(conn, sid, "start_timeout") is False
