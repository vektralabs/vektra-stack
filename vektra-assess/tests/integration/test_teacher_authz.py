"""The fresh editor check of the teacher endpoints (plan assess-teacher-ops 11a, INT-001).

Run as the real API role against worker-written ``assess_teacher_gate`` rows: absent, stale or
unreadable facts refuse and queue exactly one ``teacher_gate`` job per hint; a fresh list that
does not name the actor refuses and queues nothing.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from support.world import make_site

from vektra_assess.api.teachers import TeacherCheck, check_fresh_teacher

pytestmark = pytest.mark.integration

EDITOR, OTHER, COURSE = 31, 32, 7


async def seed_gate(
    pg, site: uuid.UUID, cmid: int, age: str = "1 second", **facts
) -> None:  # type: ignore[no-untyped-def]
    inputs = {
        "resolved": True,
        "courseid": COURSE,
        "activity_type": "assign",
        "editors": [EDITOR],
        "close_at": 1_900_000_000,
        "team_submission": False,
        "grading_method": "rubric",
        "lms_rubric": True,
        "hint": [COURSE, "assign"],
        **facts,
    }
    await pg.run(
        "worker",
        "INSERT INTO assess_teacher_gate (site_id, cmid, inputs, refreshed_at) VALUES "
        f"(:s, :c, CAST(:i AS jsonb), now() - interval '{age}') ON CONFLICT (site_id, cmid) "
        "DO UPDATE SET inputs = EXCLUDED.inputs, refreshed_at = EXCLUDED.refreshed_at",
        {"s": site, "c": cmid, "i": json.dumps(inputs)},
    )


async def jobs(pg, site: uuid.UUID) -> list:  # type: ignore[no-untyped-def]
    return await pg.run(
        "super",
        "SELECT args, dedupe_key FROM assess_jobs WHERE kind = 'teacher_gate' "
        "AND args->>'site_id' = :s ORDER BY id",
        {"s": str(site)},
    )


async def check(pg, site, cmid, actor=EDITOR, hint=(COURSE, "assign")):  # type: ignore[no-untyped-def]
    engine = create_async_engine(pg.url("api"))
    try:
        async with engine.begin() as conn:
            # bound to the site, as the teacher routes are (RLS, ADR-0033 D9.2)
            await conn.execute(
                text("SELECT set_config('app.assess_site_id', :s, true)"),
                {"s": str(site)},
            )
            return await check_fresh_teacher(
                conn,
                site=site,
                cmid=cmid,
                actor_userid=actor,
                max_age_s=120,
                courseid_hint=hint[0],
                kind_hint=hint[1],
                role="editor",
            )
    finally:
        await engine.dispose()


async def test_absent_facts_refuse_and_queue_one_refresh(pg) -> None:  # type: ignore[no-untyped-def]
    site = await make_site(pg)
    # the refresh, then its one follow-up (a pending job may already have read: review of
    # #221), then nothing more while both are pending
    checked = [await check(pg, site, 50) for _ in range(3)]
    assert {c.check for c in checked} == {TeacherCheck.STALE}
    assert [c.refresh_queued for c in checked] == [True, True, False]
    rows = await jobs(pg, site)
    assert [r.dedupe_key.endswith(":next") for r in rows] == [False, True]
    for job in rows:
        assert job.args == {
            "site_id": str(site),
            "cmid": 50,
            "courseid": COURSE,
            "activity_type": "assign",
        }


async def test_fresh_editor_passes_and_others_are_refused_without_a_refresh(pg) -> None:  # type: ignore[no-untyped-def]
    site = await make_site(pg)
    await seed_gate(pg, site, 51)
    ok = await check(pg, site, 51)
    assert ok.ok and ok.facts["lms_rubric"] is True
    # a user who is not listed (a grader, say) is not an editor (REQ-077 "activity editors only")
    assert (await check(pg, site, 51, actor=OTHER)).check is TeacherCheck.NOT_AN_EDITOR
    assert await jobs(pg, site) == []


async def test_stale_or_unreadable_facts_refuse_and_queue(pg) -> None:  # type: ignore[no-untyped-def]
    site = await make_site(pg)
    await seed_gate(pg, site, 52, age="121 seconds")
    assert (await check(pg, site, 52)).check is TeacherCheck.STALE
    await seed_gate(pg, site, 53, editors=None)
    assert (await check(pg, site, 53)).check is TeacherCheck.STALE
    assert len(await jobs(pg, site)) == 2


async def test_an_unresolved_hint_refuses_and_another_hint_asks_again(pg) -> None:  # type: ignore[no-untyped-def]
    site = await make_site(pg)
    await seed_gate(
        pg,
        site,
        54,
        resolved=False,
        reason="activity_not_in_course",
        editors=None,
        hint=[999, "assign"],
        courseid=None,
    )
    # the same wrong hint again: refused, nothing to refresh
    got = await check(pg, site, 54, hint=(999, "assign"))
    assert got.check is TeacherCheck.NOT_AN_EDITOR
    assert await jobs(pg, site) == []
    # the right hint: refused for now, and a refresh for THAT hint is queued
    assert (await check(pg, site, 54)).check is TeacherCheck.STALE
    (job,) = await jobs(pg, site)
    assert job.args["courseid"] == COURSE
    # no hint at all: nothing can be resolved, nothing is queued, and the caller is NOT told a
    # check was queued (review 5)
    assert (
        await check(pg, site, 55, hint=(None, None))
    ).check is TeacherCheck.HINT_MISSING
    assert (
        await check(pg, site, 55, hint=(COURSE, "forum"))
    ).check is TeacherCheck.HINT_MISSING
    assert len(await jobs(pg, site)) == 1


@pytest.mark.parametrize(
    "stored_hint",
    [[COURSE, "quiz"], None],
    ids=["same_course_other_kind", "no_stored_hint"],
)
async def test_an_unresolved_row_for_another_or_no_hint_asks_for_this_one(  # type: ignore[no-untyped-def]
    pg, stored_hint
) -> None:
    """CodeRabbit round 4 nitpick: the stored hint must match on course AND kind. A definitive
    unresolved answer for the same course as a quiz, or one with no stored hint, says nothing
    about this (course, assign) request: refresh, and the job carries the requested pair."""
    site = await make_site(pg)
    await seed_gate(
        pg,
        site,
        58,
        resolved=False,
        reason="activity_not_in_course",
        editors=None,
        hint=stored_hint,
        courseid=None,
    )
    assert (
        await check(pg, site, 58, hint=(COURSE, "assign"))
    ).check is TeacherCheck.STALE
    (job,) = await jobs(pg, site)
    assert (job.args["courseid"], job.args["activity_type"], job.args["cmid"]) == (
        COURSE,
        "assign",
        58,
    )


@pytest.mark.parametrize("reason", ["site_release_unknown", "error", "http_5xx", None])
async def test_a_transient_unresolved_row_is_stale_and_asks_again(pg, reason) -> None:  # type: ignore[no-untyped-def]
    """Review 3: only "the cmid is not in that course" is definitive; a site, reader or Moodle
    failure recorded as unresolved is retried, never reported as "not an editor"."""
    site = await make_site(pg)
    await seed_gate(
        pg, site, 56, resolved=False, reason=reason, editors=None, courseid=None
    )
    assert (await check(pg, site, 56)).check is TeacherCheck.STALE
    assert len(await jobs(pg, site)) == 1


@pytest.mark.parametrize("existing", [False, True])
async def test_the_facts_are_dated_when_they_were_read_not_when_stored(
    pg, existing
) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4505511259 / 4505705706: freshness counts from before the Moodle reads; a slow
    read stored at the end would look fresher than the list it carries. Both the insert and the
    ON CONFLICT update (review 2 nitpick) store the clock read before the reads began."""
    import asyncio

    from vektra_assess.worker.teacher_gate import refresh_teacher_gate

    began: list[Any] = []

    class Slow:
        async def teacher_facts(self, *_a, **_k):  # type: ignore[no-untyped-def]
            began.append((await pg.run("super", "SELECT clock_timestamp()"))[0][0])
            await asyncio.sleep(2.0)
            return {"resolved": True, "editors": [EDITOR]}

    site = await make_site(pg)
    if existing:
        await seed_gate(pg, site, 57, age="1 day")
    t0 = (await pg.run("super", "SELECT clock_timestamp()"))[0][0]
    engine = create_async_engine(pg.url("worker"))
    try:
        await refresh_teacher_gate(Slow(), engine.connect, site, 57, COURSE, "assign")
    finally:
        await engine.dispose()
    (stamp,) = (
        await pg.run(
            "super",
            "SELECT refreshed_at FROM assess_teacher_gate WHERE site_id = :s AND cmid = 57",
            {"s": site},
        )
    )[0]
    # within database-clock bounds (CodeRabbit 4505893722: no wall-time limit): not before the
    # refresh was called (so the old stamp of an existing row was replaced), not after the
    # Moodle read began (so the 2 s read is not counted as fresh)
    assert t0 <= stamp <= began[0], (t0, stamp, began[0])
