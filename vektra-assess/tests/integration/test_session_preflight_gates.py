"""M2.2: the pre-flight evaluates the START gates on fresh reads and rolls back with the gate code.

Crafted Starts (through the real API) on the fake LMS provider's scenario world: each failing gate
ends in a rollback whose reason is the gate's code, with 0 session, snapshot, key, turn and
hand-off rows left, and the capture-only reads (attempt review, rubric fillings, files) never made. A team submission is also audited ``assess_team_submission_refused``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.fake_lms import ASSIGNS, STUDENTS, scenario_provider
from support.preflight import FakePartB, activate, deps, job_context
from support.world import configure_activity, default_settings, world

from integration.test_session_preflight import (
    _counts,
    _reset,
    _rolled_back,
    _start,
    start_source,
)
from vektra_assess.worker.preflight import make_preflight_handler

pytestmark = pytest.mark.integration

STU1 = STUDENTS["stu1"]


def cmid(name: str) -> int:
    return ASSIGNS[name].cmid


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    await activate(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


#: what only the capture reads: a gate refusal must never have reached them
CAPTURE_ONLY = ("attempt_review", "rubric_fillings", "submission_file")


async def _run(w, worker, sid, settings=None):  # type: ignore[no-untyped-def]
    fake = scenario_provider()
    handler = make_preflight_handler(
        deps(
            settings or w.settings,
            w.handoff_public,
            start_source(worker, fake),
            FakePartB(),
        )
    )
    await handler(job_context(worker, sid))
    return fake


@pytest.mark.parametrize(
    ("assignment", "reason"),
    [
        ("team", "team_submission"),
        ("guide", "marking_guide"),
        # the no-cut-off assignment is also ungraded: BR-014 comes first
        ("nocutoff", "not_fully_graded"),
        ("hiddengrade", "not_released"),
    ],
)
async def test_a_failing_moodle_gate_rolls_back_with_its_code(  # type: ignore[no-untyped-def]
    pg, worker, assignment: str, reason: str
) -> None:
    async with world(pg) as w:
        await configure_activity(pg, w.site, cmid(assignment))
        sid = await _start(w, sub=STU1, cmid=cmid(assignment))
        fake = await _run(w, worker, sid)
        await _rolled_back(pg, sid, reason)
        fake.assert_never_read(*CAPTURE_ONLY)
        team_audits = (
            await pg.run(
                "super",
                "SELECT count(*) FROM assess_audit_events WHERE session_id = :s "
                "AND event = 'assess_team_submission_refused'",
                {"s": sid},
            )
        )[0][0]
        assert team_audits == (1 if assignment == "team" else 0)


async def test_req_091_fallback_without_a_teacher_date_is_not_available(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        await configure_activity(pg, w.site, cmid("rubric_vis"), attested=False)
        sid = await _start(w, sub=STU1, cmid=cmid("rubric_vis"))
        await _run(w, worker, sid)
        await _rolled_back(pg, sid, "close_unreadable")


async def test_a_teacher_date_in_the_future_keeps_the_gate_closed(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        await configure_activity(pg, w.site, cmid("rubric_vis"))
        await pg.run(
            "super",
            "UPDATE assess_activity_config SET attested_opening_at = now() + interval '1 day' "
            "WHERE site_id = :s AND cmid = :c",
            {"s": w.site, "c": cmid("rubric_vis")},
        )
        sid = await _start(w, sub=STU1, cmid=cmid("rubric_vis"))
        await _run(w, worker, sid)
        await _rolled_back(pg, sid, "close_not_passed")


async def test_end_date_passed(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        await configure_activity(
            pg, w.site, cmid("rubric_vis"), end_at_sql="now() - interval '1 minute'"
        )
        sid = await _start(w, sub=STU1, cmid=cmid("rubric_vis"))
        await _run(w, worker, sid)
        await _rolled_back(pg, sid, "end_date_passed")


async def test_privacy_fails_closed_without_the_dev_override(pg, worker) -> None:  # type: ignore[no-untyped-def]
    settings = default_settings(VEKTRA_ASSESS_PRIVACY_ALLOW_UNSET=False)
    async with world(pg, settings) as w:
        await configure_activity(pg, w.site, cmid("rubric_vis"))
        sid = await _start(w, sub=STU1, cmid=cmid("rubric_vis"))
        await _run(w, worker, sid, settings)
        await _rolled_back(pg, sid, "privacy_not_configured")
    # the override is a development-only switch
    prod = default_settings(
        VEKTRA_ENV="production", VEKTRA_ASSESS_PRIVACY_ALLOW_UNSET=True
    )
    async with world(pg, prod) as w:
        await configure_activity(pg, w.site, cmid("rubric_vis"))
        sid = await _start(w, sub=STU1, cmid=cmid("rubric_vis"))
        await _run(w, worker, sid, prod)
        await _rolled_back(pg, sid, "privacy_not_configured")


async def test_no_cutoff_is_refused_even_with_a_teacher_date(pg, worker) -> None:  # type: ignore[no-untyped-def]
    from dataclasses import replace

    from support.preflight import PASSING_FACTS, FakeSource

    async with world(pg) as w:
        await configure_activity(
            pg, w.site, cmid("rubric_vis")
        )  # attested opening date set
        sid = await _start(w, sub=STU1, cmid=cmid("rubric_vis"))
        source = FakeSource(
            facts=replace(PASSING_FACTS, activity_close_at=0, participant_closes=None)
        )
        handler = make_preflight_handler(deps(w.settings, w.handoff_public, source))
        await handler(job_context(worker, sid))
        await _rolled_back(pg, sid, "no_close_date")
        assert source.targets == []  # nothing captured: the gate refused first


async def test_a_student_not_in_the_course_is_not_enrolled(pg, worker) -> None:  # type: ignore[no-untyped-def]
    # the LMS lists no course for the student: the activity cannot be re-resolved inside the
    # student's own courses, whatever course the plugin hinted (REQ-095)
    fake = scenario_provider()
    fake.world.courses_of[STU1] = set()
    source = start_source(worker, fake)
    async with world(pg) as w:
        await configure_activity(pg, w.site, cmid("rubric_vis"))
        sid = await _start(w, sub=STU1, cmid=cmid("rubric_vis"))
        handler = make_preflight_handler(
            deps(w.settings, w.handoff_public, source, FakePartB())
        )
        await handler(job_context(worker, sid))
        assert (await _counts(pg, sid))["assess_sessions"] == 0
        reason = (
            await pg.run(
                "super",
                "SELECT reason_code FROM assess_start_attempts WHERE session_id = :s "
                "AND outcome = 'rolled_back'",
                {"s": sid},
            )
        )[0][0]
        assert reason == "not_enrolled"
        fake.assert_never_read("enrolment", *CAPTURE_ONLY)


async def test_every_gate_passing_commits(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        await configure_activity(pg, w.site, cmid("simple"))
        sid = await _start(w, sub=STU1, cmid=cmid("simple"))
        await _run(w, worker, sid)
        assert (await _counts(pg, sid))["assess_turns"] == 2
