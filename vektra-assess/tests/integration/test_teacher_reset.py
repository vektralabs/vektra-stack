"""REQ-087 reset through the real route (plan assess-teacher-ops 11b); REQ-092 is out of v1.

Real Postgres, the restricted API role, sessions through the real Start and pre-flight, the
student's render evaluation seeded as the worker writes it. Outcomes are counted on rows and
bytes: the reset row and its encrypted reason, the earlier transcript's stored bytes (hash), the
new session #N, no notice row, and the reason canary in every place it must not reach.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.recipients import configure_recipients
from support.sessions import seed_session
from support.teacher_ops import (
    body,
    close,
    count,
    list_headers,
    live_sessions,
    seed_render,
    seed_teacher_gate,
    teacher,
    with_transcript_key,
)
from support.world import err, world

from integration.test_session_preflight import _reset
from vektra_assess.crypto.teacher_reason import decrypt_reason
from vektra_assess.transcript.facts import CloseFacts
from vektra_assess.worker.transcript_gen import GenerationDeps, generate_transcript

pytestmark = pytest.mark.integration

CANARY = "CANARY-RESET-REASON-2718 certificato medico"


class Facts:
    async def close_facts(self, **_: Any) -> CloseFacts:
        return CloseFacts(course_shortname="EA 2026/27")


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


def url(cmid: int, sid: Any) -> str:
    return f"/api/v1/assess/activities/{cmid}/sessions/{sid}/reset"


async def reset(w, cmid, sid, **over: Any):  # type: ignore[no-untyped-def]
    return await w.client.post(
        url(cmid, sid), json=body(**{"reason": CANARY, **over}), headers=teacher()
    )


async def grants(pg, site) -> int:  # type: ignore[no-untyped-def]
    return await count(
        pg, "SELECT count(*) FROM assess_reset_grants WHERE site_id = :s", s=site
    )


async def test_reset_refusals_then_a_new_session_with_earlier_bytes_unchanged(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        ring = with_transcript_key(w)
        s = await seed_session(w, worker, "ACTIVE")
        await seed_teacher_gate(pg, w.site, s.cmid)
        await seed_render(pg, w.site, s.sub, s.cmid, moodle_reasons=[])

        # an ACTIVE session: refused (terminate first)
        r = await reset(w, s.cmid, s.session_id)
        assert r.status_code == 409 and err(r)["details"]["reason"] == "not_closed"
        await close(worker, s.session_id)
        # no reason: refused
        for blank in ("", "   "):
            r = await reset(w, s.cmid, s.session_id, reason=blank)
            assert r.status_code == 400, r.text
            assert err(r)["details"]["fields"] == [
                {"field": "reason", "code": "required", "range": "1-1000"}
            ]
        assert await grants(pg, w.site) == 0

        # transcript #1, its stored bytes hashed before the reset
        recipients = await configure_recipients(pg, w.site, s.cmid)
        got = await generate_transcript(
            GenerationDeps(
                kek=s.kek, transcript_kek=ring, facts=Facts(), recipients=recipients
            ),
            worker.connect,
            s.session_id,
        )
        assert got is not None and got.created

        async def stored_hash() -> str:
            (row,) = await pg.run(
                "super",
                "SELECT ciphertext, content_hash FROM assess_transcripts "
                "WHERE session_id = :s",
                {"s": s.session_id},
            )
            return hashlib.sha256(bytes(row[0])).hexdigest() + row[1]

        before = await stored_hash()

        # after the end date: refused with a pointer to the date field
        await pg.run(
            "super",
            "UPDATE assess_activity_config SET end_at = now() - interval '1 minute' "
            "WHERE site_id = :s AND cmid = :c",
            {"s": w.site, "c": s.cmid},
        )
        r = await reset(w, s.cmid, s.session_id)
        assert r.status_code == 409, r.text
        assert err(r)["code"] == "ERR-ASSESS-024"
        assert err(r)["details"] == {
            "reason": "gate",
            "gate": "end_date_passed",
            "field": "available_until",
        }
        assert await grants(pg, w.site) == 0
        # the date moved: accepted
        await pg.run(
            "super",
            "UPDATE assess_activity_config SET end_at = now() + interval '1 day' "
            "WHERE site_id = :s AND cmid = :c",
            {"s": w.site, "c": s.cmid},
        )
        r = await reset(w, s.cmid, s.session_id)
        assert r.status_code == 201, r.text
        granted = r.json()
        assert granted["session_no_next"] == 2 and granted["student_userid"] == s.sub
        assert (
            granted["include_previous_turns"] is False
        )  # closed as "finished": default OFF
        assert CANARY not in r.text
        # a second reset of the same session: one grant only
        r = await reset(w, s.cmid, s.session_id)
        assert r.status_code == 409 and err(r)["details"]["reason"] == "already_reset"
        assert await grants(pg, w.site) == 1
        # REQ-092 is out of v1: no notice row, ever
        assert (
            await count(
                pg, "SELECT count(*) FROM assess_deliveries WHERE kind = 'reset_notice'"
            )
            == 0
        )

        # session #2 starts through the real Start and pre-flight; #1 is untouched
        s2 = await seed_session(w, worker, "ACTIVE")
        rows = await pg.run(
            "super",
            "SELECT session_no, state FROM assess_sessions WHERE site_id = :s "
            "AND student_userid = :u ORDER BY session_no",
            {"s": w.site, "u": s.sub},
        )
        assert [tuple(x) for x in rows] == [(1, "CLOSED"), (2, "ACTIVE")]
        assert s2.session_id != s.session_id
        assert await stored_hash() == before

        # the reason: in the grants table encrypted, in the grader list readable, nowhere else
        (g,) = await pg.run(
            "super",
            "SELECT id, reason_ct FROM assess_reset_grants WHERE site_id = :s",
            {"s": w.site},
        )
        assert decrypt_reason(ring, "assess_reset_grants", g[0], bytes(g[1])) == CANARY
        assert CANARY.encode() not in bytes(g[1])
        audit = await pg.run(
            "super",
            "SELECT event, data FROM assess_audit_events WHERE site_id = :s",
            {"s": w.site},
        )
        resets = [a[1] for a in audit if a[0] == "assess_teacher_reset"]
        assert len(resets) == 1 and resets[0]["reset_id"] == str(g[0])
        jobs = await pg.run("super", "SELECT args::text FROM assess_jobs")
        await close(worker, s2.session_id)
        got2 = await generate_transcript(
            GenerationDeps(
                kek=s2.kek, transcript_kek=ring, facts=Facts(), recipients=recipients
            ),
            worker.connect,
            s2.session_id,
        )
        assert got2 is not None and got2.created
        from vektra_assess.transcript import store

        texts = []
        for sid in (s.session_id, s2.session_id):
            (t,) = await pg.run(
                "super",
                "SELECT id, ciphertext, dek_wrapped, content_hash FROM assess_transcripts "
                "WHERE session_id = :s",
                {"s": sid},
            )
            texts.append(
                store.open_verified(ring, t[0], bytes(t[1]), bytes(t[2]), t[3]).decode()
            )
        turns = await pg.run(
            "super",
            "SELECT ciphertext FROM assess_turns WHERE session_id = :s",
            {"s": s2.session_id},
        )
        for place in (
            json.dumps([a[1] for a in audit]),
            json.dumps([j[0] for j in jobs]),
            *texts,
        ):
            assert CANARY not in place  # (a digit substring would match random hashes)
        assert all(CANARY.encode() not in bytes(t[0]) for t in turns)
        # the grader list shows it (and only there)
        r = await w.client.get(
            f"/api/v1/assess/activities/{s.cmid}/sessions", headers=list_headers()
        )
        assert r.status_code == 200, r.text
        (student,) = r.json()["students"]
        assert [x["reason"] for x in student["resets"]] == [CANARY]
        assert (
            student["resets"][0]["student_notice"] == "studente non avvisato via email"
        )


async def test_service_failure_ticks_the_previous_turns_and_an_explicit_choice_wins(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        with_transcript_key(w)
        cmid, seeded = await live_sessions(w, worker, [7001, 7002, 7003])
        await seed_teacher_gate(pg, w.site, cmid)
        for student, reason in (
            (7001, "service_failure"),
            (7002, "timed_out"),
            (7003, "service_failure"),
        ):
            await close(worker, seeded[student][0], reason)
            await seed_render(pg, w.site, student, cmid, moodle_reasons=[])
        r1 = await reset(w, cmid, seeded[7001][0])
        r2 = await reset(w, cmid, seeded[7002][0])
        r3 = await reset(w, cmid, seeded[7003][0], include_previous_turns=False)
        assert [r.json()["include_previous_turns"] for r in (r1, r2, r3)] == [
            True,
            False,
            False,
        ]
        stored = await pg.run(
            "super",
            "SELECT student_userid, include_previous_turns FROM assess_reset_grants "
            "WHERE site_id = :s ORDER BY student_userid",
            {"s": w.site},
        )
        assert [tuple(x) for x in stored] == [
            (7001, True),
            (7002, False),
            (7003, False),
        ]


async def test_the_moodle_half_comes_from_a_fresh_render_or_is_refreshed(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        with_transcript_key(w)
        cmid, seeded = await live_sessions(w, worker, [7101])
        sid = seeded[7101][0]
        await seed_teacher_gate(pg, w.site, cmid)
        await close(worker, sid)
        # no render evaluation yet: refused, a render check queued, retry later
        r = await reset(w, cmid, sid)
        assert (
            r.status_code == 409 and err(r)["details"]["reason"] == "gate_inputs_stale"
        )
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_jobs WHERE kind = 'render_check' "
                "AND (args->>'student_userid')::bigint = 7101",
            )
            == 1
        )
        # a stale one, or one computed without the Moodle half: the same
        await seed_render(pg, w.site, 7101, cmid, moodle_reasons=[], age="1 hour")
        r = await reset(w, cmid, sid)
        assert err(r)["details"]["reason"] == "gate_inputs_stale"
        await seed_render(pg, w.site, 7101, cmid, moodle_reasons=None)
        r = await reset(w, cmid, sid)
        assert err(r)["details"]["reason"] == "gate_inputs_stale"
        # no longer eligible (the grade was hidden again): refused with the gate
        await seed_render(pg, w.site, 7101, cmid, moodle_reasons=["not_released"])
        r = await reset(w, cmid, sid)
        assert r.status_code == 409
        assert err(r)["details"] == {"reason": "gate", "gate": "not_released"}
        assert await grants(pg, w.site) == 0
        await seed_render(pg, w.site, 7101, cmid, moodle_reasons=[])
        r = await reset(w, cmid, sid)
        assert r.status_code == 201, r.text


async def test_concurrent_resets_of_one_session_yield_one(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        with_transcript_key(w)
        cmid, seeded = await live_sessions(w, worker, [7201])
        sid = seeded[7201][0]
        await seed_teacher_gate(pg, w.site, cmid)
        await close(worker, sid)
        await seed_render(pg, w.site, 7201, cmid, moodle_reasons=[])
        got = await asyncio.gather(*(reset(w, cmid, sid) for _ in range(4)))
        assert sorted(r.status_code for r in got) == [201, 409, 409, 409]
        assert await grants(pg, w.site) == 1


async def test_a_session_of_another_activity_or_an_older_one_is_not_reset(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        with_transcript_key(w)
        cmid, seeded = await live_sessions(w, worker, [7301])
        sid = seeded[7301][0]
        await seed_teacher_gate(pg, w.site, cmid)
        await seed_teacher_gate(pg, w.site, cmid + 1000)
        await close(worker, sid)
        await seed_render(pg, w.site, 7301, cmid, moodle_reasons=[])
        # the session named under another activity the teacher grades: 404, nothing written
        r = await reset(w, cmid + 1000, sid)
        assert r.status_code == 404 and err(r)["code"] == "ERR-ASSESS-008"
        r = await reset(w, cmid, "not-a-uuid")
        assert r.status_code == 400
        assert await grants(pg, w.site) == 0


async def test_a_disabled_activity_refuses_the_reset_without_waiting_for_a_render(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """Review 11b-1: the render short-circuits a disabled activity (no Moodle half is ever
    written), so the database gates must refuse first, not ``gate_inputs_stale`` forever."""
    async with world(pg) as w:
        with_transcript_key(w)
        cmid, seeded = await live_sessions(w, worker, [7501])
        sid = seeded[7501][0]
        await seed_teacher_gate(pg, w.site, cmid)
        r = await w.client.post(
            f"/api/v1/assess/activities/{cmid}/terminate",
            json=body(reason="chiusa", expected_active_count=1, also_disable=True),
            headers=teacher(),
        )
        assert r.status_code == 200, r.text
        renders = (
            "SELECT count(*) FROM assess_jobs WHERE kind = 'render_check' "
            "AND (args->>'student_userid')::bigint = 7501"
        )
        before = await count(pg, renders)
        r = await reset(w, cmid, sid)
        assert r.status_code == 409, r.text
        assert err(r)["details"] == {"reason": "gate", "gate": "not_enabled"}
        assert await count(pg, renders) == before
        assert await grants(pg, w.site) == 0


async def test_a_session_rolled_back_while_the_reset_waits_is_not_found(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4506916908: a terminate holding the activity row deletes the student's only
    (STARTING) session while the reset waits for that lock: 404, never an index error."""
    from sqlalchemy import text
    from support.teacher_ops import start

    async with world(pg) as w:
        with_transcript_key(w)
        cmid, _seeded = await live_sessions(w, worker, [7601])
        await seed_teacher_gate(pg, w.site, cmid)
        sid, _token = await start(w, 7602, cmid)
        async with w.engine.connect() as term:
            tx = await term.begin()
            await term.execute(
                text(
                    "SELECT 1 FROM assess_activity_config WHERE site_id = :s AND cmid = :c "
                    "FOR UPDATE"
                ),
                {"s": w.site, "c": cmid},
            )
            task = asyncio.create_task(reset(w, cmid, sid))
            await asyncio.sleep(0.5)
            assert not task.done()
            done = (
                await term.execute(
                    text("SELECT assess_rollback_starting(:s)"), {"s": sid}
                )
            ).scalar()
            assert done is True
            await tx.commit()
        r = await asyncio.wait_for(task, 10)
        assert r.status_code == 404, r.text
        assert err(r)["code"] == "ERR-ASSESS-008"
        assert await grants(pg, w.site) == 0


async def test_a_slow_render_is_dated_when_its_reads_began(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4506916946: ``assess_gate_inputs.refreshed_at`` is the database clock read
    BEFORE the Moodle reads (as the teacher_gate job does), so a reset never takes facts older
    than the bound for fresh ones."""
    import logging

    from integration.test_session_state import moodle_source
    from vektra_assess.jobs import JobKind
    from vektra_assess.worker.render import RenderDeps, make_render_handler
    from vektra_assess.worker.runner import JobContext

    class Slow:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        async def gate_facts(self, target: Any) -> Any:
            await asyncio.sleep(1.5)
            return await self.inner.gate_facts(target)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    async with world(pg) as w:
        s = await seed_session(w, worker, "CLOSED")
        (before,) = (await pg.run("super", "SELECT clock_timestamp()"))[0]
        handler = make_render_handler(
            RenderDeps(settings=w.settings, source=Slow(moodle_source(worker)))
        )
        await handler(
            JobContext(
                1,
                JobKind.RENDER_CHECK,
                {"site_id": str(w.site), "student_userid": s.sub, "cmid": s.cmid},
                1,
                worker.connect,
                logging.getLogger("t"),
            )
        )
        (lag,) = (
            await pg.run(
                "super",
                "SELECT extract(epoch FROM refreshed_at - :b) FROM assess_gate_inputs "
                "WHERE site_id = :s AND student_userid = :u AND cmid = :c",
                {"b": before, "s": w.site, "u": s.sub, "c": s.cmid},
            )
        )[0]
        assert 0 <= float(lag) < 1.0  # dated before the 1.5 s of reads, not after


async def test_a_render_made_before_the_fallback_moved_is_not_current(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4506916913: the Moodle half of a reset must have been evaluated with the
    CURRENT REQ-091 date: after the teacher moves it, an earlier render is stale (refresh
    queued), and one made with the new date counts."""
    async with world(pg) as w:
        with_transcript_key(w)
        cmid, seeded = await live_sessions(w, worker, [7701])
        sid = seeded[7701][0]
        await seed_teacher_gate(pg, w.site, cmid)
        await close(worker, sid)
        (attested,) = (
            await pg.run(
                "super",
                "SELECT extract(epoch FROM attested_opening_at)::bigint "
                "FROM assess_activity_config WHERE site_id = :s AND cmid = :c",
                {"s": w.site, "c": cmid},
            )
        )[0]
        await seed_render(
            pg, w.site, 7701, cmid, moodle_reasons=[], attested_at=attested
        )
        # the teacher moves the date (later): the render above no longer describes the gate
        await pg.run(
            "super",
            "UPDATE assess_activity_config SET attested_opening_at = "
            "attested_opening_at + interval '1 hour' WHERE site_id = :s AND cmid = :c",
            {"s": w.site, "c": cmid},
        )
        r = await reset(w, cmid, sid)
        assert r.status_code == 409, r.text
        assert err(r)["details"]["reason"] == "gate_inputs_stale"
        await seed_render(
            pg, w.site, 7701, cmid, moodle_reasons=[], attested_at=attested + 3600
        )
        r = await reset(w, cmid, sid)
        assert r.status_code == 201, r.text


async def test_the_queued_render_unblocks_the_reset(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit nitpick (PR #179): no seeded render: the reset queues one, the REAL render
    handler runs it (recorded Moodle), and the retried reset is granted."""
    import logging

    from integration.test_session_state import moodle_source
    from vektra_assess.jobs import JobKind
    from vektra_assess.worker.render import RenderDeps, make_render_handler
    from vektra_assess.worker.runner import JobContext

    async with world(pg) as w:
        with_transcript_key(w)
        s = await seed_session(w, worker, "CLOSED")
        await seed_teacher_gate(pg, w.site, s.cmid)
        r = await reset(w, s.cmid, s.session_id)
        assert err(r)["details"]["reason"] == "gate_inputs_stale"
        (args,) = (
            await pg.run(
                "super",
                "SELECT args FROM assess_jobs WHERE kind = 'render_check' "
                "AND (args->>'student_userid')::bigint = :u AND state = 'ready'",
                {"u": s.sub},
            )
        )[0]
        handler = make_render_handler(
            RenderDeps(settings=w.settings, source=moodle_source(worker))
        )
        await handler(
            JobContext(
                1, JobKind.RENDER_CHECK, args, 1, worker.connect, logging.getLogger("t")
            )
        )
        r = await reset(w, s.cmid, s.session_id)
        assert r.status_code == 201, r.text
