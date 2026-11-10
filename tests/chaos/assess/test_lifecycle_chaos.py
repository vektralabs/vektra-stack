"""Session-lifecycle chaos (plan assess-session-lifecycle M5.11), on plan 3's harness.

Assertions are row counts on a migrated pg16 container with the real roles:

- each of the six close reasons through the plan-2 close function: hand-off rows 0, exactly one
  ``transcript_generate`` job, epoch bumped, the old session token rejected by the gate (409
  closed, never a live read);
- ``kill -9`` of a process mid pre-flight commit (a real client process holding the commit
  transaction open as the worker role, killed with SIGKILL): Postgres aborts it, nothing of the
  session survives but the STARTING row, and the STARTING reaper brings the student back to
  ELIGIBLE with snapshot / key / turn rows 0;
- ``kill -9`` of the API mid-reply (a real process holding the live-reply claim as the API role):
  the claim dies with the process, so ``reconcile_reply`` may take the turn (it then appends
  exactly one marked turn: ``vektra-assess/tests/integration/test_session_reconcile.py``);
- Postgres restart mid-turn (the container restarted under an open append transaction): the
  turn is lost with the transaction, the client's retry appends it once, a second retry is
  refused by the sequence check: no duplicate turn, state consistent.

The real pre-flight against Moodle under kill -9 is not reproduced here (it needs the dev Moodle):
the DB-level effect of the kill (an aborted commit transaction) is what is exercised.
"""

from __future__ import annotations

import asyncio
import os
import select
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from invariants import assert_invariants
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

GENESIS = "0" * 64
READY_TIMEOUT_S = 30
REASONS = (
    "finished",
    "timed_out",
    "service_failure",
    "budget_exhausted",
    "terminated_by_teacher",  # plan-2 close function until plan 11's teacher action
    "access_revoked",
)


async def seed(pg: Any, state: str = "ACTIVE") -> tuple[uuid.UUID, uuid.UUID]:
    """A committed session (snapshot, opening turn, hand-off, enrolment) or a STARTING row."""
    site, sid = uuid.uuid4(), uuid.uuid4()
    await pg.run(
        "super",
        "INSERT INTO assess_sites (id, moodle_site_uuid, base_url) VALUES (:i, :u, 'https://m.example')",
        {"i": site, "u": str(site)},
    )
    now = datetime.now(UTC)
    if state == "STARTING":
        # requested five minutes ago, past T: what a pre-flight killed mid-way leaves behind
        await pg.run(
            "super",
            "INSERT INTO assess_sessions (id, site_id, cmid, student_userid, state, epoch, "
            "created_at, starting_expires_at) VALUES (:s, :site, 1, 1, 'STARTING', 1, "
            "now() - interval '5 minutes', now() - interval '4 minutes')",
            {"s": sid, "site": site},
        )
    if state != "STARTING":
        await pg.run(
            "super",
            "INSERT INTO assess_sessions (id, site_id, cmid, student_userid, state, epoch, "
            "committed_at, duration_s, deadline_at) VALUES (:s, :site, 1, 1, 'STARTING', 1, "
            ":c, 1800, :d)",
            {"s": sid, "site": site, "c": now, "d": now + timedelta(seconds=1800)},
        )
        for sql in (
            "INSERT INTO assess_snapshots (session_id, ciphertext, content_hash) "
            "VALUES (:s, '\\x01', :g)",
            "INSERT INTO assess_session_keys (session_id, kek_wrapped, kek_kid, key_version) "
            "VALUES (:s, '\\x05', 'k1', 1)",
            "INSERT INTO assess_turns (session_id, turn_no, role, kind, ciphertext, turn_hash, "
            "prev_hash) VALUES (:s, 1, 'bot', 'opening', '\\x02', 'h1', :g)",
            "UPDATE assess_sessions SET state = 'ACTIVE' WHERE id = :s",
            "INSERT INTO assess_session_handoff (session_id, session_state, epoch, sealed_dek, "
            "handoff_kid) VALUES (:s, 'ACTIVE', 1, '\\x03', 'k1')",
        ):
            await pg.run("super", sql, {"s": sid, "g": GENESIS})
    return site, sid


async def one(pg: Any, sql: str, **params: Any) -> Any:
    return (await pg.run("super", sql, params))[0][0]


def hold(url: str, sql: str, params: dict[str, Any]) -> subprocess.Popen[str]:
    """A real client process that runs ``sql`` in an open transaction, says READY and waits."""
    code = textwrap.dedent(
        f"""
        import asyncio, sys
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine
        async def main():
            engine = create_async_engine({url!r})
            async with engine.connect() as conn:
                await conn.execute(text({sql!r}), {params!r})
                print("READY", flush=True)
                await asyncio.sleep(3600)
        asyncio.run(main())
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    # bounded readiness wait (CodeRabbit review 5808100754): a child that never says READY
    # (stuck connecting, crashed) is killed and reaped, never waited on forever
    ready, _, _ = select.select([proc.stdout], [], [], READY_TIMEOUT_S)
    line = proc.stdout.readline().strip() if ready else ""
    if line != "READY":
        proc.kill()
        _out, err = proc.communicate(timeout=10)
        raise AssertionError(
            f"child not ready within {READY_TIMEOUT_S} s: {err[-500:]}"
        )
    return proc


def kill9(proc: subprocess.Popen[str]) -> None:
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(10)


@pytest.mark.parametrize("reason", REASONS)
async def test_each_close_reason_by_row_count(pg, reason: str) -> None:  # type: ignore[no-untyped-def]
    from vektra_assess.api.session_gate import LIVE, GateRejected, gate
    from vektra_assess.tokens import SessionClaims

    site, sid = await seed(pg)
    assert (
        await pg.run(
            "worker", "SELECT assess_close_session(:s, 1, :r)", {"s": sid, "r": reason}
        )
    )[0][0]
    assert (
        await one(
            pg,
            "SELECT count(*) FROM assess_session_handoff WHERE session_id = :s",
            s=sid,
        )
        == 0
    )
    assert (
        await one(
            pg,
            "SELECT count(*) FROM assess_jobs WHERE kind = 'transcript_generate' "
            "AND args->>'session_id' = :s",
            s=str(sid),
        )
        == 1
    )
    assert await one(pg, "SELECT epoch FROM assess_sessions WHERE id = :s", s=sid) == 2
    session_no = await one(
        pg, "SELECT session_no FROM assess_sessions WHERE id = :s", s=sid
    )
    # the old session token (epoch 1) is refused by the gate as CLOSED, before any decrypt
    engine = create_async_engine(pg.url("api"))
    try:
        async with engine.connect() as conn:
            claims = SessionClaims(
                sub=1,
                site=site,
                cmid=1,
                sid=sid,
                seq=int(session_no),
                epoch=1,
                jti="x",
                iat=0,
                exp=0,
            )
            with pytest.raises(GateRejected) as exc:
                await gate(conn, claims, sid, allowed=LIVE)
            assert exc.value.step == "closed"  # the old token reaches the closed check
    finally:
        await engine.dispose()
    await assert_invariants(pg, f"close:{reason}", expect_sessions=1)


async def test_kill9_mid_preflight_commit_leaves_the_student_eligible(pg) -> None:  # type: ignore[no-untyped-def]
    from vektra_assess.worker.reaper import reap_starting

    _site, sid = await seed(pg, "STARTING")
    proc = hold(
        pg.url("worker"),
        "INSERT INTO assess_snapshots (session_id, ciphertext, content_hash) "
        "VALUES (:s, '\\x01', 'x')",
        {"s": str(sid)},
    )
    kill9(proc)  # the commit transaction dies with the process
    await asyncio.sleep(0.5)
    rows = (
        "SELECT (SELECT count(*) FROM assess_snapshots WHERE session_id = :s) + "
        "(SELECT count(*) FROM assess_session_keys WHERE session_id = :s) + "
        "(SELECT count(*) FROM assess_turns WHERE session_id = :s)"
    )
    assert await one(pg, rows, s=sid) == 0
    engine = create_async_engine(pg.url("worker"))
    try:
        assert await reap_starting(engine.connect, 60) >= 1
    finally:
        await engine.dispose()
    assert (
        await one(pg, "SELECT count(*) FROM assess_sessions WHERE id = :s", s=sid) == 0
    )
    assert await one(pg, rows, s=sid) == 0
    assert (
        await one(
            pg,
            "SELECT reason_code FROM assess_start_attempts WHERE session_id = :s "
            "AND outcome = 'rolled_back'",
            s=sid,
        )
        == "start_timeout"
    )
    await assert_invariants(pg, "kill9:preflight", expect_sessions=0)


async def test_kill9_of_the_api_mid_reply_releases_the_claim(pg) -> None:  # type: ignore[no-untyped-def]
    from vektra_assess.turn_markers import REPLY_CLAIM_KEY_SQL

    _site, sid = await seed(pg)
    try_lock = f"SELECT pg_try_advisory_lock({REPLY_CLAIM_KEY_SQL})"
    unlock = f"SELECT pg_advisory_unlock({REPLY_CLAIM_KEY_SQL})"
    proc = hold(
        pg.url("api"),
        f"SELECT pg_advisory_lock({REPLY_CLAIM_KEY_SQL})",
        {"sid": str(sid)},
    )
    try:
        engine = create_async_engine(pg.url("worker"))
        from sqlalchemy import text

        async with engine.connect() as conn:
            assert (
                await conn.execute(text(try_lock), {"sid": str(sid)})
            ).scalar() is False
            kill9(proc)
            deadline = time.monotonic() + 10
            got = False
            while time.monotonic() < deadline and not got:
                got = bool(
                    (await conn.execute(text(try_lock), {"sid": str(sid)})).scalar()
                )
                await asyncio.sleep(0.1)
            assert got, "the live-reply claim must die with the API process"
            await conn.execute(text(unlock), {"sid": str(sid)})
        await engine.dispose()
    finally:
        if proc.poll() is None:
            kill9(proc)
    await assert_invariants(pg, "kill9:api-mid-reply", expect_sessions=1)


async def test_postgres_restart_mid_turn_never_duplicates_a_turn(pg_fixed_port) -> None:  # type: ignore[no-untyped-def]
    import docker

    pg = pg_fixed_port
    _site, sid = await seed(pg)
    append = (
        "SELECT assess_append_turn(CAST(:s AS uuid), 1, 0, 'student', 'normal', "
        "'\\x04'::bytea, 'h2', 'h1')"
    )
    proc = hold(pg.url("api"), append, {"s": str(sid)})
    try:
        docker.from_env().containers.get(pg.container_id).restart(timeout=5)
    finally:
        if proc.poll() is None:
            kill9(proc)
    for _ in range(60):
        try:
            await pg.run("super", "SELECT 1")
            break
        except Exception:
            await asyncio.sleep(0.5)
    turns = "SELECT count(*) FROM assess_turns WHERE session_id = :s"
    assert await one(pg, turns, s=sid) == 1  # the open append died with the restart
    first = (await pg.run("api", append, {"s": str(sid)}))[0][0]
    again = (await pg.run("api", append, {"s": str(sid)}))[0][0]
    assert first == 2 and again is None  # the retry is refused by the sequence check
    assert await one(pg, turns, s=sid) == 2
    assert (
        await one(
            pg,
            "SELECT seq = 1 AND pending_reply_since IS NOT NULL FROM assess_sessions WHERE id = :s",
            s=sid,
        )
        is True
    )
    await assert_invariants(pg, "postgres-restart:mid-turn", expect_sessions=1)


async def _session_rows(pg: Any, sid: Any) -> tuple[Any, ...]:
    return tuple(
        (
            await pg.run(
                "super",
                "SELECT s.state, s.epoch, "
                "(SELECT count(*) FROM assess_session_handoff h WHERE h.session_id = s.id), "
                "(SELECT count(*) FROM assess_jobs j WHERE j.kind = 'transcript_generate' "
                " AND j.args->>'session_id' = CAST(s.id AS text)), "
                "(SELECT count(*) FROM assess_turns t WHERE t.session_id = s.id) "
                "FROM assess_sessions s WHERE s.id = :s",
                {"s": sid},
            )
        )[0]
    )


async def test_kill9_of_the_api_mid_lazy_close_leaves_the_session_live_then_closes_once(
    pg,
) -> None:  # type: ignore[no-untyped-def]
    """The API's lazy close (gate step 5) killed inside its transaction: nothing of the close
    survives (state, epoch, hand-off and job unchanged), and the next request closes exactly once."""
    _site, sid = await seed(pg)
    before = await _session_rows(pg, sid)
    assert before == ("ACTIVE", 1, 1, 0, 1)
    close = "SELECT assess_close_session(CAST(:s AS uuid), 1, 'timed_out')"
    proc = hold(pg.url("api"), close, {"s": str(sid)})
    kill9(proc)
    await asyncio.sleep(0.5)
    assert await _session_rows(pg, sid) == before  # the killed close left no trace
    await assert_invariants(pg, "kill9:api-mid-lazy-close", expect_sessions=1)
    assert (await pg.run("api", close, {"s": str(sid)}))[0][0] is True
    # a second request racing the first: whatever it returns, it closes nothing again (no
    # second job, no second epoch bump), the exactly-one close of REQ-067
    await pg.run("api", close, {"s": str(sid)})
    assert await _session_rows(pg, sid) == ("CLOSED", 2, 0, 1, 1)
    await assert_invariants(pg, "lazy-close:after-kill", expect_sessions=1)


async def test_an_epoch_bump_mid_stream_refuses_the_late_append(pg) -> None:  # type: ignore[no-untyped-def]
    """A reply streamed under epoch 1 when a close bumps the epoch to 2: its late persist is
    refused, the turn count is unchanged, and the session stays closed exactly once."""
    _site, sid = await seed(pg)
    append = (
        "SELECT assess_append_turn(CAST(:s AS uuid), 1, 0, 'student', 'normal', "
        "'\\x04'::bytea, 'h2', 'h1')"
    )
    assert (
        await pg.run(
            "worker",
            "SELECT assess_close_session(CAST(:s AS uuid), 1, 'service_failure')",
            {"s": str(sid)},
        )
    )[0][0] is True
    late = (await pg.run("api", append, {"s": str(sid)}))[0][0]
    assert late is None
    assert await _session_rows(pg, sid) == ("CLOSED", 2, 0, 1, 1)
    await assert_invariants(pg, "epoch-bump:mid-stream", expect_sessions=1)


async def test_control_the_same_append_succeeds_without_the_epoch_bump(pg) -> None:  # type: ignore[no-untyped-def]
    """Negative control for the epoch scenario: without the close the append is accepted, so the
    refusal above is caused by the epoch, not by a broken statement."""
    _site, sid = await seed(pg)
    append = (
        "SELECT assess_append_turn(CAST(:s AS uuid), 1, 0, 'student', 'normal', "
        "'\\x04'::bytea, 'h2', 'h1')"
    )
    assert (await pg.run("api", append, {"s": str(sid)}))[0][0] == 2
    assert (await _session_rows(pg, sid))[4] == 2
