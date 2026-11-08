"""REQ-081 terminate / disable through the real route (plan assess-teacher-ops 11b).

Real Postgres, the restricted API role, sessions committed by the real pre-flight. Every outcome
is counted on the rows: session states and close reasons, transcript jobs, hand-off rows, the
termination row and its audit, and on a refusal, that nothing moved.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.teacher_ops import (
    GRADER,
    body,
    close,
    count,
    live_sessions,
    seed_teacher_gate,
    start,
    teacher,
    with_transcript_key,
)
from support.world import configure_activity, err, world

from integration.test_session_preflight import _reset
from vektra_assess.crypto.teacher_reason import decrypt_reason

pytestmark = pytest.mark.integration

REASON = "CANARY-TERMINATE-REASON-3141 esame annullato"


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


def url(cmid: int) -> str:
    return f"/api/v1/assess/activities/{cmid}/terminate"


async def states(pg, site, cmid) -> dict[str, int]:  # type: ignore[no-untyped-def]
    rows = await pg.run(
        "super",
        "SELECT state || coalesce(':' || close_reason, ''), count(*) FROM assess_sessions "
        "WHERE site_id = :s AND cmid = :c GROUP BY 1",
        {"s": site, "c": cmid},
    )
    return {r[0]: int(r[1]) for r in rows}


async def test_three_active_sessions_close_through_the_single_path(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        ring = with_transcript_key(w)
        cmid, seeded = await live_sessions(w, worker, [6001, 6002, 6003])
        await seed_teacher_gate(pg, w.site, cmid)
        sids = [sid for sid, _t in seeded.values()]
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_session_handoff WHERE session_id = ANY(:i)",
                i=sids,
            )
            == 3
        )
        # one more student is still STARTING (pre-flight not run): rolled back, not consumed
        starting, _ = await start(w, 6004, cmid)

        r = await w.client.post(
            url(cmid),
            json=body(reason=REASON, expected_active_count=3),
            headers=teacher(),
        )
        assert r.status_code == 200, r.text
        got = r.json()
        assert (got["closed"], got["rolled_back"], got["disabled"]) == (3, 1, False)

        assert await states(pg, w.site, cmid) == {"CLOSED:terminated_by_teacher": 3}
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_jobs WHERE kind = 'transcript_generate' "
                "AND args->>'session_id' = ANY(:i)",
                i=[str(s) for s in sids],
            )
            == 3
        )
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_session_handoff WHERE session_id = ANY(:i)",
                i=sids,
            )
            == 0
        )
        # every close bumped the epoch (session tokens revoked)
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_sessions WHERE id = ANY(:i) AND epoch = 2",
                i=sids,
            )
            == 3
        )
        # the STARTING row is gone, its attempt says why, and the student is ELIGIBLE again
        assert (
            await count(
                pg, "SELECT count(*) FROM assess_sessions WHERE id = :i", i=starting
            )
            == 0
        )
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_start_attempts WHERE session_id = :i "
                "AND outcome = 'rolled_back' AND reason_code = 'terminated_by_teacher'",
                i=starting,
            )
            == 1
        )
        # the termination: its reason readable with the transcript key, never in the audit
        (row,) = await pg.run(
            "super",
            "SELECT id, reason_ct, closed_count, rolled_back_count, created_by "
            "FROM assess_terminations WHERE site_id = :s",
            {"s": w.site},
        )
        assert (
            decrypt_reason(ring, "assess_terminations", row[0], bytes(row[1])) == REASON
        )
        assert (row[2], row[3], row[4]) == (3, 1, str(GRADER))
        audit = await pg.run(
            "super",
            "SELECT event, reason_code, data FROM assess_audit_events WHERE site_id = :s "
            "ORDER BY id",
            {"s": w.site},
        )
        term = [a for a in audit if a[0] == "assess_teacher_terminate"]
        assert len(term) == 1 and term[0][2]["termination_id"] == str(row[0])
        closes = [a for a in audit if a[0] == "assess_close"]
        assert sorted(a[1] for a in closes[-3:]) == ["terminated_by_teacher"] * 3
        assert "CANARY-TERMINATE" not in json.dumps([a[2] for a in audit])
        assert "CANARY-TERMINATE" not in r.text


async def test_a_count_mismatch_is_409_and_changes_nothing(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        with_transcript_key(w)
        cmid, seeded = await live_sessions(w, worker, [6011, 6012])
        await seed_teacher_gate(pg, w.site, cmid)
        before = await states(pg, w.site, cmid)
        for expected in (1, 3):
            r = await w.client.post(
                url(cmid),
                json=body(reason=REASON, expected_active_count=expected),
                headers=teacher(),
            )
            assert r.status_code == 409, r.text
            assert err(r)["code"] == "ERR-ASSESS-025"
            assert err(r)["details"] == {"current": 2}
        assert await states(pg, w.site, cmid) == before == {"ACTIVE": 2}
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_terminations WHERE site_id = :s",
                s=w.site,
            )
            == 0
        )
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_session_handoff WHERE session_id = ANY(:i)",
                i=[s for s, _ in seeded.values()],
            )
            == 2
        )
        # a missing reason is a field error, not a 409, and changes nothing either
        r = await w.client.post(
            url(cmid),
            json=body(reason="  ", expected_active_count=2),
            headers=teacher(),
        )
        assert r.status_code == 400 and err(r)["code"] == "ERR-ASSESS-022"
        assert await states(pg, w.site, cmid) == {"ACTIVE": 2}


async def test_disable_blocks_start_and_re_enable_never_reopens(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        with_transcript_key(w)
        cmid, seeded = await live_sessions(w, worker, [6021])
        await seed_teacher_gate(pg, w.site, cmid)
        r = await w.client.post(
            url(cmid),
            json=body(reason=REASON, expected_active_count=1, also_disable=True),
            headers=teacher(),
        )
        assert r.status_code == 200 and r.json()["disabled"] is True, r.text
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_activity_config WHERE site_id = :s "
                "AND cmid = :c AND NOT enabled",
                s=w.site,
                c=cmid,
            )
            == 1
        )
        changed = await pg.run(
            "super",
            "SELECT data FROM assess_audit_events WHERE site_id = :s "
            "AND event = 'assess_teacher_config_changed' AND reason_code = 'enabled'",
            {"s": w.site},
        )
        assert [(c[0]["old"], c[0]["new"]) for c in changed] == [(True, False)]
        # an ELIGIBLE student cannot Start any more (the Start route checks ``enabled``)
        token = await w.presession(sub=6022, cmid=cmid)
        r = await w.client.post(
            "/api/v1/assess/sessions", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 409, r.text
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_sessions WHERE student_userid = 6022",
            )
            == 0
        )
        # re-enable: the CLOSED session stays CLOSED
        await configure_activity(pg, w.site, cmid, enabled=True)
        (sid, _tok) = seeded[6021]
        assert await states(pg, w.site, cmid) == {"CLOSED:terminated_by_teacher": 1}
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_sessions WHERE id = :i AND state = 'CLOSED'",
                i=sid,
            )
            == 1
        )


async def test_disable_alone_lets_active_sessions_run_and_close_normally(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """REQ-081: disabling (config ``enabled=false``) without terminating blocks new Starts only;
    an ACTIVE session still accepts turns and closes by its own path."""
    from support.preflight import FakeStreamRunner

    async with world(pg, assess_runner=FakeStreamRunner()) as w:
        cmid, seeded = await live_sessions(w, worker, [6031])
        sid, token = seeded[6031]
        await configure_activity(pg, w.site, cmid, enabled=False)
        r = await w.client.post(
            f"/api/v1/assess/sessions/{sid}/turns",
            json={"text": "Ho scelto quella struttura perche' era piu' semplice."},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 202, r.text
        driver = w.app.state.assess_turn_driver
        for task in list(driver.tasks.values()):
            await task
        await close(worker, sid, "finished")
        assert await states(pg, w.site, cmid) == {"CLOSED:finished": 1}


async def test_starting_rollback_is_refused_for_any_other_state(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """``assess_rollback_starting`` deletes a STARTING row only: an ACTIVE or CLOSED session is
    never deleted, whoever calls it (the API role holds EXECUTE)."""
    async with world(pg) as w:
        cmid, seeded = await live_sessions(w, worker, [6041])
        sid, _ = seeded[6041]
        assert (await pg.run("api", "SELECT assess_rollback_starting(:s)", {"s": sid}))[
            0
        ][0] is False
        await close(worker, sid)
        assert (await pg.run("api", "SELECT assess_rollback_starting(:s)", {"s": sid}))[
            0
        ][0] is False
        assert await states(pg, w.site, cmid) == {"CLOSED:finished": 1}


async def test_an_in_flight_start_and_a_terminate_with_disable_serialize(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """Review 11b-3: a Start holds the activity row FOR SHARE until its STARTING row commits;
    terminate locks it FOR UPDATE first. So a Start in flight is waited for, then counted and
    rolled back; a Start after the disable is refused. Two connections, real roles."""
    import asyncio

    from sqlalchemy import text

    from vektra_assess.api import start as start_route

    async with world(pg) as w:
        with_transcript_key(w)
        cmid, _seeded = await live_sessions(w, worker, [6051])
        await seed_teacher_gate(pg, w.site, cmid)
        params = {"s": w.site, "c": cmid}
        async with w.engine.connect() as starting:
            tx = await starting.begin()
            got = (await starting.execute(start_route.ACTIVITY_LOCK, params)).first()
            assert got.enabled is True
            term = asyncio.create_task(
                w.client.post(
                    url(cmid),
                    json=body(
                        reason=REASON, expected_active_count=1, also_disable=True
                    ),
                    headers=teacher(),
                )
            )
            await asyncio.sleep(0.5)
            assert not term.done()  # waits for the Start's lock
            sid = (
                await starting.execute(
                    text(
                        "INSERT INTO assess_sessions (site_id, cmid, student_userid, state, "
                        "starting_expires_at) VALUES (:s, :c, 6052, 'STARTING', "
                        "now() + interval '30 seconds') RETURNING id"
                    ),
                    params,
                )
            ).scalar()
            await tx.commit()
        r = await asyncio.wait_for(term, 10)
        assert r.status_code == 200, r.text
        assert (r.json()["closed"], r.json()["rolled_back"]) == (1, 1)
        assert (
            await count(pg, "SELECT count(*) FROM assess_sessions WHERE id = :i", i=sid)
            == 0
        )
        # a Start after the disable is refused inside its own transaction, nothing written
        token = await w.presession(sub=6053, cmid=cmid)
        r = await w.client.post(
            "/api/v1/assess/sessions", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 409
        assert (
            await count(
                pg, "SELECT count(*) FROM assess_sessions WHERE student_userid = 6053"
            )
            == 0
        )


async def test_the_start_rechecks_the_activity_under_its_lock(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """The advisory pre-check reads the activity on another connection; a disable landing
    between it and the Start transaction is still refused (the re-read under the lock)."""
    from unittest.mock import patch

    from vektra_assess.api import start as start_route

    async with world(pg) as w:
        cmid, _seeded = await live_sessions(w, worker, [6061])
        original = start_route._advisory

        async def advisory_then_disable(request, conn, claims):  # type: ignore[no-untyped-def]
            await original(request, conn, claims)
            await configure_activity(pg, w.site, cmid, enabled=False)

        token = await w.presession(sub=6062, cmid=cmid)
        with patch.object(start_route, "_advisory", advisory_then_disable):
            r = await w.client.post(
                "/api/v1/assess/sessions", headers={"Authorization": f"Bearer {token}"}
            )
        assert r.status_code == 409, r.text
        assert err(r)["details"]["reason"] == "activity_not_configured"
        assert (
            await count(
                pg, "SELECT count(*) FROM assess_sessions WHERE student_userid = 6062"
            )
            == 0
        )
