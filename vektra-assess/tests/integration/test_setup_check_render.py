"""A site that never passed a setup check renders unreadable; the first check makes it render.

Coordinator decision 2026-11-08 (plan assess-moodle-ws Notes): ``assess_sites.release`` is the
release whose capability matrix is valid. A setup-check run whose probes were persisted sets it,
in the matrix's own transaction; the render check (``MoodleStartSource``) reads it. Before this,
no production code wrote ``release``, so a registered site rendered ``unreadable`` forever and
``/state`` never left "checking" for a student.
"""

from __future__ import annotations

import json
import logging

import pytest
from assess_moodle_support import Replay, make_client, manifest, uid
from sqlalchemy.ext.asyncio import create_async_engine
from support.world import configure_activity, world

from integration.test_session_preflight import _reset, moodle_source
from vektra_assess.capabilities import FLAGS
from vektra_assess.jobs import JobKind
from vektra_assess.moodle.probes import derive_probe_manifest
from vektra_assess.worker.render import RenderDeps, make_render_handler
from vektra_assess.worker.runner import JobContext
from vektra_assess.worker.setup_check import make_setup_check_handler

from .test_setup_check_run import SETTINGS, full_registry

pytestmark = pytest.mark.integration

BRANCH = "4.3"  # the branch the render tests' recorded source serves
M = manifest(BRANCH)
STU1 = uid(BRANCH, "stu1")


def _ctx(engine, kind: JobKind, args) -> JobContext:  # type: ignore[no-untyped-def]
    return JobContext(1, kind, args, 1, engine.connect, logging.getLogger("t"))


async def render_status(pg, engine, w, cm: int) -> tuple[str, str | None]:  # type: ignore[no-untyped-def]
    handler = make_render_handler(
        RenderDeps(settings=w.settings, source=moodle_source(engine))
    )
    await handler(
        _ctx(
            engine,
            JobKind.RENDER_CHECK,
            {"site_id": str(w.site), "student_userid": STU1, "cmid": cm},
        )
    )
    row = (
        await pg.run(
            "super",
            "SELECT status, reason_code FROM assess_render_state "
            "WHERE site_id = :s AND student_userid = :u AND cmid = :c",
            {"s": w.site, "u": STU1, "c": cm},
        )
    )[0]
    return str(row[0]), row[1]


async def test_a_never_checked_site_renders_unreadable_until_its_first_setup_check(
    pg,
) -> None:  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    try:
        async with world(pg) as w:
            # registered as an operator would: no release known yet, the probe manifest given
            await pg.run(
                "super",
                "UPDATE assess_sites SET release = NULL, last_seen_release = NULL, "
                "probe_manifest = CAST(:m AS jsonb) WHERE id = :s",
                {"s": w.site, "m": json.dumps(derive_probe_manifest(M))},
            )
            cm = int(M["assigns"]["rubric_vis"]["cmid"])
            await configure_activity(pg, w.site, cm)

            assert await render_status(pg, engine, w, cm) == (
                "unavailable",
                "unreadable",
            )

            check = make_setup_check_handler(
                engine,
                make_client(Replay(BRANCH)),
                SETTINGS,
                registry=full_registry(),
            )
            await check(_ctx(engine, JobKind.SETUP_CHECK, {"site_id": str(w.site)}))

            release, last_seen = (
                await pg.run(
                    "super",
                    "SELECT release, last_seen_release FROM assess_sites WHERE id = :s",
                    {"s": w.site},
                )
            )[0]
            assert release == M["release"] and last_seen == M["release"]
            rows = await pg.run(
                "super",
                "SELECT flag, release FROM assess_site_capabilities WHERE site_id = :s",
                {"s": w.site},
            )
            assert {r[0] for r in rows} == set(FLAGS)
            assert {r[1] for r in rows} == {M["release"]}

            assert await render_status(pg, engine, w, cm) == ("eligible", "ok")
    finally:
        await engine.dispose()


async def test_a_run_without_probes_leaves_the_release_unset(pg) -> None:  # type: ignore[no-untyped-def]
    """A revoked token: site_info does not answer, nothing is probed, ``release`` stays NULL."""
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    try:
        async with world(pg) as w:
            await pg.run(
                "super",
                "UPDATE assess_sites SET release = NULL, last_seen_release = NULL "
                "WHERE id = :s",
                {"s": w.site},
            )
            replay = Replay(BRANCH)
            replay.mutate = lambda _f, _b: {
                "exception": "moodle_exception",
                "errorcode": "invalidtoken",
                "message": "bad",
            }
            check = make_setup_check_handler(
                engine, make_client(replay), SETTINGS, registry=full_registry()
            )
            await check(_ctx(engine, JobKind.SETUP_CHECK, {"site_id": str(w.site)}))
            assert (
                await pg.run(
                    "super",
                    "SELECT release FROM assess_sites WHERE id = :s",
                    {"s": w.site},
                )
            )[0][0] is None
    finally:
        await engine.dispose()


async def test_a_run_without_a_probe_manifest_leaves_the_release_unset(pg) -> None:  # type: ignore[no-untyped-def]
    """Review finding 9: every flag ``not_probed`` is not a validated matrix, so ``release``
    must not move (the readers would pick a model set nothing vouched for)."""
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    try:
        async with world(pg) as w:
            await pg.run(
                "super",
                "UPDATE assess_sites SET release = NULL, last_seen_release = NULL, "
                "probe_manifest = NULL WHERE id = :s",
                {"s": w.site},
            )
            check = make_setup_check_handler(
                engine, make_client(Replay(BRANCH)), SETTINGS, registry=full_registry()
            )
            await check(_ctx(engine, JobKind.SETUP_CHECK, {"site_id": str(w.site)}))
            release, last_seen = (
                await pg.run(
                    "super",
                    "SELECT release, last_seen_release FROM assess_sites WHERE id = :s",
                    {"s": w.site},
                )
            )[0]
            assert last_seen == M["release"]  # site_info answered
            assert release is None
            reasons = await pg.run(
                "super",
                "SELECT DISTINCT reason_code FROM assess_site_capabilities "
                "WHERE site_id = :s",
                {"s": w.site},
            )
            assert [r[0] for r in reasons] == ["not_probed"]
    finally:
        await engine.dispose()
