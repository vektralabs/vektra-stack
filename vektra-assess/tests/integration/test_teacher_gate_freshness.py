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
from vektra_assess.api.teachers import FACT_FRESH_S, RETRY_AFTER_S
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
    """The plugin retries server-side (PHP): the wait is a few seconds, and the retry lands
    inside the window in which the refreshed facts count as an answer (no retry loop)."""
    assert 1 <= RETRY_AFTER_S <= RETRY_AFTER_MAX_S <= 5
    assert RETRY_AFTER_S < FACT_FRESH_S


async def test_gate_refresh_queues_once_per_hint_and_coalesces(pg, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ctx, w = await ready(pg, monkeypatch)
    try:
        # no facts yet: one job; a second open of the page while it is pending queues nothing
        r = await w.client.post(refresh_url(70), json=action(), headers=auth("teacher"))
        assert r.status_code == 202, r.text
        assert r.json() == {"cmid": 70, "queued": True, "refreshed_at": None}
        assert r.headers["cache-control"] == "no-store"
        r = await w.client.post(refresh_url(70), json=action(), headers=auth("teacher"))
        assert r.status_code == 202 and r.json()["queued"] is False
        (job,) = await gate_jobs(pg, w.site, 70)
        args = job.args if isinstance(job.args, dict) else json.loads(job.args)
        assert args == {
            "site_id": str(w.site),
            "cmid": 70,
            "courseid": COURSE,
            "activity_type": "assign",
        }

        # facts for this hint read a moment ago: nothing to read again
        await seed_gate(pg, w.site, 71)
        r = await w.client.post(refresh_url(71), json=action(), headers=auth("teacher"))
        assert r.status_code == 202 and r.json()["queued"] is False
        assert r.json()["refreshed_at"] is not None
        assert await gate_jobs(pg, w.site, 71) == []
        # ...but another hint is not answered by them, and old facts are read again
        r = await w.client.post(
            refresh_url(71), json=action(activity_type="quiz"), headers=auth("teacher")
        )
        assert r.json()["queued"] is True
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


async def test_a_refusal_on_facts_just_read_is_final(pg, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ctx, w = await ready(pg, monkeypatch)
    try:
        await seed_gate(pg, w.site, 76, close_at=0)  # read 1 s ago: an answer
        r = await w.client.put(config_url(76), json=enable(), headers=auth("teacher"))
        assert r.status_code == 400
        assert "retry_after_s" not in err(r)["details"]
        assert "retry-after" not in r.headers
        assert await gate_jobs(pg, w.site, 76) == []
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
