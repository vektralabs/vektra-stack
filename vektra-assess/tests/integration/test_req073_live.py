"""M5.10 / REQ-073 end to end on the LIVE dev Moodle sites (4.3 and 5.2, ``-m moodle``).

A full session (Start, pre-flight against the real site, a turn, Fine) must leave the student's
Moodle grade, rubric and feedback byte-identical: the plan-4 capture is taken before and after and
its canonical bytes compared by hash. The service account must hold no grade-write function (the
plan-4 setup check, ``no_grade_write``). The adversarial verdict counts are not re-measured here:
they come from the plan-7 eval report (``tests/eval/eval_assess.py``), which the release checks.

SKIPPED with ``BLOCKED: ...`` unless the dev env variables of ``test_moodle_live`` are set (the
integration job lists this module among the live ones that may skip).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

import pytest
from assess_moodle_support import BRANCHES, Replay, make_client, use_live
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import (
    FakePartB,
    FakeStreamRunner,
    activate,
    deps,
    job_context,
    kek_ring,
)
from support.sessions import seed_session
from support.world import world
from test_context_capture import InProcessParser

from integration.test_session_preflight import _reset
from vektra_assess.context.snapshot import canonical_bytes
from vektra_assess.worker.preflight import StartTarget, make_preflight_handler
from vektra_assess.worker.start_source import MoodleStartSource

from .test_moodle_live import _load
from .test_moodle_setup_live import _prov, run_check

pytestmark = [
    pytest.mark.integration,
    pytest.mark.moodle,
    pytest.mark.parametrize("branch", BRANCHES),
]

_TARGET = text(
    "SELECT s.id, s.site_id, s.cmid, s.student_userid, s.courseid_hint, s.session_no, "
    "c.activity_type, c.flags FROM assess_sessions s JOIN assess_activity_config c "
    "ON c.site_id = s.site_id AND c.cmid = s.cmid WHERE s.id = :s"
)


async def moodle_dump_hash(worker, branch: str, sid) -> str:  # type: ignore[no-untyped-def]
    async with worker.connect() as conn:
        r = (await conn.execute(_TARGET, {"s": sid})).one()
    target = StartTarget(
        session_id=r.id,
        site_id=r.site_id,
        cmid=int(r.cmid),
        student_userid=int(r.student_userid),
        courseid_hint=r.courseid_hint,
        activity_type=str(r.activity_type),
        session_no=int(r.session_no),
        flags=dict(r.flags or {}),
    )
    source = MoodleStartSource(
        make_client(Replay(branch)), InProcessParser(), worker.connect
    )
    built = await source.capture(target)
    snapshot = getattr(built, "snapshot", None)
    assert snapshot is not None, f"capture failed: {built!r}"
    # the read times change between two captures even when Moodle does not: they are
    # normalised for THIS comparison only (production serialization untouched)
    stable = snapshot.model_copy(
        update={
            "header": snapshot.header.model_copy(update={"captured_at": 0}),
            "overall": snapshot.overall.model_copy(update={"grade_read_at": 0}),
        }
    )
    return hashlib.sha256(canonical_bytes(stable)).hexdigest()


@pytest.fixture(autouse=True)
def _no_live_leak(branch: str) -> Iterator[None]:
    """``run_check`` switches the branch to live and leaves switching back to its own module's
    fixture; without this, every later test in the run reads the live manifest."""
    yield
    use_live(branch, None)


async def test_a_full_session_leaves_moodle_byte_identical(pg, branch: str) -> None:  # type: ignore[no-untyped-def]
    env = _load(branch)  # skips (BLOCKED) when the dev env is not configured
    use_live(branch, env)
    await _reset(pg)
    worker = create_async_engine(pg.url("worker"))
    try:
        async with world(pg, assess_runner=FakeStreamRunner()) as w:
            # the baseline is read BEFORE Start's pre-flight touches anything: the session
            # is requested (STARTING), Moodle is read, then the pre-flight runs
            s = await seed_session(w, worker, "STARTING", branch=branch)
            before = await moodle_dump_hash(worker, branch, s.session_id)
            await activate(pg)
            source = MoodleStartSource(
                make_client(Replay(branch)), InProcessParser(), worker.connect
            )
            handler = make_preflight_handler(
                deps(w.settings, w.handoff_public, source, FakePartB(), kek=kek_ring())
            )
            await handler(job_context(worker, s.session_id))
            url = f"/api/v1/assess/sessions/{s.session_id}"
            auth = {"Authorization": f"Bearer {s.session_token}"}
            r = await w.client.post(
                f"{url}/turns", json={"text": "Perché ho perso punti?"}, headers=auth
            )
            assert r.status_code == 202
            driver = w.app.state.assess_turn_driver
            for task in list(driver.tasks.values()):
                await task
            assert (
                await w.client.post(f"{url}/finish", headers=auth)
            ).status_code == 200
            after = await moodle_dump_hash(worker, branch, s.session_id)
            assert after == before, "Moodle grade, rubric or feedback changed"
    finally:
        await worker.dispose()
        use_live(branch, None)


async def test_the_service_account_cannot_write_grades(branch: str) -> None:
    _load(branch)  # skips (BLOCKED) when the dev env is not configured
    _out, items = await run_check(branch, _prov(branch)["service_account"]["token"])
    assert items["no_grade_write"][0] == "OK", items["no_grade_write"]
