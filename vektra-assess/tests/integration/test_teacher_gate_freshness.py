"""The teacher gate kept fresh for the plugin (live E2E follow-up, 2026-11-13).

Two symptoms of the cached LMS facts (``assess_teacher_gate``) seen live: a teacher's first click
after a few idle minutes was refused "checking your role", and enabling right after setting the
assignment cut-off in the LMS was refused ``assignment_without_cutoff`` because the cached fact
predated the change. Against the real routes, the restricted API role and real Postgres:

- ``POST /activities/{cmid}/gate-refresh`` queues one refresh per hint, coalesced (a pending
  job, or facts for the same hint read in the last ``REFRESH_MIN_INTERVAL_S``);
- a refusal a refresh may lift (stale facts; an enable refused on an LMS fact not just read)
  queues the refresh and carries ``error.details.retry_after_s`` plus ``Retry-After``; a refusal
  on facts just read, or with any non-LMS error, claims no retry.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from support.recipients import config_pair
from support.world import auth, default_settings, err, make_key, world

from integration.test_teacher_authz import COURSE, EDITOR, seed_gate
from vektra_assess.api import teacher_config as route
from vektra_assess.api.teachers import ANSWER_WINDOW_S, RETRY_AFTER_S
from vektra_assess.errors import RETRY_AFTER_MAX_S

pytestmark = pytest.mark.integration

A = "prof@uni.example"


def config_url(cmid: int) -> str:
    return f"/api/v1/assess/activities/{cmid}/config"


def refresh_url(cmid: int) -> str:
    return f"/api/v1/assess/activities/{cmid}/gate-refresh"


def action(**over: Any) -> dict[str, Any]:
    return {
        "actor_userid": EDITOR,
        "courseid": COURSE,
        "activity_type": "assign",
        **over,
    }


def enable(**over: Any) -> dict[str, Any]:
    return {
        **action(),
        "expected_version": 0,
        "enabled": True,
        "cap_minutes": 30,
        "transcript_recipients": [A],
        **over,
    }


async def gate_jobs(pg, site: uuid.UUID, cmid: int) -> list[Any]:  # type: ignore[no-untyped-def]
    return await pg.run(
        "super",
        "SELECT args, state FROM assess_jobs WHERE kind = 'teacher_gate' "
        "AND args->>'site_id' = :s AND (args->>'cmid')::int = :c ORDER BY id",
        {"s": str(site), "c": cmid},
    )


async def ready(pg, monkeypatch):  # type: ignore[no-untyped-def]
    """A site whose privacy and setup check are green: an enable is refused only for what the
    test seeds in the LMS facts."""
    ctx = world(pg, default_settings(VEKTRA_ASSESS_RECIPIENT_DOMAINS="uni.example"))
    w = await ctx.__aenter__()
    w.app.state.assess_keys[route.PUBLIC_KEY] = config_pair()[0]

    async def configured(_conn: Any, _site: Any) -> bool:
        return True

    monkeypatch.setattr(route.privacy, "is_configured", configured)
    await pg.run(
        "super",
        "INSERT INTO assess_setup_check_reports (site_id, status, items, finished_at) "
        "VALUES (:s, 'done', CAST(:i AS jsonb), now())",
        {
            "s": w.site,
            "i": json.dumps([{"name": "x", "result": "OK", "reason_code": None}]),
        },
    )
    return ctx, w


def test_the_retry_hint_stays_low_and_below_the_answer_window() -> None:
    """The plugin retries server-side (PHP): the wait is a few seconds, and the retries land
    inside the window in which the refusal's own refresh counts as the answer (no loop)."""
    assert 1 <= RETRY_AFTER_S <= RETRY_AFTER_MAX_S <= 5
    assert 2 * RETRY_AFTER_S < ANSWER_WINDOW_S


async def db_epoch(pg) -> float:  # type: ignore[no-untyped-def]
    return float((await pg.run("super", "SELECT extract(epoch FROM now())"))[0][0])


async def answered(pg, site, cmid: int, queued_ago_s: float, **facts: Any) -> None:  # type: ignore[no-untyped-def]
    """The row a refresh queued BY A REFUSAL wrote (the worker stamps ``queued_by`` and the
    job's enqueue time ``queued_at``), read one second ago."""
    now = await db_epoch(pg)
    await seed_gate(
        pg, site, cmid, queued_by="refusal", queued_at=now - queued_ago_s, **facts
    )


async def test_gate_refresh_queues_once_per_hint_and_coalesces(pg, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ctx, w = await ready(pg, monkeypatch)
    try:
        # no facts yet: one job; a second open of the page while it is pending queues the one
        # follow-up (the pending one may be running, second review I); a third queues nothing
        r = await w.client.post(refresh_url(70), json=action(), headers=auth("teacher"))
        assert r.status_code == 202, r.text
        assert r.json() == {"cmid": 70, "queued": True, "refreshed_at": None}
        assert r.headers["cache-control"] == "no-store"
        r = await w.client.post(refresh_url(70), json=action(), headers=auth("teacher"))
        assert r.status_code == 202 and r.json()["queued"] is True
        r = await w.client.post(refresh_url(70), json=action(), headers=auth("teacher"))
        assert r.status_code == 202 and r.json()["queued"] is False
        job, _follow_up = await gate_jobs(pg, w.site, 70)
        args = job.args if isinstance(job.args, dict) else json.loads(job.args)
        assert args == {
            "site_id": str(w.site),
            "cmid": 70,
            "courseid": COURSE,
            "activity_type": "assign",
            "queued_by": "page",
        }

        # facts for this hint read a moment ago: nothing to read again
        await seed_gate(pg, w.site, 71)
        r = await w.client.post(refresh_url(71), json=action(), headers=auth("teacher"))
        assert r.status_code == 202 and r.json()["queued"] is False
        assert r.json()["refreshed_at"] is not None
        assert await gate_jobs(pg, w.site, 71) == []
        # coalesced per cmid (review of #221, finding 5): a resolved row read a moment ago
        # answers whatever hint the body brings
        r = await w.client.post(
            refresh_url(71), json=action(activity_type="quiz"), headers=auth("teacher")
        )
        assert r.json()["queued"] is False
        assert await gate_jobs(pg, w.site, 71) == []
        # old facts are read again
        await seed_gate(pg, w.site, 72, age="5 minutes")
        r = await w.client.post(refresh_url(72), json=action(), headers=auth("teacher"))
        assert r.json()["queued"] is True
    finally:
        await ctx.__aexit__(None, None, None)


async def test_gate_refresh_takes_the_teacher_key_and_a_well_formed_body(
    pg, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    ctx, w = await ready(pg, monkeypatch)
    try:
        for name in ("admin", "mint"):
            r = await w.client.post(refresh_url(73), json=action(), headers=auth(name))
            assert r.status_code == 403 and err(r)["code"] == "ERR-AUTH-003"
        for bad in (
            action(courseid=0),
            action(activity_type="forum"),
            {"courseid": COURSE, "activity_type": "assign"},  # no actor
            action(extra=1),
        ):
            r = await w.client.post(refresh_url(73), json=bad, headers=auth("teacher"))
            assert r.status_code == 400 and err(r)["code"] == "ERR-ASSESS-014", bad
        r = await w.client.post(refresh_url(0), json=action(), headers=auth("teacher"))
        assert r.status_code == 400
        assert await gate_jobs(pg, w.site, 73) == []
        # a teacher key of another site queues for its own site only
        other = await make_key(pg, ["assess:teacher"], w.other_site, "teacher")
        w.app.state.registry.get("key_store", "default").table["other_teacher"] = other
        r = await w.client.post(
            refresh_url(73), json=action(), headers=auth("other_teacher")
        )
        assert r.status_code == 202
        assert await gate_jobs(pg, w.site, 73) == []
        assert len(await gate_jobs(pg, w.other_site, 73)) == 1
    finally:
        await ctx.__aexit__(None, None, None)


async def test_stale_facts_refuse_with_a_retry_hint(pg, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ctx, w = await ready(pg, monkeypatch)
    try:
        await seed_gate(pg, w.site, 74, age="10 minutes")
        r = await w.client.put(config_url(74), json=enable(), headers=auth("teacher"))
        assert r.status_code == 403 and err(r)["details"] == {
            "reason": "editor_check_stale",
            "retry_after_s": RETRY_AFTER_S,
        }
        assert r.headers["retry-after"] == str(RETRY_AFTER_S)
        assert len(await gate_jobs(pg, w.site, 74)) == 1
        # a fresh list that does not name the actor is an answer: no retry
        await seed_gate(pg, w.site, 74)
        r = await w.client.put(
            config_url(74),
            json=enable(actor_userid=EDITOR + 100),
            headers=auth("teacher"),
        )
        assert r.status_code == 403 and err(r)["details"] == {"reason": "not_an_editor"}
        assert "retry-after" not in r.headers
    finally:
        await ctx.__aexit__(None, None, None)


async def test_a_cutoff_refusal_on_facts_not_just_read_asks_for_a_retry(
    pg, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """The live case: the cut-off was set in the LMS a moment ago, the cached row still says 0
    (read a minute ago, within the freshness bound). The refusal queues a refresh and tells the
    plugin to retry; once the refresh landed (seeded here as the worker would write it) the very
    same request passes."""
    ctx, w = await ready(pg, monkeypatch)
    try:
        await seed_gate(pg, w.site, 75, age="1 minute", close_at=0)
        r = await w.client.put(config_url(75), json=enable(), headers=auth("teacher"))
        assert r.status_code == 400, r.text
        e = err(r)
        assert e["code"] == "ERR-ASSESS-022"
        assert e["details"]["fields"] == [
            {"field": "enabled", "code": "assignment_without_cutoff"}
        ]
        assert e["details"]["retry_after_s"] == RETRY_AFTER_S
        assert "retry_after" not in e  # one place only: details.retry_after_s
        assert r.headers["retry-after"] == str(RETRY_AFTER_S)
        (job,) = await gate_jobs(pg, w.site, 75)
        assert job.state == "ready"
        assert (await pg.run("super", "SELECT count(*) FROM assess_activity_config"))[
            0
        ][0] == 0

        await seed_gate(pg, w.site, 75)  # the refresh: cut-off now set, read just now
        r = await w.client.put(config_url(75), json=enable(), headers=auth("teacher"))
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is True
    finally:
        await ctx.__aexit__(None, None, None)


async def test_the_facts_a_refusal_had_read_again_are_final(pg, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Second review, A: the answer to a refusal is the read its own refresh made (queued by a
    refusal, enqueued in the last ANSWER_WINDOW_S, read after that): final, no hint, no job."""
    ctx, w = await ready(pg, monkeypatch)
    try:
        await answered(pg, w.site, 76, queued_ago_s=4, close_at=0)
        r = await w.client.put(config_url(76), json=enable(), headers=auth("teacher"))
        assert r.status_code == 400
        assert "retry_after_s" not in err(r)["details"]
        assert "retry-after" not in r.headers
        assert await gate_jobs(pg, w.site, 76) == []
    finally:
        await ctx.__aexit__(None, None, None)


async def test_facts_read_moments_ago_by_another_refresh_are_not_final(
    pg, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Second review, A: facts read a second ago by the page-open refresh may still predate the
    cut-off the teacher set since; the age of a row says nothing about that. The refusal
    queues its own refresh and hints."""
    ctx, w = await ready(pg, monkeypatch)
    try:
        await seed_gate(pg, w.site, 86, close_at=0)  # read 1 s ago, not for a refusal
        r = await w.client.put(config_url(86), json=enable(), headers=auth("teacher"))
        assert r.status_code == 400
        assert err(r)["details"]["retry_after_s"] == RETRY_AFTER_S
        assert len(await gate_jobs(pg, w.site, 86)) == 1
        # a refusal's own answer from long ago does not answer a new refusal either
        await answered(pg, w.site, 87, queued_ago_s=ANSWER_WINDOW_S + 30, close_at=0)
        r = await w.client.put(config_url(87), json=enable(), headers=auth("teacher"))
        assert err(r)["details"]["retry_after_s"] == RETRY_AFTER_S
    finally:
        await ctx.__aexit__(None, None, None)


async def test_a_refusal_with_a_non_lms_error_refreshes_but_claims_no_retry(
    pg, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """A retry cannot pass while the teacher must fix something else (no recipient): the stale
    fact is still refreshed, so the next save after the fix sees it, but no retry is claimed."""
    ctx, w = await ready(pg, monkeypatch)
    try:
        await seed_gate(pg, w.site, 77, age="1 minute", close_at=0)
        r = await w.client.put(
            config_url(77),
            json=enable(transcript_recipients=None),
            headers=auth("teacher"),
        )
        assert r.status_code == 400
        codes = {f["code"] for f in err(r)["details"]["fields"]}
        assert {"assignment_without_cutoff", "empty"} <= codes
        assert "retry_after_s" not in err(r)["details"]
        assert "retry-after" not in r.headers
        assert len(await gate_jobs(pg, w.site, 77)) == 1
    finally:
        await ctx.__aexit__(None, None, None)


# --- review of #221 ------------------------------------------------------------------------------


async def lease_job(pg, site: uuid.UUID, cmid: int, key: str) -> None:  # type: ignore[no-untyped-def]
    """A teacher_gate job the worker is running now: it read its inputs before this test's
    request (so it does not cover a change made after)."""
    await pg.run(
        "super",
        "INSERT INTO assess_jobs (kind, args, dedupe_key, state, lease_until, leased_by) "
        "VALUES ('teacher_gate', CAST(:a AS jsonb), :k, 'leased', now() + interval '1 minute', "
        "'w1')",
        {
            "a": json.dumps(
                {
                    "site_id": str(site),
                    "cmid": cmid,
                    "courseid": COURSE,
                    "activity_type": "assign",
                }
            ),
            "k": key,
        },
    )


async def test_a_leased_refresh_gets_a_follow_up_before_a_retry_is_promised(
    pg, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Finding 2: the job already running read the LMS before this refusal; it does not cover
    the teacher's change. A follow-up job (ready, reads after the refusal) is queued, and only
    then is a retry promised."""
    from vektra_assess.jobs import teacher_gate_dedupe_key

    ctx, w = await ready(pg, monkeypatch)
    try:
        await seed_gate(pg, w.site, 80, age="1 minute", close_at=0)
        await lease_job(
            pg, w.site, 80, teacher_gate_dedupe_key(w.site, 80, COURSE, "assign")
        )
        r = await w.client.put(config_url(80), json=enable(), headers=auth("teacher"))
        assert r.status_code == 400
        assert err(r)["details"]["retry_after_s"] == RETRY_AFTER_S
        states = sorted(j.state for j in await gate_jobs(pg, w.site, 80))
        assert states == ["leased", "ready"]
    finally:
        await ctx.__aexit__(None, None, None)


async def test_the_retry_hint_is_bounded_without_a_fresh_read(pg, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Finding 4: with the worker not reading (down, slow), the hint is given at most twice
    (one refresh and one follow-up queued); the third refusal is final, no retry_after_s."""
    ctx, w = await ready(pg, monkeypatch)
    try:
        await seed_gate(pg, w.site, 81, age="1 minute", close_at=0)
        hints = []
        for _ in range(3):
            r = await w.client.put(
                config_url(81), json=enable(), headers=auth("teacher")
            )
            assert r.status_code == 400
            hints.append(err(r)["details"].get("retry_after_s"))
            assert ("retry-after" in r.headers) is (hints[-1] is not None)
        assert hints == [RETRY_AFTER_S, RETRY_AFTER_S, None]
        assert len(await gate_jobs(pg, w.site, 81)) == 2

        # the same bound on the stale role check
        await seed_gate(pg, w.site, 82, age="10 minutes")
        hints = []
        for _ in range(3):
            r = await w.client.put(
                config_url(82), json=enable(), headers=auth("teacher")
            )
            assert r.status_code == 403
            assert err(r)["details"]["reason"] == "editor_check_stale"
            hints.append(err(r)["details"].get("retry_after_s"))
        assert hints == [RETRY_AFTER_S, RETRY_AFTER_S, None]
    finally:
        await ctx.__aexit__(None, None, None)


async def test_an_end_date_refused_on_the_cached_opening_asks_for_a_retry(
    pg, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Finding 3 / second review G: ``opening_unknown`` because the cached facts have no close
    date is an LMS fact (hint); ``not_after_opening`` is the teacher's date (final)."""
    ctx, w = await ready(pg, monkeypatch)
    try:
        await seed_gate(
            pg, w.site, 88, age="1 minute"
        )  # a close date: the opening is known
        r = await w.client.put(
            config_url(88),
            json={
                **action(),
                "expected_version": 0,
                "available_until": "2020-01-01T00:00:00Z",
            },
            headers=auth("teacher"),
        )
        assert r.status_code == 400, r.text
        assert [f["code"] for f in err(r)["details"]["fields"]] == ["not_after_opening"]
        assert "retry_after_s" not in err(r)["details"]
        assert await gate_jobs(pg, w.site, 88) == []

        await seed_gate(pg, w.site, 83, age="1 minute", close_at=0)  # no cut-off cached
        r = await w.client.put(
            config_url(83),
            json={
                **action(),
                "expected_version": 0,
                "available_until": "2020-01-01T00:00:00Z",
            },
            headers=auth("teacher"),
        )
        assert r.status_code == 400, r.text
        (field,) = err(r)["details"]["fields"]
        assert (field["field"], field["code"]) == ("available_until", "opening_unknown")
        assert err(r)["details"]["retry_after_s"] == RETRY_AFTER_S
        assert len(await gate_jobs(pg, w.site, 83)) == 1
    finally:
        await ctx.__aexit__(None, None, None)


async def test_gate_refresh_uses_the_resolved_course_not_the_body_hint(
    pg, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Finding 5: a resolved row knows the activity's course; a wrong courseid in the body
    must not queue a read that answers activity_not_in_course."""
    ctx, w = await ready(pg, monkeypatch)
    try:
        await seed_gate(pg, w.site, 84, age="5 minutes")
        r = await w.client.post(
            refresh_url(84),
            json=action(courseid=COURSE + 1000),
            headers=auth("teacher"),
        )
        assert r.status_code == 202 and r.json()["queued"] is True
        (job,) = await gate_jobs(pg, w.site, 84)
        args = job.args if isinstance(job.args, dict) else json.loads(job.args)
        assert (args["courseid"], args["activity_type"]) == (COURSE, "assign")
    finally:
        await ctx.__aexit__(None, None, None)


async def test_a_not_in_course_read_for_another_hint_keeps_the_resolved_row(pg) -> None:  # type: ignore[no-untyped-def]
    """Finding 5, worker side: a read that says the cmid is not in the hinted course replaces
    the row only when it answers the row's own hint; a resolved row of another hint stays."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from support.world import make_site

    from vektra_assess.worker.teacher_gate import refresh_teacher_gate

    class Source:
        def __init__(self, facts: dict[str, Any]) -> None:
            self.facts = facts

        async def teacher_facts(self, *_a: Any, **_k: Any) -> dict[str, Any]:
            return dict(self.facts)

    site = await make_site(pg)
    await seed_gate(pg, site, 85, age="5 minutes")
    engine = create_async_engine(pg.url("worker"))
    not_here = {"resolved": False, "reason": "activity_not_in_course"}
    try:
        await refresh_teacher_gate(
            Source(not_here), engine.connect, site, 85, COURSE + 1, "assign"
        )

        async def row() -> Any:
            return (
                await pg.run(
                    "super",
                    "SELECT inputs FROM assess_teacher_gate WHERE site_id = :s AND cmid = 85",
                    {"s": site},
                )
            )[0][0]

        inputs = await row()
        inputs = inputs if isinstance(inputs, dict) else json.loads(inputs)
        assert inputs["resolved"] is True and inputs["hint"] == [COURSE, "assign"]
        # the row's own hint answering "not in course" does replace it (the activity moved)
        await refresh_teacher_gate(
            Source(not_here), engine.connect, site, 85, COURSE, "assign"
        )
        inputs = await row()
        inputs = inputs if isinstance(inputs, dict) else json.loads(inputs)
        assert inputs["resolved"] is False
    finally:
        await engine.dispose()


# --- second review of #221 ------------------------------------------------------------------------


async def test_gate_refresh_queues_a_follow_up_beside_a_running_refresh(
    pg, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Second review, I: the page-open refresh follows the same rule as a refusal: a refresh
    that is already running may have read before, so one follow-up is queued."""
    from vektra_assess.jobs import teacher_gate_dedupe_key

    ctx, w = await ready(pg, monkeypatch)
    try:
        await seed_gate(pg, w.site, 89, age="5 minutes")
        await lease_job(
            pg, w.site, 89, teacher_gate_dedupe_key(w.site, 89, COURSE, "assign")
        )
        r = await w.client.post(refresh_url(89), json=action(), headers=auth("teacher"))
        assert r.status_code == 202 and r.json()["queued"] is True
        assert sorted(j.state for j in await gate_jobs(pg, w.site, 89)) == [
            "leased",
            "ready",
        ]
    finally:
        await ctx.__aexit__(None, None, None)


async def test_a_refusal_queued_refresh_stamps_its_provenance(pg) -> None:  # type: ignore[no-untyped-def]
    """Second review, A: the worker records which kind of request queued the read and when
    (the job's enqueue time); the read itself is dated when it began, after that."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from support.world import make_site

    from vektra_assess.jobs import JobKind
    from vektra_assess.worker.runner import JobContext
    from vektra_assess.worker.teacher_gate import make_teacher_gate_handler

    class Source:
        async def teacher_facts(self, *_a: Any, **_k: Any) -> dict[str, Any]:
            return {"resolved": True, "editors": [EDITOR]}

    site = await make_site(pg)
    args = {
        "site_id": str(site), "cmid": 90, "courseid": COURSE, "activity_type": "assign",
        "queued_by": "refusal",
    }  # fmt: skip
    ((job_id, queued_at),) = await pg.run(
        "super",
        "INSERT INTO assess_jobs (kind, args, state) VALUES ('teacher_gate', CAST(:a AS jsonb), "
        "'leased') RETURNING id, extract(epoch FROM created_at)",
        {"a": json.dumps(args)},
    )
    engine = create_async_engine(pg.url("worker"))
    try:
        ctx = JobContext(
            job_id=int(job_id), kind=JobKind.TEACHER_GATE, args=args, attempt=1,
            acquire=engine.connect, log=__import__("logging").getLogger("t"),
        )  # fmt: skip
        await make_teacher_gate_handler(Source())(ctx)
    finally:
        await engine.dispose()
    ((inputs, read_at),) = await pg.run(
        "super",
        "SELECT inputs, extract(epoch FROM refreshed_at) FROM assess_teacher_gate "
        "WHERE site_id = :s AND cmid = 90",
        {"s": site},
    )
    inputs = inputs if isinstance(inputs, dict) else json.loads(inputs)
    assert inputs["queued_by"] == "refusal"
    assert inputs["queued_at"] == pytest.approx(float(queued_at), abs=1e-3)
    assert float(read_at) >= float(queued_at)


async def test_an_older_read_never_overwrites_a_newer_one(pg) -> None:  # type: ignore[no-untyped-def]
    """Second review, B: two refreshes of one activity can finish out of order; the row keeps
    the one whose read began last."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from support.world import make_site

    from vektra_assess.worker.teacher_gate import refresh_teacher_gate

    site = await make_site(pg)

    class Overtaken:
        async def teacher_facts(self, *_a: Any, **_k: Any) -> dict[str, Any]:
            # a newer read lands while this one is still reading
            await seed_gate(pg, site, 91, age="0 seconds", editors=[EDITOR, 99])
            return {"resolved": True, "editors": [EDITOR], "hint": [COURSE, "assign"]}

    engine = create_async_engine(pg.url("worker"))
    try:
        await refresh_teacher_gate(
            Overtaken(), engine.connect, site, 91, COURSE, "assign"
        )
    finally:
        await engine.dispose()
    ((inputs,),) = await pg.run(
        "super",
        "SELECT inputs FROM assess_teacher_gate WHERE site_id = :s AND cmid = 91",
        {"s": site},
    )
    inputs = inputs if isinstance(inputs, dict) else json.loads(inputs)
    assert inputs["editors"] == [EDITOR, 99]
