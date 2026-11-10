"""Chaos: the Postgres container restarted during a transcript delivery and during a Start burst.

A real container restart (``docker restart``) on a fixed host port, under the real code paths.

**During delivery** (REQ-075). The production ``deliver_transcript`` runs against the TLS mail
sink, which stores the message and withholds its 250. While the relay holds the copy, Postgres
restarts: the handler's settle meets a dead connection, so no row is SENT and its claim is
committed with no outcome. Once the claim is stale a new run resends with the same Message-ID.
Rows and sink copies: exactly one SENT row per recipient, an audited
``assess_delivery_possible_duplicate`` for every row whose message was already on the relay, never
more than two copies of one Message-ID.

**During a Start burst** (REQ-067, ARCH-068 outbox). Starts keep arriving while the container
restarts. Each Start is one transaction (STARTING row, ``requested`` attempt, ``preflight`` job,
``assess_start`` audit), so every student has all four rows or none, never a part of them; the
students whose Start died with the connection start again and get exactly one session; a second
Start of a student who has one is refused and adds nothing. The negative control inserts a STARTING
row without its job and shows that the per-student check reports it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from fixtures.mailsink import MailSink
from sqlalchemy.ext.asyncio import create_async_engine
from support.sessions import fixture_target
from support.world import beat, configure_activity, default_settings, world

import docker
from integration.test_delivery_handler import STALE, Clock, _deps, _ready, events, rows
from integration.test_session_preflight import _reset
from vektra_assess.delivery import handler as h

sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "tests" / "chaos" / "assess")
)
from invariants import assert_invariants, check

pytestmark = pytest.mark.integration

N_STARTS = 30
WAVE = 5


def _restart(container_id: str) -> None:
    docker.from_env().containers.get(container_id).restart(timeout=5)


async def _postgres_back(pg: Any, timeout_s: float = 60) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        try:
            await pg.run("super", "SELECT 1")
            return
        except Exception:
            if asyncio.get_running_loop().time() > deadline:
                raise
            await asyncio.sleep(0.5)


async def test_postgres_restart_between_data_and_250_leaves_one_sent_row_per_recipient(
    pg_fixed_port, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    pg = pg_fixed_port
    await _reset(pg)
    sink = MailSink(tmp_path / "sink").start()
    first = create_async_engine(pg.url("worker"))
    try:
        async with world(pg) as w:
            s, ring, config, _pair = await _ready(w, first)
            assert [r.status for r in await rows(pg, s.session_id)] == [
                "PENDING",
                "PENDING",
            ]
            clock = await Clock().sync(pg)
            sink.fault = "hold_after_data"
            task = asyncio.create_task(
                h.deliver_transcript(
                    _deps(ring, config, sink, clock), first.connect, s.session_id
                )
            )
            held = await asyncio.to_thread(sink.held.wait, 60)
            assert held, "the handler never reached DATA"
            await asyncio.sleep(
                0.5
            )  # the second row reaches DATA too, if it is going to
            on_relay = len(sink.messages)
            assert on_relay >= 1
            await asyncio.to_thread(_restart, pg.container_id)
            sink.fault = None
            sink.release.set()  # the 250 arrives after the database went away
            outcome = await asyncio.gather(task, return_exceptions=True)
            await _postgres_back(pg)

            stuck = await rows(pg, s.session_id)
            # a row whose settle found a live connection is SENT (its 250 did arrive); the
            # restart cost at least one settle: that row is PENDING with its claim committed
            lost = [r for r in stuck if r.status == "PENDING"]
            assert lost, (outcome, [r.status for r in stuck])
            assert {r.status for r in stuck} <= {"PENDING", "SENT"}
            claimed = [r for r in lost if r.attempt_started_at is not None]
            assert claimed, "the lost settle left its claim behind"
            for r in stuck:
                if r.status == "SENT":
                    assert len(sink.by_message_id(r.message_id)) == 1

            # the retry, past the dead claim's lease, on a fresh pool (the old one is dead)
            clock.now += STALE + STALE
            second = create_async_engine(pg.url("worker"))
            try:
                got = await h.deliver_transcript(
                    _deps(ring, config, sink, clock), second.connect, s.session_id
                )
                assert got is not None
                again = await h.deliver_transcript(
                    _deps(ring, config, sink, clock), second.connect, s.session_id
                )
                assert again is not None
            finally:
                await second.dispose()
            final = await rows(pg, s.session_id)
            assert [r.status for r in final] == ["SENT", "SENT"]
            dup = await events(pg, s.session_id, "assess_delivery_possible_duplicate")
            assert len(dup) == len(claimed)
            for r in final:
                copies = len(sink.by_message_id(r.message_id))
                assert 1 <= copies <= 2, (r.message_id, copies)
            assert len(sink.messages) == on_relay + len(
                lost
            )  # each lost row resent once
            await assert_invariants(
                pg,
                "postgres-restart:mid-delivery",
                expect_sessions=1,
                sites=[w.site],
                only=[
                    "one_sent_per_recipient",
                    "no_orphan_content",
                    "handoff_only_live",
                ],
                sink_copies=lambda mid: len(sink.by_message_id(mid)),
            )
            # negative control: the same rows against a relay that kept nothing must fail
            blind = await check(
                pg,
                "negative:mid-delivery-empty-relay",
                expect_sessions=1,
                sites=[w.site],
                only=["one_sent_per_recipient"],
                sink_copies=lambda _mid: 0,
            )
            assert not blind.ok and len(blind.violations["sink_copies"]) == len(final)
    finally:
        await first.dispose()
        sink.stop()


#: per student: sessions, requested attempts, preflight jobs, assess_start audit events
_PER_STUDENT = (
    "SELECT u.sub, "
    "(SELECT count(*) FROM assess_sessions s WHERE s.site_id = :site AND s.cmid = :c "
    " AND s.student_userid = u.sub), "
    "(SELECT count(*) FROM assess_start_attempts a WHERE a.site_id = :site AND a.cmid = :c "
    " AND a.student_userid = u.sub AND a.outcome = 'requested'), "
    "(SELECT count(*) FROM assess_jobs j JOIN assess_sessions s "
    " ON j.args->>'session_id' = s.id::text WHERE j.kind = 'preflight' "
    " AND s.site_id = :site AND s.cmid = :c AND s.student_userid = u.sub), "
    "(SELECT count(*) FROM assess_audit_events e JOIN assess_sessions s ON e.session_id = s.id "
    " WHERE e.event = 'assess_start' AND s.site_id = :site AND s.cmid = :c "
    " AND s.student_userid = u.sub) "
    "FROM unnest(CAST(:subs AS integer[])) AS u(sub) ORDER BY u.sub"
)


async def per_student(
    pg: Any, site: Any, cmid: int, subs: list[int]
) -> dict[int, tuple[int, ...]]:
    got = await pg.run("super", _PER_STUDENT, {"site": site, "c": cmid, "subs": subs})
    return {int(r[0]): tuple(int(x) for x in r[1:]) for r in got}


def partial(rows_: dict[int, tuple[int, ...]]) -> list[int]:
    """Students with some of a Start's rows but not all four (the outbox broken)."""
    return [sub for sub, r in rows_.items() if r not in ((0, 0, 0, 0), (1, 1, 1, 1))]


async def _start(w: Any, sub: int, cmid: int) -> str:
    """Mint and Start; the outcome code. A request that died with the database is ``error``."""
    try:
        m = await w.mint(sub, cmid)
        if m.status_code != 201:
            return f"mint:{m.status_code}"
        r = await w.client.post(
            "/api/v1/assess/sessions",
            headers={"Authorization": f"Bearer {m.json()['token']}"},
        )
        return str(r.status_code)
    except Exception as exc:  # the in-process API re-raises the dead connection
        return f"error:{type(exc).__name__}"


async def test_postgres_restart_during_a_start_burst_keeps_each_start_whole(
    pg_fixed_port,
) -> None:  # type: ignore[no-untyped-def]
    pg = pg_fixed_port
    await _reset(pg)
    settings = default_settings(VEKTRA_ASSESS_ADMISSION_K=500)
    async with world(pg, settings) as w:
        _sub, cmid = fixture_target()
        await configure_activity(pg, w.site, cmid)
        students = list(range(7000, 7000 + N_STARTS))
        outcomes: dict[int, str] = {}
        waves = [students[i : i + WAVE] for i in range(0, N_STARTS, WAVE)]

        async def wave(subs: list[int]) -> None:
            got = await asyncio.gather(*(_start(w, sub, cmid) for sub in subs))
            outcomes.update(zip(subs, got, strict=True))

        await wave(waves[0])  # before the restart
        assert all(o == "202" for o in outcomes.values()), outcomes
        restart = asyncio.create_task(asyncio.to_thread(_restart, pg.container_id))
        i = 1
        while (
            not restart.done() and i < len(waves) - 1
        ):  # Starts keep arriving meanwhile
            await wave(waves[i])
            i += 1
        await restart
        await _postgres_back(pg)
        await beat(pg)
        for subs in waves[i:]:
            await wave(subs)
        errors = [s for s, o in outcomes.items() if o != "202"]
        assert errors, f"the restart must have hit the burst: {outcomes}"
        assert len(errors) < N_STARTS

        rows_ = await per_student(pg, w.site, cmid, students)
        assert partial(rows_) == [], rows_
        for sub, o in outcomes.items():
            if o == "202":
                assert rows_[sub] == (1, 1, 1, 1), (sub, rows_[sub])

        # the students whose Start died start again; the others are refused, adding nothing
        for sub in students:
            want = "409" if rows_[sub] == (1, 1, 1, 1) else "202"
            for _ in range(5):  # a pooled connection the restart killed fails once
                got = await _start(w, sub, cmid)
                if not got.startswith("error"):
                    break
            assert got == want, (sub, got, rows_[sub])
        final = await per_student(pg, w.site, cmid, students)
        assert final == {sub: (1, 1, 1, 1) for sub in students}, final
        total = (
            await pg.run(
                "super",
                "SELECT count(*) FROM assess_sessions WHERE site_id = :s AND cmid = :c",
                {"s": w.site, "c": cmid},
            )
        )[0][0]
        assert total == N_STARTS
        await assert_invariants(
            pg, "postgres-restart:start-burst", expect_sessions=N_STARTS, sites=[w.site]
        )

        # negative control: a STARTING row whose job is missing is reported by the same check
        await pg.run(
            "super",
            "INSERT INTO assess_sessions (site_id, cmid, student_userid, state, "
            "starting_expires_at) VALUES (:s, :c, 9999, 'STARTING', now() + interval '1 minute')",
            {"s": w.site, "c": cmid},
        )
        broken = await per_student(pg, w.site, cmid, [*students, 9999])
        assert partial(broken) == [9999]
        await pg.run(
            "super",
            "DELETE FROM assess_sessions WHERE site_id = :s AND student_userid = 9999",
            {"s": w.site},
        )
