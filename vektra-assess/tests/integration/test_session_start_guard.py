"""M5.4: Start cap, the 3-consecutive-rollback rule and the per-site Start breaker (REQ-071)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.sessions import fixture_target, seed_session
from support.world import err, world

from integration.test_session_preflight import _reset
from vektra_assess.api.events import _poll
from vektra_assess.worker.preflight import rollback_start
from vektra_assess.worker.start_guard import StartLimits

pytestmark = pytest.mark.integration


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    await pg.run("super", "DELETE FROM assess_start_breaker")
    await pg.run("super", "DELETE FROM assess_jobs")
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


async def n(pg, sql: str, **params) -> int:  # type: ignore[no-untyped-def]
    return int((await pg.run("super", sql, params))[0][0])


async def rolled_back(w, worker, reason: str, limits: StartLimits, **kw):  # type: ignore[no-untyped-def]
    s = await seed_session(w, worker, "STARTING", **kw)
    async with worker.begin() as conn:
        assert await rollback_start(conn, s.session_id, reason, limits=limits)
    return s


class _Req:
    def __init__(self, app) -> None:  # type: ignore[no-untyped-def]
        self.app = app


async def test_the_sixth_start_within_an_hour_is_refused_and_consumes_nothing(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        await seed_session(w, worker, "ELIGIBLE")
        sub, cmid = fixture_target()
        for _ in range(5):
            await pg.run(
                "super",
                "INSERT INTO assess_start_attempts (site_id, cmid, student_userid, outcome) "
                "VALUES (:s, :c, :u, 'requested')",
                {"s": w.site, "c": cmid, "u": sub},
            )
        token = await w.presession(sub=sub, cmid=cmid)
        r = await w.client.post(
            "/api/v1/assess/sessions", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 429, r.text
        assert err(r)["details"] == {"reason": "start_rate_cap"}
        assert 3000 <= err(r)["retry_after"] <= 3600  # "Riprova tra X minuti"
        assert await n(pg, "SELECT count(*) FROM assess_sessions") == 0
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE event = 'assess_start_rate_cap_hit'",
            )
            == 1
        )
        # an hour later the same student may start again (the token was not consumed)
        await pg.run(
            "super",
            "UPDATE assess_start_attempts SET created_at = now() - interval '61 minutes'",
        )
        r = await w.client.post(
            "/api/v1/assess/sessions", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 202, r.text


async def test_three_consecutive_system_rollbacks_notify_teacher_and_operator_once(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    limits = StartLimits(breaker_rollbacks=1000)
    async with world(pg) as w:
        await rolled_back(w, worker, "capacity", limits)  # not a system failure
        for _ in range(2):
            await rolled_back(w, worker, "provider_unavailable", limits)
        assert (
            await n(pg, "SELECT count(*) FROM assess_jobs WHERE kind LIKE '%_notify'")
            == 0
        )
        last = await rolled_back(w, worker, "start_timeout", limits)
        jobs = await pg.run(
            "super",
            "SELECT kind, args->>'reason' FROM assess_jobs WHERE kind LIKE '%_notify' ORDER BY kind",
        )
        assert [tuple(j) for j in jobs] == [
            ("operator_notify", "rollback_cap"),
            ("teacher_notify", "rollback_cap"),
        ]
        # the student reads "Problema tecnico persistente" for that rollback
        state, _epoch, data = await _poll(_Req(w.app), last.session_id)  # type: ignore[arg-type]
        assert state is None and data == {"reason": "technical_problem_persistent"}
        # a fourth one in the same streak notifies nobody again
        await rolled_back(w, worker, "internal_error", limits)
        assert (
            await n(pg, "SELECT count(*) FROM assess_jobs WHERE kind LIKE '%_notify'")
            == 2
        )
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE event = 'assess_rollback_cap_hit'",
            )
            == 1
        )


async def test_the_site_breaker_opens_refuses_fast_half_opens_and_closes_on_commit(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    limits = StartLimits(breaker_rollbacks=2, rollback_notify_after=10)
    async with world(pg) as w:
        await rolled_back(
            w, worker, "provider_unavailable", limits, assignment="rubric_vis"
        )
        await rolled_back(w, worker, "capture_failed", limits, assignment="rubric_vis")
        assert (
            await n(
                pg, "SELECT count(*) FROM assess_start_breaker WHERE state = 'open'"
            )
            == 1
        )
        # open: refused fast, nothing consumed or written
        token = await w.presession(*fixture_target(name="simple"))
        jtis = await n(
            pg, "SELECT count(*) FROM assess_token_jti WHERE consumed_at IS NULL"
        )
        r = await w.client.post(
            "/api/v1/assess/sessions", headers={"Authorization": f"Bearer {token}"}
        )
        assert (
            r.status_code == 503 and err(r)["details"]["reason"] == "start_breaker_open"
        )
        assert await n(pg, "SELECT count(*) FROM assess_sessions") == 0
        assert (
            await n(
                pg, "SELECT count(*) FROM assess_token_jti WHERE consumed_at IS NULL"
            )
            == jtis
        )
        # cooldown over: half-open, one Start goes through and commits; the commit closes it
        await pg.run(
            "super",
            "UPDATE assess_start_breaker SET half_open_at = now() - interval '1 second'",
        )
        await seed_session(w, worker, "ACTIVE", assignment="simple")
        assert (
            await n(
                pg, "SELECT count(*) FROM assess_start_breaker WHERE state = 'closed'"
            )
            == 1
        )
        events = await pg.run(
            "super",
            "SELECT event FROM assess_audit_events WHERE event LIKE 'assess_start_breaker_%' "
            "ORDER BY id",
        )
        assert [e[0] for e in events] == [
            "assess_start_breaker_open",
            "assess_start_breaker_close",
        ]


async def test_half_open_lets_one_start_at_a_time(pg, worker) -> None:  # type: ignore[no-untyped-def]
    limits = StartLimits(breaker_rollbacks=1, rollback_notify_after=10)
    async with world(pg) as w:
        await rolled_back(
            w, worker, "provider_unavailable", limits, assignment="rubric_vis"
        )
        await pg.run(
            "super",
            "UPDATE assess_start_breaker SET half_open_at = now() - interval '1 second'",
        )
        await seed_session(
            w, worker, "STARTING", assignment="simple"
        )  # the probe Start
        token = await w.presession(*fixture_target(name="scale"))
        r = await w.client.post(
            "/api/v1/assess/sessions", headers={"Authorization": f"Bearer {token}"}
        )
        assert (
            r.status_code == 503 and err(r)["details"]["reason"] == "start_breaker_open"
        )


async def test_concurrent_starts_on_a_half_open_breaker_let_one_through(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """Review fix 8: "one Start at a time" while half-open is decided atomically."""
    import asyncio

    limits = StartLimits(breaker_rollbacks=1, rollback_notify_after=10)
    async with world(pg) as w:
        await rolled_back(
            w, worker, "provider_unavailable", limits, assignment="rubric_vis"
        )
        await pg.run(
            "super",
            "UPDATE assess_start_breaker SET half_open_at = now() - interval '1 second'",
        )
        tokens = []
        for name in ("simple", "scale", "files", "guide"):
            await seed_session(w, worker, "ELIGIBLE", assignment=name)
            tokens.append(await w.presession(*fixture_target(name=name)))
        replies = await asyncio.gather(
            *(
                w.client.post(
                    "/api/v1/assess/sessions",
                    headers={"Authorization": f"Bearer {t}"},
                )
                for t in tokens
            )
        )
        codes = sorted(r.status_code for r in replies)
        assert codes.count(202) <= 1, codes
        assert (
            await n(pg, "SELECT count(*) FROM assess_sessions WHERE state = 'STARTING'")
            <= 1
        )


async def test_moodle_unreachable_alerts_the_operator_once_per_window(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """REQ-071: a pre-flight rolled back because Moodle could not be read queues ONE
    content-free operator_notify per site per 15-minute window, in the rollback transaction;
    a rollback for a negated gate queues nothing."""
    limits = StartLimits(breaker_rollbacks=1000, rollback_notify_after=100)
    async with world(pg) as w:
        await rolled_back(w, worker, "not_released", limits, assignment="hiddengrade")
        assert (
            await n(
                pg, "SELECT count(*) FROM assess_jobs WHERE kind = 'operator_notify'"
            )
            == 0
        )
        for i in range(1, 6):  # five Starts of five students in the window
            await rolled_back(
                w, worker, "unreadable", limits, assignment="rubric_vis", user=f"stu{i}"
            )
        # the first alert already delivered: still none for the same window
        await pg.run(
            "super",
            "UPDATE assess_jobs SET state = 'done' WHERE kind = 'operator_notify'",
        )
        await rolled_back(
            w,
            worker,
            "capture_failed_policy_input_unreadable",
            limits,
            assignment="simple",
        )
        jobs = await pg.run(
            "super",
            "SELECT args FROM assess_jobs WHERE kind = 'operator_notify'",
        )
        assert [j[0] for j in jobs] == [
            {
                "site_id": str(w.site),
                "reason": "unreadable",
                "kind": "moodle_unreachable",
            }
        ]


async def test_capture_failures_count_as_system_rollbacks(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4498466290: the capture step's suffixed reasons
    (capture_failed_<reason>) are system rollbacks for the streak and the breaker."""
    limits = StartLimits(breaker_rollbacks=3, rollback_notify_after=3)
    async with world(pg) as w:
        for _ in range(3):
            last = await rolled_back(
                w, worker, "capture_failed_attempt_unreadable", limits
            )
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_jobs WHERE kind = 'teacher_notify' "
                "AND args->>'reason' = 'rollback_cap'",
            )
            == 1
        )
        assert (
            await n(
                pg, "SELECT count(*) FROM assess_start_breaker WHERE state = 'open'"
            )
            == 1
        )
        _state, _e, data = await _poll(_Req(w.app), last.session_id)  # type: ignore[arg-type]
        assert data == {"reason": "technical_problem_persistent"}


async def test_concurrent_rollbacks_at_the_threshold_open_the_breaker(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4498466324: two system rollbacks reaching the threshold together must not
    each miss the other's attempt row; the per-site count is serialised through commit."""
    import asyncio

    limits = StartLimits(breaker_rollbacks=2, rollback_notify_after=10)
    async with world(pg) as w:
        a = await seed_session(w, worker, "STARTING", assignment="rubric_vis")
        b = await seed_session(w, worker, "STARTING", assignment="simple")
        first = await worker.connect()
        second = await worker.connect()
        try:
            await first.begin()
            assert await rollback_start(
                first, a.session_id, "internal_error", limits=limits
            )
            await second.begin()
            other = asyncio.create_task(
                rollback_start(second, b.session_id, "internal_error", limits=limits)
            )
            await asyncio.sleep(
                0.5
            )  # the second one runs while the first is uncommitted
            await first.commit()
            assert await asyncio.wait_for(other, 10)
            await second.commit()
        finally:
            await first.close()
            await second.close()
        assert (
            await n(
                pg, "SELECT count(*) FROM assess_start_breaker WHERE state = 'open'"
            )
            == 1
        )


async def test_an_older_start_committing_keeps_the_cooldown(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit review 5808100754 (preflight.py 573): only a Start begun in half-open (the
    probe) closes the breaker; one admitted before it opened that commits during the cooldown
    leaves it open."""
    from assess_moodle_support import Replay, make_client
    from support.preflight import FakePartB, activate, deps, job_context, kek_ring
    from test_context_capture import InProcessParser

    from vektra_assess.worker.preflight import make_preflight_handler
    from vektra_assess.worker.start_source import MoodleStartSource

    async with world(pg) as w:
        await activate(pg)
        s = await seed_session(w, worker, "STARTING")
        await pg.run(
            "super",
            "INSERT INTO assess_start_breaker (site_id, state, opened_at, half_open_at) "
            "VALUES (:s, 'open', now(), now() + interval '10 minutes')",
            {"s": w.site},
        )
        source = MoodleStartSource(
            make_client(Replay("4.3")), InProcessParser(), worker.connect
        )
        handler = make_preflight_handler(
            deps(w.settings, w.handoff_public, source, FakePartB(), kek=kek_ring())
        )
        await handler(job_context(worker, s.session_id))
        assert (
            await n(pg, "SELECT count(*) FROM assess_sessions WHERE state = 'ACTIVE'")
            == 1
        )
        assert (
            await n(
                pg, "SELECT count(*) FROM assess_start_breaker WHERE state = 'open'"
            )
            == 1
        )


async def test_a_failed_half_open_probe_restarts_the_cooldown(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit review 5808100754 (start_guard.py 173-174): a system rollback while the
    breaker is half-open is the probe failing: the cooldown restarts, whatever the window
    count says."""
    limits = StartLimits(breaker_rollbacks=1000, rollback_notify_after=10)
    async with world(pg) as w:
        await pg.run(
            "super",
            "INSERT INTO assess_start_breaker (site_id, state, opened_at, half_open_at) "
            "VALUES (:s, 'open', now() - interval '1 hour', now() - interval '1 second')",
            {"s": w.site},
        )
        await rolled_back(
            w, worker, "provider_unavailable", limits, assignment="simple"
        )
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_start_breaker WHERE state = 'open' "
                "AND half_open_at > now() + interval '60 seconds'",
            )
            == 1
        )
