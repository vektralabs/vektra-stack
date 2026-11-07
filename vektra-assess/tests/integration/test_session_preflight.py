"""M1.5: the pre-flight handler on a real database, worker role, recorded Moodle responses.

Start goes through the real API route (``support.world``); the handler runs in-process with the
production ``MoodleStartSource`` over the 4.3 recordings and the in-process parser, a fake Part B
runner, and the real crypto. Every outcome is asserted by counting rows, and a committed session
is opened back through ``get_session_material`` as the API role.
"""

from __future__ import annotations

import asyncio
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
from vektra_assess.worker.preflight import (
    StartTarget,
    make_preflight_handler,
    rollback_start,
)
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
        ("provider_stopped", "provider_unavailable"),  # the model provider is down
        (
            "ws_unreachable",
            "unreadable",
        ),  # every Moodle read fails: nothing is readable
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
        elif case == "provider_stopped":
            from datetime import UTC, datetime

            from vektra_assess.llm.types import ProviderCallRecord, ProviderOutageError

            now = datetime.now(UTC)
            runner = FakePartB(
                fail=ProviderOutageError(ProviderCallRecord(now, None, now, "error"))
            )
        elif case == "ws_unreachable":
            from vektra_assess.worker.preflight import MoodleGateFacts

            source = FakeSource(facts=MoodleGateFacts())
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


async def test_the_gate_clock_includes_the_time_spent_reading_moodle(  # type: ignore[no-untyped-def]
    pg, worker_engine
) -> None:
    """CodeRabbit 4493120961: an end date that passes while the Moodle facts are read closes
    the gate (the gate's now is the database's now plus the time spent since)."""
    from dataclasses import replace as dc_replace

    from support.preflight import FakeSource

    await _reset(pg)
    await activate(pg)
    t = [0.0]

    class SlowFacts(FakeSource):
        async def gate_facts(self, target):  # type: ignore[no-untyped-def]
            t[0] += 120  # two minutes of Moodle reads, on the handler's clock
            return await super().gate_facts(target)

    async with world(pg, default_settings(VEKTRA_ASSESS_START_T_S=300)) as w:
        await configure_activity(
            pg, w.site, CMID, end_at_sql="now() + interval '60 seconds'"
        )
        sid = await _start(w)
        built = await moodle_source(worker_engine).capture(
            StartTarget(uuid.uuid4(), w.site, CMID, STU, None, "assign", 1, {})
        )
        d = dc_replace(
            deps(w.settings, w.handoff_public, SlowFacts(built)), clock=lambda: t[0]
        )
        await make_preflight_handler(d)(job_context(worker_engine, sid))
        await _rolled_back(pg, sid, "end_date_passed")


async def test_a_commit_that_overruns_t_rolls_back(
    pg, worker_engine, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4493109439: T is checked again at the END of the commit transaction (wall
    clock), not only when it starts."""
    import asyncio

    from vektra_assess.worker import preflight as mod

    await _reset(pg)
    await activate(pg)
    original = mod.persist_capture

    async def slow_persist(*a, **k):  # type: ignore[no-untyped-def]
        await asyncio.sleep(7)
        return await original(*a, **k)

    monkeypatch.setattr(mod, "persist_capture", slow_persist)
    settings = default_settings(VEKTRA_ASSESS_START_T_S=20)
    async with world(pg, settings) as w:
        await configure_activity(pg, w.site, CMID)
        sid = await _start(w)
        await pg.run(
            "super",
            "UPDATE assess_sessions SET created_at = now() - interval '13 seconds' WHERE id = :s",
            {"s": sid},
        )
        handler = make_preflight_handler(
            deps(settings, w.handoff_public, moodle_source(worker_engine))
        )
        await handler(job_context(worker_engine, sid))
        await _rolled_back(pg, sid, "start_timeout")


async def test_an_audit_write_that_overruns_t_rolls_back(
    pg, worker_engine, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4493109439 (round 2): the T guard is the LAST statement before COMMIT, after
    the commit audit event, so a slow audit write cannot carry the commit past T."""
    import asyncio

    from vektra_assess.audit import AssessAuditEvent
    from vektra_assess.worker import preflight as mod

    await _reset(pg)
    await activate(pg)
    original = mod.write_event

    async def slow_commit_audit(conn, event, **kw):  # type: ignore[no-untyped-def]
        if event is AssessAuditEvent.COMMIT:
            await asyncio.sleep(7)
        return await original(conn, event, **kw)

    monkeypatch.setattr(mod, "write_event", slow_commit_audit)
    settings = default_settings(VEKTRA_ASSESS_START_T_S=20)
    async with world(pg, settings) as w:
        await configure_activity(pg, w.site, CMID)
        sid = await _start(w)
        await pg.run(
            "super",
            "UPDATE assess_sessions SET created_at = now() - interval '13 seconds' WHERE id = :s",
            {"s": sid},
        )
        handler = make_preflight_handler(
            deps(settings, w.handoff_public, moodle_source(worker_engine))
        )
        await handler(job_context(worker_engine, sid))
        await _rolled_back(pg, sid, "start_timeout")


async def test_the_session_clock_starts_at_the_end_of_the_commit_transaction(
    pg, worker_engine, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit on PR #153: committed_at and the deadline come from the wall clock at the end
    of the commit transaction, not from now() (its start), so a slow commit transaction does not
    shorten the student's time."""
    import asyncio

    from vektra_assess.audit import AssessAuditEvent
    from vektra_assess.worker import preflight as mod

    await _reset(pg)
    await activate(pg)
    original = mod.write_event

    async def slow_commit_audit(conn, event, **kw):  # type: ignore[no-untyped-def]
        if event is AssessAuditEvent.COMMIT:
            await asyncio.sleep(3)
        return await original(conn, event, **kw)

    monkeypatch.setattr(mod, "write_event", slow_commit_audit)
    settings = default_settings()
    async with world(pg, settings) as w:
        await configure_activity(pg, w.site, CMID)
        sid = await _start(w)
        handler = make_preflight_handler(
            deps(settings, w.handoff_public, moodle_source(worker_engine))
        )
        await handler(job_context(worker_engine, sid))
        state, waited, span, duration = (
            await pg.run(
                "super",
                "SELECT state, extract(epoch FROM committed_at - created_at), "
                "extract(epoch FROM deadline_at - committed_at), duration_s "
                "FROM assess_sessions WHERE id = :s",
                {"s": sid},
            )
        )[0]
        assert state == "ACTIVE"
        # stamped after the 3 s audit write, not at the transaction start before it
        assert float(waited) >= 3.0
        assert float(span) == float(duration)


def _ambiguous_acquire(engine, armed: dict, mode: str):  # type: ignore[no-untyped-def]
    """``acquire`` whose next transaction, once armed, fails at COMMIT: ``after`` commits for
    real and then raises (the outcome is lost on the wire), ``before`` rolls back and raises."""
    from contextlib import asynccontextmanager

    class Tx:
        def __init__(self, tx):  # type: ignore[no-untyped-def]
            self._tx = tx

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            await self._tx.__aenter__()
            return self

        async def __aexit__(self, et, e, tb):  # type: ignore[no-untyped-def]
            if et is None and armed.get("on"):
                armed["on"] = False
                if mode == "after":
                    await self._tx.__aexit__(None, None, None)  # COMMIT happens
                else:
                    await self._tx.__aexit__(
                        ConnectionResetError, ConnectionResetError(), None
                    )  # rolled back
                raise ConnectionResetError("connection lost at COMMIT")
            return await self._tx.__aexit__(et, e, tb)

    class Conn:
        def __init__(self, conn):  # type: ignore[no-untyped-def]
            self._c = conn

        def __getattr__(self, name):  # type: ignore[no-untyped-def]
            return getattr(self._c, name)

        def begin(self):  # type: ignore[no-untyped-def]
            return Tx(self._c.begin())

    @asynccontextmanager
    async def acquire():  # type: ignore[no-untyped-def]
        async with engine.connect() as c:
            yield Conn(c)

    return acquire


@pytest.mark.parametrize(("mode", "committed"), [("after", True), ("before", False)])
async def test_an_ambiguous_commit_is_resolved_by_rereading_the_row(  # type: ignore[no-untyped-def]
    pg, worker_engine, monkeypatch, mode: str, committed: bool
) -> None:
    """CodeRabbit 4493109439 (round 2): an error during or after COMMIT re-reads the row on a
    new connection: ACTIVE (committed) is left alone with no rollback event; STARTING is rolled
    back (commit_failed)."""
    import logging

    from vektra_assess.jobs import JobKind
    from vektra_assess.worker import preflight as mod
    from vektra_assess.worker.runner import JobContext

    await _reset(pg)
    await activate(pg)
    armed: dict[str, bool] = {}
    original = mod.persist_capture

    async def arming(*a, **k):  # type: ignore[no-untyped-def]
        armed["on"] = True  # the commit transaction is the one in progress
        return await original(*a, **k)

    monkeypatch.setattr(mod, "persist_capture", arming)
    async with world(pg) as w:
        await configure_activity(pg, w.site, CMID)
        sid = await _start(w)
        handler = make_preflight_handler(
            deps(w.settings, w.handoff_public, moodle_source(worker_engine))
        )
        ctx = JobContext(
            1,
            JobKind.PREFLIGHT,
            {"session_id": str(sid)},
            1,
            _ambiguous_acquire(worker_engine, armed, mode),
            logging.getLogger("t"),
        )
        await handler(ctx)  # never raises: the outcome is resolved
        if committed:
            assert await _counts(pg, sid) == {
                "assess_sessions": 1,
                "assess_snapshots": 1,
                "assess_session_keys": 1,
                "assess_turns": 2,
                "assess_session_handoff": 1,
            }
            rollbacks = await pg.run(
                "super",
                "SELECT (SELECT count(*) FROM assess_audit_events WHERE session_id = :s "
                "AND event = 'assess_start_rollback') + (SELECT count(*) FROM "
                "assess_start_attempts WHERE session_id = :s AND outcome = 'rolled_back')",
                {"s": sid},
            )
            assert rollbacks[0][0] == 0
        else:
            await _rolled_back(pg, sid, "commit_failed")


async def test_the_first_enrolment_check_keeps_its_observation_time(  # type: ignore[no-untyped-def]
    pg, worker_engine
) -> None:
    """CodeRabbit 4498466313: the 'enrolled' row the commit writes is dated no later than the
    pre-flight's admission (before Moodle was read), never at the commit: a slow capture
    cannot make an old enrolment observation look fresh."""
    await _reset(pg)
    await activate(pg)
    settings = default_settings()
    async with world(pg, settings) as w:
        await configure_activity(pg, w.site, CMID)
        inner = moodle_source(worker_engine)

        class SlowGate(FakeSource):
            async def gate_facts(self, target):  # type: ignore[no-untyped-def]
                await asyncio.sleep(2.0)
                return await inner.gate_facts(target)

        source = SlowGate(inner=inner)
        sid = await _start(w)
        handler = make_preflight_handler(deps(settings, w.handoff_public, source, None))
        await handler(job_context(worker_engine, sid))
        age = await pg.run(
            "super",
            "SELECT extract(epoch FROM s.committed_at - c.checked_at) FROM assess_sessions s "
            "JOIN assess_enrolment_checks c ON c.session_id = s.id WHERE s.id = :s",
            {"s": sid},
        )
        assert float(age[0][0]) >= 1.5, age
