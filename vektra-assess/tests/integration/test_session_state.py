"""M2.3: ``GET /state``, the render check job and the gate-refresh tick on a real database."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from assess_moodle_support import manifest, uid
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import PASSING_FACTS, FakeSource
from support.recipients import TEACHERS, configure_recipients
from support.sessions import seed_session
from support.world import configure_activity, default_settings, err, world

from integration.test_session_preflight import _reset, moodle_source
from vektra_assess.jobs import JobKind
from vektra_assess.worker.render import (
    RenderDeps,
    gate_refresh_tick,
    make_render_handler,
)
from vektra_assess.worker.runner import JobContext

pytestmark = pytest.mark.integration

M = manifest("4.3")
STU1 = uid("4.3", "stu1")


def cmid(name: str) -> int:
    return int(M["assigns"][name]["cmid"])


async def n(pg, sql: str, **params) -> int:  # type: ignore[no-untyped-def]
    return int((await pg.run("super", sql, params))[0][0])


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    for table in ("assess_render_checks", "assess_render_state", "assess_gate_inputs"):
        await pg.run("super", f"DELETE FROM {table}")
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


async def state(w, token: str):  # type: ignore[no-untyped-def]
    return await w.client.get(
        "/api/v1/assess/state", headers={"Authorization": f"Bearer {token}"}
    )


async def render(
    w, worker, sub: int, cm: int, source=None, graders=None, recipients=None
) -> None:  # type: ignore[no-untyped-def]
    handler = make_render_handler(
        RenderDeps(
            settings=w.settings,
            source=source or moodle_source(worker),
            graders=graders,
            recipients=recipients,
        )
    )
    await handler(
        JobContext(
            1,
            JobKind.RENDER_CHECK,
            {"site_id": str(w.site), "student_userid": sub, "cmid": cm},
            1,
            worker.connect,
            logging.getLogger("t"),
        )
    )


EXTENDED = (
    "active",
    "selected_attempt",
    "pre_session",
    "closed_at",
    "ended_at",
    "transcripts",
)


def core(view: dict) -> dict:  # type: ignore[type-arg]
    """The plan-8 fields of a /state view, without the plan-9 extended contract."""
    return {k: v for k, v in view.items() if k not in EXTENDED}


async def test_not_configured_is_not_enabled_and_queues_nothing(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        r = await state(w, await w.presession(sub=STU1, cmid=4242))
        assert r.status_code == 200 and r.json() == {
            "state": "not_enabled",
            "reason": "not_enabled",
        }
        assert await n(pg, "SELECT count(*) FROM assess_render_checks") == 0


async def test_checking_then_eligible_with_one_deduped_check(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        cm = cmid("rubric_vis")
        await configure_activity(pg, w.site, cm)
        tok = await w.presession(sub=STU1, cmid=cm)
        for _ in range(3):
            r = await state(w, tok)
            assert r.json() == {"state": "unavailable", "reason": "checking"}
        assert (
            await n(
                pg, "SELECT count(*) FROM assess_render_checks WHERE done_at IS NULL"
            )
            == 1
        )
        assert (
            await n(pg, "SELECT count(*) FROM assess_jobs WHERE kind = 'render_check'")
            == 1
        )
        args = (
            await pg.run(
                "super", "SELECT args FROM assess_jobs WHERE kind='render_check'"
            )
        )[0][0]
        assert args == {
            "site_id": str(w.site),
            "student_userid": STU1,
            "cmid": cm,
            "courseid": 7,
        }
        # the jti of a /state token is not consumed
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_token_jti WHERE consumed_at IS NOT NULL",
            )
            == 0
        )
        await render(w, worker, STU1, cm)
        assert (
            await n(
                pg, "SELECT count(*) FROM assess_render_checks WHERE done_at IS NULL"
            )
            == 0
        )
        r = await state(w, tok)
        assert r.json()["state"] == "eligible" and r.json()["reason"] == "ok"


@pytest.mark.parametrize(
    ("assignment", "reason"),
    [
        ("hiddengrade", "not_released"),
        ("team", "team_submission"),
        ("guide", "marking_guide"),
        ("nocutoff", "not_fully_graded"),
    ],
)
async def test_a_negated_gate_changes_the_button_and_refuses_a_crafted_start(
    pg, worker, assignment: str, reason: str
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        cm = cmid(assignment)
        await configure_activity(pg, w.site, cm)
        await render(w, worker, STU1, cm)
        r = await state(w, await w.presession(sub=STU1, cmid=cm))
        assert r.json()["state"] != "eligible" and r.json()["reason"] == reason, (
            r.json()
        )
        start = await w.client.post(
            "/api/v1/assess/sessions",
            headers={
                "Authorization": f"Bearer {await w.presession(sub=STU1, cmid=cm)}"
            },
        )
        assert start.status_code == 409 and err(start)["details"]["reason"] == reason
        assert (
            await n(pg, "SELECT count(*) FROM assess_sessions WHERE cmid = :c", c=cm)
            == 0
        )
        assert (
            await n(pg, "SELECT count(*) FROM assess_jobs WHERE kind = 'preflight'")
            == 0
        )


def _date_forms(epoch: int) -> list[str]:
    d = datetime.fromtimestamp(epoch, tz=UTC)
    return [str(epoch), d.isoformat(), d.strftime("%Y-%m-%d"), d.strftime("%d/%m/%Y")]


async def test_another_students_extension_never_leaks_a_date(pg, worker) -> None:  # type: ignore[no-untyped-def]
    other = 2_100_000_000  # someone else's extension holds the gate
    async with world(pg) as w:
        cm = cmid("rubric_vis")
        await configure_activity(pg, w.site, cm, attested=False)
        held = FakeSource(facts=replace(PASSING_FACTS, participant_closes=(other,)))
        await render(w, worker, STU1, cm, held)
        r = await state(w, await w.presession(sub=STU1, cmid=cm))
        body = r.text
        assert (
            r.json()["state"] == "not_open" and r.json()["reason"] == "close_not_passed"
        )
        assert "opens_at" not in r.json()
        for form in _date_forms(other):
            assert form not in body, form
        # nor does the worker store it where any API route reads
        stored = await pg.run("super", "SELECT inputs::text FROM assess_gate_inputs")
        for form in _date_forms(other):
            assert form not in json.dumps([list(x) for x in stored]), form
        # the student's OWN extension may be shown
        own = FakeSource(
            facts=replace(
                PASSING_FACTS, participant_closes=(other,), own_close_at=other
            )
        )
        await render(w, worker, STU1, cm, own)
        r = await state(w, await w.presession(sub=STU1, cmid=cm))
        assert r.json()["opens_at"] == datetime.fromtimestamp(other, tz=UTC).isoformat()


async def test_an_open_session_is_active_with_its_id(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "ACTIVE")
        r = await state(w, await w.presession(sub=s.sub, cmid=s.cmid))
        assert core(r.json()) == {
            "state": "active",
            "reason": "session_open",
            "session_id": str(s.session_id),
        }


async def test_state_is_rate_limited_per_student_and_activity(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """Review fix 7: the window is keyed by (site, sub, cmid), not by the jti that changes on every
    page view; the global per-key limiter stays dormant."""
    settings = default_settings(VEKTRA_ASSESS_STATE_RPM=3)
    async with world(pg, settings) as w:
        cm = cmid("rubric_vis")
        await configure_activity(pg, w.site, cm)
        codes = [
            (await state(w, await w.presession(sub=STU1, cmid=cm))).status_code
            for _ in range(4)
        ]
        assert codes == [
            200,
            200,
            200,
            429,
        ]  # a fresh token per page view does not reset it
        r = await state(w, await w.presession(sub=STU1, cmid=cm))
        assert err(r)["code"] == "ERR-ASSESS-012" and r.headers["retry-after"]
        # another student, or another activity of the same student, has its own window
        assert (
            await state(w, await w.presession(sub=STU1 + 1, cmid=cm))
        ).status_code == 200
        assert (
            await state(w, await w.presession(sub=STU1, cmid=cm + 1))
        ).status_code == 200
        assert not hasattr(w.app.state, "rate_limiter")


async def test_the_refresh_tick_requeues_stale_pairs_and_deletes_week_old_rows(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        cm = cmid("rubric_vis")
        await configure_activity(pg, w.site, cm)
        await state(w, await w.presession(sub=STU1, cmid=cm))
        await render(w, worker, STU1, cm)
        await pg.run("super", "DELETE FROM assess_jobs")
        assert (await gate_refresh_tick(worker.connect, 300))["queued"] == 0  # fresh
        await pg.run(
            "super",
            "UPDATE assess_render_state SET computed_at = now() - interval '10 minutes'",
        )
        assert (await gate_refresh_tick(worker.connect, 300))["queued"] == 1
        assert (
            await n(pg, "SELECT count(*) FROM assess_jobs WHERE kind = 'gate_refresh'")
            == 1
        )
        assert (await gate_refresh_tick(worker.connect, 300))["queued"] == 0  # deduped
        # a pair nobody asked about for a week: its rows go
        await pg.run(
            "super",
            "INSERT INTO assess_render_state (site_id, student_userid, cmid, status, computed_at) "
            "VALUES (:s, 99, 1, 'eligible', now() - interval '8 days')",
            {"s": w.site},
        )
        await gate_refresh_tick(worker.connect, 300)
        assert (
            await n(
                pg, "SELECT count(*) FROM assess_render_state WHERE student_userid = 99"
            )
            == 0
        )
        assert (
            await n(
                pg,
                "SELECT count(*) FROM assess_render_state WHERE student_userid = :u",
                u=STU1,
            )
            == 1
        )


@pytest.mark.parametrize("render_status", ["eligible", "active"])
async def test_a_closed_session_without_reset_is_closed_whatever_the_render_says(  # type: ignore[no-untyped-def]
    pg, worker, render_status: str
) -> None:
    """Review fix 4: the student's own CLOSED session decides before any render row."""
    async with world(pg) as w:
        s = await seed_session(w, worker, "CLOSED")
        await pg.run(
            "super",
            "INSERT INTO assess_render_state (site_id, student_userid, cmid, status, reason_code) "
            "VALUES (:s, :u, :c, :st, 'ok') ON CONFLICT (site_id, student_userid, cmid) "
            "DO UPDATE SET status = EXCLUDED.status, computed_at = now()",
            {"s": w.site, "u": s.sub, "c": s.cmid, "st": render_status},
        )
        r = await state(w, await w.presession(sub=s.sub, cmid=s.cmid))
        assert core(r.json()) == {"state": "closed", "reason": "already_discussed"}
        # a reset grant reopens the decision to the render state
        await pg.run(
            "super",
            # the complete row the reset route writes (revision 0038; the reason is opaque here)
            "INSERT INTO assess_reset_grants (site_id, cmid, student_userid, granted_by, "
            "session_no, reason_ct, reason_kid) VALUES (:s, :c, :u, '41', 1, '\\x00', 't1')",
            {"s": w.site, "c": s.cmid, "u": s.sub},
        )
        r = await state(w, await w.presession(sub=s.sub, cmid=s.cmid))
        assert r.json()["state"] == render_status


async def test_an_open_session_stays_active_when_the_activity_is_disabled(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4493120939: disabling an activity never hides a started session (BR-006:
    started sessions are not revoked); with no open session it is not_enabled."""
    async with world(pg) as w:
        s = await seed_session(w, worker, "ACTIVE")
        await configure_activity(pg, w.site, s.cmid, enabled=False)
        r = await state(w, await w.presession(sub=s.sub, cmid=s.cmid))
        assert core(r.json()) == {
            "state": "active",
            "reason": "session_open",
            "session_id": str(s.session_id),
        }


# --- plan 9: the extended /state contract agreed with the plugin (P2) ----------------------


async def test_active_carries_session_no_minutes_left_and_deadline(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "ACTIVE", cap_minutes=30)
        view = (await state(w, await w.presession(sub=s.sub, cmid=s.cmid))).json()
        assert view["active"]["session_no"] == 1
        assert 29 <= view["active"]["minutes_left"] <= 30
        assert datetime.fromisoformat(view["active"]["deadline_at"]).tzinfo is not None
        assert not {"pre_session", "closed_at", "ended_at", "selected_attempt"} & set(
            view
        )


async def test_starting_has_no_deadline_yet(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "STARTING")
        view = (await state(w, await w.presession(sub=s.sub, cmid=s.cmid))).json()
        assert view["state"] == "starting" and view["active"]["session_no"] == 1
        assert "deadline_at" not in view["active"]


async def test_closed_carries_closed_at_only(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "CLOSED")
        view = (await state(w, await w.presession(sub=s.sub, cmid=s.cmid))).json()
        assert view["state"] == "closed"
        assert datetime.fromisoformat(view["closed_at"]).tzinfo is not None
        assert not {"active", "pre_session", "ended_at", "selected_attempt"} & set(view)
        # plan 10: one entry per CLOSED session; not generated yet -> not available
        assert view["transcripts"] == [
            {"tid": str(s.session_id), "n": 1, "available": False, "purged": False}
        ]


async def test_transcripts_list_generated_and_shredded_ones(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s = await seed_session(w, worker, "CLOSED")
        await pg.run(
            "worker",
            "INSERT INTO assess_transcripts (id, session_id, ciphertext, dek_wrapped, "
            "transcript_kid, content_hash, turns_digest, filename, byte_size) "
            "VALUES (:t, :t, '\\x00', '\\x00', 'k', 'h', 'd', 'f.html', 1)",
            {"t": s.session_id},
        )
        view = (await state(w, await w.presession(sub=s.sub, cmid=s.cmid))).json()
        assert view["transcripts"] == [
            {"tid": str(s.session_id), "n": 1, "available": True, "purged": False}
        ]
        # the plan-2 shred deletes the row: the entry reads "not available", and purged stays
        # false (review #9: no surviving trace of the shred until plan 12's tombstone)
        from vektra_assess.crypto.shred import shred_transcript

        async with worker.begin() as conn:
            report = await shred_transcript(conn, s.session_id)
        assert report.deleted["assess_transcripts"] == 1
        view = (await state(w, await w.presession(sub=s.sub, cmid=s.cmid))).json()
        assert view["transcripts"] == [
            {"tid": str(s.session_id), "n": 1, "available": False, "purged": False}
        ]


async def test_eligible_names_the_recipients_masked_from_the_teacher_config(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """REQ-082 as amended: the render check reads the course's graders (who may download) and
    the configured recipients (sealed teacher config); /state names the recipients with the
    local part masked and never carries a full address."""
    async with world(pg) as w:
        cm = cmid("rubric_vis")
        await configure_activity(pg, w.site, cm, cap_minutes=30)
        await pg.run(
            "super",
            "UPDATE assess_sites SET release = :r, base_url = :b WHERE id = :s",
            {"r": M["release"], "b": "https://moodle.example.org", "s": w.site},
        )
        src = moodle_source(worker)
        config = await configure_recipients(pg, w.site, cm)
        await render(
            w,
            worker,
            STU1,
            cm,
            FakeSource(facts=PASSING_FACTS),
            graders=src,
            recipients=config,
        )
        inputs = (
            await pg.run(
                "super",
                "SELECT inputs FROM assess_gate_inputs WHERE site_id = :s AND cmid = :c "
                "AND student_userid = :u",
                {"s": w.site, "c": cm, "u": STU1},
            )
        )[0][0]
        graders = [uid("4.3", g) for g in ("grader1", "grader2")]
        assert inputs["graders"] == sorted(graders)
        view = (await state(w, await w.presession(sub=STU1, cmid=cm))).json()
        assert view["pre_session"]["recipients"] == [
            {"role": "recipient", "name": "p***@uni.example"},
            {"role": "recipient", "name": "m***@uni.example"},
        ]
        dumped = json.dumps(view) + json.dumps(inputs)
        for address in TEACHERS:
            assert address not in dumped and address.split("@")[0] not in dumped


async def test_eligible_carries_the_pre_session_data(pg, worker) -> None:  # type: ignore[no-untyped-def]
    settings = default_settings(
        VEKTRA_ASSESS_PRIVACY_NOTICE_URL="https://uni.example/privacy"
    )
    async with world(pg, settings) as w:
        cm = cmid("rubric_vis")
        await configure_activity(pg, w.site, cm, cap_minutes=30)
        await pg.run(
            "super",
            "INSERT INTO assess_extra_time (site_id, cmid, student_userid, extra_seconds) "
            "VALUES (:s, :c, :u, 600)",
            {"s": w.site, "c": cm, "u": STU1},
        )
        await render(w, worker, STU1, cm, FakeSource(facts=PASSING_FACTS))
        view = (await state(w, await w.presession(sub=STU1, cmid=cm))).json()
        assert view["state"] == "eligible", view
        assert view["pre_session"] == {
            "duration_min": 40,
            "course_material_used": False,
            "limits": {"max_message_chars": 2000, "messages_per_min": 4},
            "privacy": {
                "notice_url": "https://uni.example/privacy",
                "notice_version": "1",
            },
            "previous_sessions": 0,
        }  # recipients and files only when the render worker recorded them
        assert not {"active", "closed_at", "ended_at", "selected_attempt"} & set(view)
        # what the render worker records is passed through (data only, filtered)
        await pg.run(
            "super",
            "UPDATE assess_gate_inputs SET inputs = inputs || CAST(:j AS jsonb) "
            "WHERE site_id = :s AND cmid = :c AND student_userid = :u",
            {
                "s": w.site,
                "c": cm,
                "u": STU1,
                "j": json.dumps(
                    {
                        "selected_attempt_no": 2,
                        "recipients": [
                            {"role": "editingteacher", "name": "Prof. Rossi"}
                        ],
                        "files": [
                            {"name": "relazione.pdf", "predicted": "readable"},
                            {"name": "x.bin", "predicted": "bogus"},
                        ],
                    }
                ),
            },
        )
        view = (await state(w, await w.presession(sub=STU1, cmid=cm))).json()
        assert view["selected_attempt"] == {"attempt_no": 2}
        assert view["pre_session"]["recipients"] == [
            {"role": "editingteacher", "name": "Prof. Rossi"}
        ]
        assert view["pre_session"]["files"] == [
            {"name": "relazione.pdf", "predicted": "readable"}
        ]


async def test_ended_carries_ended_at(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        cm = cmid("rubric_vis")
        await configure_activity(
            pg, w.site, cm, cap_minutes=30, end_at_sql="now() - interval '1 day'"
        )
        await render(w, worker, STU1, cm, FakeSource(facts=PASSING_FACTS))
        view = (await state(w, await w.presession(sub=STU1, cmid=cm))).json()
        assert view["state"] == "ended", view
        assert datetime.fromisoformat(view["ended_at"]).tzinfo is not None
        assert not {"active", "pre_session", "closed_at"} & set(view)


@pytest.mark.parametrize(
    ("kind", "cm"),
    [("quiz", int(M["quizzes"]["essay"]["cmid"])), ("assign", cmid("rubric_vis"))],
)
async def test_graders_are_recorded_for_quizzes_and_assignments_alike(
    pg, worker, kind: str, cm: int
) -> None:  # type: ignore[no-untyped-def]
    """Review #1/#10: the grader read uses the activity's real type (a quiz cmid resolves among
    the course's quizzes), also while the activity is disabled, and leaves no scratch entry in
    the source's resolution cache."""
    async with world(pg) as w:
        await configure_activity(pg, w.site, cm, kind=kind, enabled=False)
        await pg.run(
            "super",
            "UPDATE assess_sites SET release = :r, base_url = :b WHERE id = :s",
            {"r": M["release"], "b": "https://moodle.example.org", "s": w.site},
        )
        src = moodle_source(worker)
        await render(w, worker, STU1, cm, src, graders=src)
        inputs = (
            await pg.run(
                "super",
                "SELECT inputs FROM assess_gate_inputs WHERE site_id = :s AND cmid = :c "
                "AND student_userid = :u",
                {"s": w.site, "c": cm, "u": STU1},
            )
        )[0][0]
        assert inputs["graders"] == sorted(
            uid("4.3", g) for g in ("grader1", "grader2")
        )
        assert src.cached == 0
