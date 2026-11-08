"""The fresh editor check of the teacher endpoints (plan assess-teacher-ops 11a, INT-001).

Run as the real API role against worker-written ``assess_teacher_gate`` rows: absent, stale or
unreadable facts refuse and queue exactly one ``teacher_gate`` job per hint; a fresh list that
does not name the actor refuses and queues nothing.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.world import make_site

from vektra_assess.api.teachers import TeacherCheck, check_fresh_editor

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
        "graders": [EDITOR, OTHER],
        "close_at": 1_900_000_000,
        "team_submission": False,
        "grading_method": "rubric",
        "moodle_rubric": True,
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
            return await check_fresh_editor(
                conn,
                site=site,
                cmid=cmid,
                actor_userid=actor,
                max_age_s=120,
                courseid_hint=hint[0],
                kind_hint=hint[1],
            )
    finally:
        await engine.dispose()


async def test_absent_facts_refuse_and_queue_one_refresh(pg) -> None:  # type: ignore[no-untyped-def]
    site = await make_site(pg)
    assert (await check(pg, site, 50)).check is TeacherCheck.STALE
    assert (
        await check(pg, site, 50)
    ).check is TeacherCheck.STALE  # deduped while pending
    (job,) = await jobs(pg, site)
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
    assert ok.ok and ok.facts["moodle_rubric"] is True
    # a grader who is not an editor is still not an editor (REQ-077 "activity editors only")
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
        pg, site, 54, resolved=False, editors=None, hint=[999, "assign"], courseid=None
    )
    # the same wrong hint again: refused, nothing to refresh
    got = await check(pg, site, 54, hint=(999, "assign"))
    assert got.check is TeacherCheck.NOT_AN_EDITOR
    assert await jobs(pg, site) == []
    # the right hint: refused for now, and a refresh for THAT hint is queued
    assert (await check(pg, site, 54)).check is TeacherCheck.STALE
    (job,) = await jobs(pg, site)
    assert job.args["courseid"] == COURSE
    # no hint at all: nothing can be resolved, nothing is queued
    assert (await check(pg, site, 55, hint=(None, None))).check is TeacherCheck.STALE
    assert len(await jobs(pg, site)) == 1
