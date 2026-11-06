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


async def render(w, worker, sub: int, cm: int, source=None) -> None:  # type: ignore[no-untyped-def]
    handler = make_render_handler(
        RenderDeps(settings=w.settings, source=source or moodle_source(worker))
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


async def test_a_negated_gate_changes_the_button_and_refuses_a_crafted_start(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        cm = cmid("hiddengrade")
        await configure_activity(pg, w.site, cm)
        await render(w, worker, STU1, cm)
        r = await state(w, await w.presession(sub=STU1, cmid=cm))
        assert (
            r.json()["state"] == "not_graded" and r.json()["reason"] == "not_released"
        )
        start = await w.client.post(
            "/api/v1/assess/sessions",
            headers={
                "Authorization": f"Bearer {await w.presession(sub=STU1, cmid=cm)}"
            },
        )
        assert (
            start.status_code == 409
            and err(start)["details"]["reason"] == "not_released"
        )
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
        assert r.json() == {
            "state": "active",
            "reason": "session_open",
            "session_id": str(s.session_id),
        }


async def test_state_is_rate_limited_per_token(pg, worker) -> None:  # type: ignore[no-untyped-def]
    settings = default_settings(VEKTRA_ASSESS_STATE_RPM=3)
    async with world(pg, settings) as w:
        cm = cmid("rubric_vis")
        await configure_activity(pg, w.site, cm)
        tok = await w.presession(sub=STU1, cmid=cm)
        codes = [(await state(w, tok)).status_code for _ in range(4)]
        assert codes == [200, 200, 200, 429]
        r = await state(w, tok)
        assert err(r)["code"] == "ERR-ASSESS-012" and r.headers["retry-after"]
        # another token of the same student has its own window
        assert (
            await state(w, await w.presession(sub=STU1, cmid=cm))
        ).status_code == 200


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
