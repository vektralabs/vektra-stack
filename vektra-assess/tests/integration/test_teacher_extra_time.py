"""REQ-078 extra time and REQ-091 opening fallback through the real routes (plan 11b).

Extra time is counted on the committed session itself (``deadline_at - committed_at``); the
opening fallback on the config row and its audit, across the capability-matrix fixtures.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.sessions import fixture_target
from support.teacher_ops import (
    GRADER,
    body,
    close,
    count,
    live_sessions,
    seed_render,
    seed_teacher_gate,
    site_release,
    teacher,
    with_transcript_key,
)
from support.world import configure_activity, err, world

from integration.test_session_preflight import _reset

pytestmark = pytest.mark.integration


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


def extra_url(cmid: int, userid: int | str) -> str:
    return f"/api/v1/assess/activities/{cmid}/students/{userid}/extra-time"


async def test_plus_50_percent_on_30_minutes_is_a_45_minute_deadline(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        _sub, cmid = fixture_target()
        await site_release(w)
        await configure_activity(pg, w.site, cmid, cap_minutes=30)
        await seed_teacher_gate(pg, w.site, cmid)
        r = await w.client.put(
            extra_url(cmid, 8001), json=body(percent=50), headers=teacher()
        )
        assert r.status_code == 200, r.text
        assert r.json() == {
            "student_userid": 8001,
            "extra_seconds": 900,
            "duration_s": 2700,
        }
        await live_sessions(w, worker, [8001, 8002], cap_minutes=30)
        rows = await pg.run(
            "super",
            "SELECT student_userid, duration_s, "
            "extract(epoch FROM deadline_at - committed_at)::int FROM assess_sessions "
            "WHERE site_id = :s ORDER BY student_userid",
            {"s": w.site},
        )
        # exactly 45 min after the commit (no outage credited), the other student 30
        assert [tuple(x) for x in rows] == [(8001, 2700, 2700), (8002, 1800, 1800)]
        audit = await pg.run(
            "super",
            "SELECT data FROM assess_audit_events WHERE site_id = :s "
            "AND event = 'assess_teacher_extra_time'",
            {"s": w.site},
        )
        assert [a[0] for a in audit] == [
            {
                "cmid": cmid,
                "actor": GRADER,
                "student": 8001,
                "old_s": None,
                "new_s": 900,
            }
        ]


async def test_extra_time_is_immutable_once_started_and_has_no_reason(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        with_transcript_key(w)
        cmid, seeded = await live_sessions(w, worker, [8101], cap_minutes=30)
        await seed_teacher_gate(pg, w.site, cmid)
        sid = seeded[8101][0]

        async def put(**over: object):  # type: ignore[no-untyped-def]
            return await w.client.put(
                extra_url(cmid, 8101), json=body(**over), headers=teacher()
            )

        r = await put(minutes=10)
        assert r.status_code == 400, r.text
        assert err(r)["details"]["fields"] == [
            {"field": "extra_time", "code": "session_locked", "state": "ACTIVE"}
        ]
        await close(worker, sid)
        r = await put(minutes=10)
        assert err(r)["details"]["fields"][0]["state"] == "CLOSED"
        # a reset pending: the next session may get extra time
        await seed_render(pg, w.site, 8101, cmid, moodle_reasons=[])
        g = await w.client.post(
            f"/api/v1/assess/activities/{cmid}/sessions/{sid}/reset",
            json=body(reason="DSA certificata"),
            headers=teacher(),
        )
        assert g.status_code == 201, g.text
        r = await put(minutes=10)
        assert r.status_code == 200 and r.json()["extra_seconds"] == 600
        # REQ-078: no reason field exists
        r = await put(minutes=10, reason="DSA certificata")
        assert r.status_code == 400 and err(r)["code"] == "ERR-ASSESS-014"
        # BR-013 bounds through the route: total 240 prevails, +100 % needs a confirmation
        r = await put(minutes=211)
        assert err(r)["details"]["fields"][0]["code"] == "total_too_long"
        r = await put(percent=101)
        assert err(r)["details"]["fields"][0]["code"] == "confirm_over_100"
        r = await put(percent=101, confirm_over_100=True)
        assert r.status_code == 200
        r = await put(minutes=5, percent=5)
        assert err(r)["details"]["fields"][0]["code"] == "minutes_or_percent"
        r = await w.client.put(
            extra_url(cmid, "١٢"), json=body(minutes=5), headers=teacher()
        )
        assert r.status_code == 400  # an ASCII user id only
        assert "DSA" not in json.dumps(
            [
                a[0]
                for a in await pg.run(
                    "super",
                    "SELECT data FROM assess_audit_events WHERE site_id = :s",
                    {"s": w.site},
                )
            ]
        )


def fallback_url(cmid: int) -> str:
    return f"/api/v1/assess/activities/{cmid}/opening-fallback"


CLOSE = 1_900_000_000


def at(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def fb(epoch: int, **over: object) -> dict[str, object]:
    return body(
        **{
            "opening_at": at(epoch),
            "attestation": True,
            "attestation_text_version": "it-2026-1",
            **over,
        }
    )


async def caps(pg, site, release: str, **states: str) -> None:  # type: ignore[no-untyped-def]
    await pg.run(
        "super",
        "UPDATE assess_sites SET last_seen_release = :r WHERE id = :s",
        {"r": release, "s": site},
    )
    for flag, state in states.items():
        await pg.run(
            "super",
            "INSERT INTO assess_site_capabilities (site_id, flag, release, state, checked_at) "
            "VALUES (:s, :f, :r, :st, now()) ON CONFLICT (site_id, flag) DO UPDATE SET "
            "state = EXCLUDED.state, release = EXCLUDED.release, checked_at = now()",
            {"s": site, "f": flag, "r": release, "st": state},
        )


async def test_fallback_follows_the_readability_matrix(pg) -> None:  # type: ignore[no-untyped-def]
    """Quiz overrides readable + assignment extensions unreadable: the quiz's fallback is
    refused (both its close inputs readable), the assignment's accepted; a quiz whose grace is
    partial (closed quiz) lands on REQ-091 too."""
    async with world(pg) as w:
        rel = "4.5 (Build: 20250101)"
        await caps(
            pg,
            w.site,
            rel,
            quiz_overrides="readable",
            quiz_grace="readable",
            assign_extensions="unreadable",
            assign_user_overrides="unreadable",
        )
        quiz, assign = 901, 902
        for cmid, kind in ((quiz, "quiz"), (assign, "assign")):
            await configure_activity(pg, w.site, cmid, kind=kind, attested=False)
            await seed_teacher_gate(pg, w.site, cmid, kind=kind, close_at=CLOSE)
        r = await w.client.put(
            fallback_url(quiz), json=fb(CLOSE, activity_type="quiz"), headers=teacher()
        )
        assert r.status_code == 400, r.text
        assert err(r)["details"]["fields"] == [
            {"field": "opening_at", "code": "fallback_not_needed"}
        ]
        r = await w.client.put(fallback_url(assign), json=fb(CLOSE), headers=teacher())
        assert r.status_code == 200, r.text
        assert r.json()["opening_at"] == at(CLOSE) and r.json()["previous"] is None
        # the quiz's grace becomes partial: the fallback applies
        await caps(pg, w.site, rel, quiz_grace="partial")
        r = await w.client.put(
            fallback_url(quiz),
            json=fb(CLOSE + 60, activity_type="quiz"),
            headers=teacher(),
        )
        assert r.status_code == 200, r.text
        rows = await pg.run(
            "super",
            "SELECT cmid, extract(epoch FROM attested_opening_at)::bigint, attested_by "
            "FROM assess_activity_config WHERE site_id = :s ORDER BY cmid",
            {"s": w.site},
        )
        assert [tuple(x) for x in rows] == [
            (quiz, CLOSE + 60, str(GRADER)),
            (assign, CLOSE, str(GRADER)),
        ]
        audit = await pg.run(
            "super",
            "SELECT data FROM assess_audit_events WHERE site_id = :s "
            "AND event = 'assess_teacher_opening_attested' ORDER BY id",
            {"s": w.site},
        )
        assert [
            (a[0]["cmid"], a[0]["new"], a[0]["attestation_text_version"]) for a in audit
        ] == [(assign, CLOSE, "it-2026-1"), (quiz, CLOSE + 60, "it-2026-1")]


async def test_fallback_bounds_attestation_and_later_only_moves(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        with_transcript_key(w)
        cmid, seeded = await live_sessions(w, worker, [8201])
        # the recorded assignment: its cut-off is the activity close the date must follow
        await seed_teacher_gate(pg, w.site, cmid, close_at=CLOSE)
        await pg.run(
            "super",
            "UPDATE assess_activity_config SET attested_opening_at = to_timestamp(:t) "
            "WHERE site_id = :s AND cmid = :c",
            {"t": CLOSE + 100, "s": w.site, "c": cmid},
        )
        r = await w.client.put(
            fallback_url(cmid), json=fb(CLOSE - 1), headers=teacher()
        )
        assert err(r)["details"]["fields"][0]["code"] == "before_close"
        r = await w.client.put(
            fallback_url(cmid),
            json=fb(CLOSE + 200, attestation=False),
            headers=teacher(),
        )
        assert err(r)["details"]["fields"] == [
            {"field": "attestation", "code": "required"}
        ]
        # a session of this activity started: only a later date
        r = await w.client.put(
            fallback_url(cmid), json=fb(CLOSE + 50), headers=teacher()
        )
        assert err(r)["details"]["fields"] == [
            {"field": "opening_at", "code": "only_later", "current_at": CLOSE + 100}
        ]
        r = await w.client.put(
            fallback_url(cmid), json=fb(CLOSE + 200), headers=teacher()
        )
        assert r.status_code == 200, r.text
        assert r.json()["previous"] == at(CLOSE + 100)
        # an activity not configured yet has no date to attest
        await seed_teacher_gate(pg, w.site, 903, close_at=CLOSE)
        r = await w.client.put(fallback_url(903), json=fb(CLOSE), headers=teacher())
        assert err(r)["details"]["fields"][0]["code"] == "activity_not_configured"
        # refusals changed nothing
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE site_id = :s "
                "AND event = 'assess_teacher_opening_attested'",
                s=w.site,
            )
            == 1
        )
        assert seeded  # the session that made the later-only rule apply


async def test_the_start_gate_takes_the_later_of_close_and_teacher_date() -> None:
    """REQ-091 max() rule, through the stored date: the evaluator plan 8's pre-flight runs
    gives max(activity effective close, teacher date) in fallback, and max(automatic, teacher
    date) once every close input is readable (unit matrix in ``test_gates.py``)."""
    from vektra_assess.gates import Code, GateInputs, evaluate_start_gates

    now = CLOSE + 1_000
    base = {
        "now": now,
        "activity_kind": "quiz",
        "enabled": True,
        "attempt_in_progress": False,
        "has_submitted_attempt": True,
        "regrade_in_progress": False,
        "fully_graded": True,
        "released": True,
        "activity_close_at": CLOSE,
        "enrolled": True,
        "privacy_configured": True,
    }
    later = now + timedelta(hours=1).seconds
    fallback = evaluate_start_gates(
        GateInputs(**base, participant_closes=None, teacher_opening_at=later)  # type: ignore[arg-type]
    )
    assert (fallback.code, fallback.gate_at) == (Code.CLOSE_NOT_PASSED, later)
    no_date = evaluate_start_gates(GateInputs(**base, participant_closes=None))  # type: ignore[arg-type]
    assert no_date.code is Code.CLOSE_UNREADABLE  # NOT_AVAILABLE without a teacher date
    readable = evaluate_start_gates(
        GateInputs(**base, participant_closes=(now + 60,), teacher_opening_at=CLOSE)  # type: ignore[arg-type]
    )
    assert (readable.code, readable.gate_at) == (Code.CLOSE_NOT_PASSED, now + 60)


async def test_the_fallback_date_must_stay_before_the_end_date(pg) -> None:  # type: ignore[no-untyped-def]
    """Review 11b-2: an opening at or after "disponibile fino al" would make every Start
    impossible: refused on ``opening_at``, nothing stored."""
    async with world(pg) as w:
        await configure_activity(
            pg, w.site, 904, attested=False, end_at_sql=f"to_timestamp({CLOSE + 100})"
        )
        await seed_teacher_gate(pg, w.site, 904, close_at=CLOSE)
        r = await w.client.put(
            fallback_url(904), json=fb(CLOSE + 100), headers=teacher()
        )
        assert r.status_code == 400, r.text
        assert err(r)["details"]["fields"] == [
            {
                "field": "opening_at",
                "code": "not_before_end",
                "available_until": CLOSE + 100,
            }
        ]
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_activity_config WHERE site_id = :s "
                "AND attested_opening_at IS NOT NULL",
                s=w.site,
            )
            == 0
        )
        r = await w.client.put(
            fallback_url(904), json=fb(CLOSE + 99), headers=teacher()
        )
        assert r.status_code == 200, r.text


async def test_extra_time_waits_for_an_in_flight_start_and_then_refuses(pg) -> None:  # type: ignore[no-untyped-def]
    """Review 11b-5: a Start holds the activity row FOR SHARE until its STARTING row commits;
    the extra-time PUT locks it FOR UPDATE first, then sees the STARTING row and refuses."""
    import asyncio

    from sqlalchemy import text

    from vektra_assess.api import start as start_route

    async with world(pg) as w:
        await configure_activity(pg, w.site, 905, cap_minutes=30)
        await seed_teacher_gate(pg, w.site, 905)
        params = {"s": w.site, "c": 905}
        async with w.engine.connect() as starting:
            tx = await starting.begin()
            await starting.execute(start_route.ACTIVITY_LOCK, params)
            put = asyncio.create_task(
                w.client.put(
                    extra_url(905, 8301), json=body(minutes=10), headers=teacher()
                )
            )
            await asyncio.sleep(0.5)
            assert not put.done()
            await starting.execute(
                text(
                    "INSERT INTO assess_sessions (site_id, cmid, student_userid, state, "
                    "starting_expires_at) VALUES (:s, :c, 8301, 'STARTING', "
                    "now() + interval '30 seconds')"
                ),
                params,
            )
            await tx.commit()
        r = await asyncio.wait_for(put, 10)
        assert r.status_code == 400, r.text
        assert err(r)["details"]["fields"] == [
            {"field": "extra_time", "code": "session_locked", "state": "STARTING"}
        ]
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_extra_time WHERE site_id = :s",
                s=w.site,
            )
            == 0
        )


async def test_zero_extra_time_clears_a_grant(pg) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4506916916: 0 minutes / 0 % is a valid request (BR-013 0-240 / 0-200 %)
    and sets the grant back to none; it is not a database error."""
    async with world(pg) as w:
        await configure_activity(pg, w.site, 907, cap_minutes=30)
        await seed_teacher_gate(pg, w.site, 907)
        stored = "SELECT extra_seconds FROM assess_extra_time WHERE site_id = :s AND cmid = 907"
        for over, expected in (
            ({"minutes": 15}, 900),
            ({"minutes": 0}, 0),
            ({"percent": 50}, 900),
            ({"percent": 0}, 0),
        ):
            r = await w.client.put(
                extra_url(907, 8401), json=body(**over), headers=teacher()
            )
            assert r.status_code == 200, (over, r.text)
            assert r.json()["extra_seconds"] == await count(pg, stored, s=w.site)
            assert r.json()["extra_seconds"] == expected
        assert await count(pg, stored, s=w.site) == 0


async def test_a_cap_raised_after_a_grant_never_gives_more_than_240_minutes(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4506916939: 210 min granted at a 30-min cap, then the cap raised to 120:
    the Start freezes 240 min (the total prevails), not 330."""
    async with world(pg) as w:
        _sub, cmid = fixture_target()
        await site_release(w)
        await configure_activity(pg, w.site, cmid, cap_minutes=30)
        await seed_teacher_gate(pg, w.site, cmid)
        r = await w.client.put(
            extra_url(cmid, 8501),
            json=body(minutes=210, confirm_over_100=True),
            headers=teacher(),
        )
        assert r.status_code == 200 and r.json()["duration_s"] == 240 * 60, r.text
        await live_sessions(w, worker, [8501], cap_minutes=120)
        rows = await pg.run(
            "super",
            "SELECT duration_s, extract(epoch FROM deadline_at - committed_at)::int "
            "FROM assess_sessions WHERE site_id = :s AND student_userid = 8501",
            {"s": w.site},
        )
        assert [tuple(x) for x in rows] == [(240 * 60, 240 * 60)]
