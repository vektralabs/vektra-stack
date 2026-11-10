"""A setup-check run against a real Postgres, as the WORKER role (plan group E).

The Moodle side is the recorded 4.3 / 5.2 responses (no network); the database is real, with
the real grants: the run may only do what the worker is allowed to do.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from assess_moodle_support import BASE_URL, FIXTURES, Replay, make_client, manifest, now
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from structlog.testing import capture_logs

from vektra_assess.capabilities import FLAGS, NotReady, Ready, site_readiness
from vektra_assess.config import VektraAssessSettings
from vektra_assess.moodle import setup_check as sc
from vektra_assess.moodle.probes import (
    FlagResult,
    derive_probe_manifest,
    persist_matrix,
)

pytestmark = pytest.mark.integration

BRANCH = "5.2"
ENV = {
    "VEKTRA_ASSESS_ENABLED": "true",
    "VEKTRA_ASSESS_INGEST_ACCOUNT_USERNAME": "vektra_ingest_acct",
    "VEKTRA_ASSESS_UI_ORIGIN": "https://chat.vektra-ui.test",
    "VEKTRA_ASSESS_API_PUBLIC_ORIGIN": "https://api.vektra-api.test",
}
SETTINGS = VektraAssessSettings(**ENV)  # type: ignore[arg-type]
SITE_INFO = "core_webservice_get_site_info"


def full_registry() -> sc.ItemRegistry:
    """Built-ins plus the three pluggable items, as the plans that own them will register."""
    reg = sc.builtin_registry()

    async def fine(_ctx: Any) -> sc.Outcome:
        return sc.ok()

    for name in sc.PLUGGABLE_ITEMS:
        reg.register(name, fine)
    return reg


@asynccontextmanager
async def worker_engine(pg) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg.url("worker"))
    try:
        yield engine
    finally:
        await engine.dispose()


async def make_site(pg, *, release: str | None = None) -> uuid.UUID:
    site = uuid.uuid4()
    await pg.run(
        "super",
        "INSERT INTO assess_sites (id, moodle_site_uuid, base_url, last_seen_release, "
        "probe_manifest) VALUES (:i, :u, :b, :r, CAST(:p AS jsonb))",
        {
            "i": site,
            "u": str(site),
            "b": BASE_URL,
            "r": release,
            "p": json.dumps(derive_probe_manifest(manifest(BRANCH))),
        },
    )
    return site


def serve_files(replay: Replay) -> None:
    from urllib.parse import urlsplit

    doc = json.loads(
        (
            FIXTURES / BRANCH / "mod_assign_get_submission_status__files-stu1.json"
        ).read_text()
    )["response"]
    for plugin in doc["lastattempt"]["submission"]["plugins"]:
        for area in plugin.get("fileareas", []):
            for f in area["files"]:
                local = FIXTURES / BRANCH / "files" / f"stu1__{f['filename']}"
                if local.exists():
                    replay.files[urlsplit(f["fileurl"]).path] = local.read_bytes()


async def run(pg, site, *, mutate=None, report_id=None, registry=None):
    replay = Replay(BRANCH)
    replay.mutate = mutate
    serve_files(replay)
    client = make_client(replay)
    async with worker_engine(pg) as engine:
        return await sc.run_setup_check(
            engine,
            site_id=site,
            client=client,
            settings=SETTINGS,
            registry=registry or full_registry(),
            report_id=report_id,
            now=now(BRANCH),
        )


async def count(pg, sql: str, **p) -> int:
    return (await pg.run("super", sql, p))[0][0]


async def worker_readiness(pg, site):
    async with pg.conn("worker") as c:
        return await site_readiness(c, site)


async def test_a_run_writes_the_matrix_and_a_done_report_and_the_site_is_ready(pg):
    site = await make_site(pg)
    out = await run(pg, site)
    assert out.all_ok and out.release == manifest(BRANCH)["release"]
    assert await count(
        pg, "SELECT count(*) FROM assess_site_capabilities WHERE site_id=:s", s=site
    ) == len(FLAGS)
    rep = await pg.run(
        "super",
        "SELECT status, moodle_release, items, finished_at FROM assess_setup_check_reports WHERE site_id=:s",
        {"s": site},
    )
    assert len(rep) == 1
    status, release, items, finished = rep[0]
    assert (status, release) == (
        "done",
        manifest(BRANCH)["release"],
    ) and finished is not None
    assert {i["result"] for i in items} == {"OK"}
    assert set(items[0]) == {"name", "result", "reason_code"}
    assert len(items) == len(sc.BUILTIN_ITEMS) + len(sc.PLUGGABLE_ITEMS)
    assert await count(
        pg,
        "SELECT count(*) FROM assess_setup_checks WHERE site_id=:s AND result='ok'",
        s=site,
    ) == len(items)
    assert await worker_readiness(pg, site) == Ready()
    # the audit trail holds nothing for a clean run
    assert (
        await count(
            pg, "SELECT count(*) FROM assess_audit_events WHERE site_id=:s", s=site
        )
        == 0
    )


async def test_unregistered_pluggable_items_keep_the_site_not_ready(pg):
    site = await make_site(pg)
    out = await run(pg, site, registry=sc.builtin_registry())
    assert not out.all_ok
    ready = await worker_readiness(pg, site)
    assert ready == NotReady(sc.PLUGGABLE_ITEMS)


async def test_a_crash_mid_run_leaves_no_partial_matrix(pg, monkeypatch):
    site = await make_site(pg)

    async def half(conn, site_id, release, results):
        await persist_matrix(conn, site_id, release, results[:5])
        raise RuntimeError("worker died")

    monkeypatch.setattr(sc, "persist_matrix", half)
    with pytest.raises(RuntimeError, match="worker died"):
        await run(pg, site)
    assert (
        await count(
            pg, "SELECT count(*) FROM assess_site_capabilities WHERE site_id=:s", s=site
        )
        == 0
    )
    assert (
        await count(
            pg, "SELECT count(*) FROM assess_setup_checks WHERE site_id=:s", s=site
        )
        == 0
    )
    rep = await pg.run(
        "super",
        "SELECT status, finished_at IS NOT NULL FROM assess_setup_check_reports WHERE site_id=:s",
        {"s": site},
    )
    assert [tuple(r) for r in rep] == [("failed", True)]
    assert (await worker_readiness(pg, site)).__class__ is NotReady


async def test_a_release_change_is_observed_first_and_the_reprobe_restores(pg):
    old = "5.2.3+ (Build: 20260901)"
    site = await make_site(pg, release=old)
    async with pg.conn("worker") as c:
        await persist_matrix(
            c,
            site,
            old,
            tuple(FlagResult(f, "readable", None, "a" * 64) for f in FLAGS),
        )
        await c.commit()
    await run(pg, site)
    new = manifest(BRANCH)["release"]
    assert (
        await pg.run(
            "super",
            "SELECT last_seen_release FROM assess_sites WHERE id=:s",
            {"s": site},
        )
    )[0][0] == new
    assert await count(
        pg,
        "SELECT count(*) FROM assess_site_capabilities WHERE site_id=:s AND release=:r",
        s=site,
        r=new,
    ) == len(FLAGS)
    assert (
        await count(
            pg,
            "SELECT count(*) FROM assess_site_capabilities WHERE site_id=:s AND release=:r",
            s=site,
            r=old,
        )
        == 0
    )
    # the change itself was observed, audited and queued a re-check
    assert (
        await count(
            pg,
            "SELECT count(*) FROM assess_audit_events WHERE site_id=:s AND event='assess_site_release_changed'",
            s=site,
        )
        == 1
    )
    assert (
        await count(
            pg,
            "SELECT count(*) FROM assess_jobs WHERE dedupe_key=:k",
            k=f"setup_check:{site}",
        )
        == 1
    )
    assert await worker_readiness(pg, site) == Ready()


async def test_a_revoked_token_makes_the_site_not_ready_with_one_audit_row_and_one_alert(
    pg,
):
    site = await make_site(pg)
    await run(pg, site)
    assert await worker_readiness(pg, site) == Ready()
    before = await pg.run(
        "super",
        "SELECT flag, checked_at FROM assess_site_capabilities WHERE site_id=:s ORDER BY flag",
        {"s": site},
    )
    envelope = {
        "exception": "moodle_exception",
        "errorcode": "invalidtoken",
        "message": "bad",
    }
    with capture_logs() as logs:
        out = await run(pg, site, mutate=lambda _f, _b: envelope)
    assert out.token_rejected
    ready = await worker_readiness(pg, site)
    assert isinstance(ready, NotReady) and "token_works" in ready.items
    rep = (
        await pg.run(
            "super",
            "SELECT status, moodle_release, items FROM assess_setup_check_reports "
            "WHERE site_id=:s ORDER BY finished_at DESC LIMIT 1",
            {"s": site},
        )
    )[0]
    assert rep[0] == "done" and rep[1] == manifest(BRANCH)["release"]
    assert {
        "name": "token_works",
        "result": "MISSING",
        "reason_code": "invalidtoken",
    } in rep[2]
    assert (
        await count(
            pg,
            "SELECT count(*) FROM assess_audit_events WHERE site_id=:s AND event='assess_ws_token_rejected'",
            s=site,
        )
        == 1
    )
    alerts = [e for e in logs if e.get("event") == "assess_ws_token_rejected"]
    assert len(alerts) == 1 and alerts[0]["alert"] == "operator"
    assert set(alerts[0]) <= {"event", "log_level", "alert", "site_id", "reason_code"}
    # nothing was probed, so the matrix is exactly what the last good run wrote
    after = await pg.run(
        "super",
        "SELECT flag, checked_at FROM assess_site_capabilities WHERE site_id=:s ORDER BY flag",
        {"s": site},
    )
    assert after == before
    # the next good run is Ready again
    await run(pg, site)
    assert await worker_readiness(pg, site) == Ready()


async def test_a_queued_report_is_revalidated_and_run(pg):
    site = await make_site(pg)
    rid = (
        await pg.run(
            "api",
            "INSERT INTO assess_setup_check_reports (site_id) VALUES (:s) RETURNING id",
            {"s": site},
        )
    )[0][0]
    await run(pg, site, report_id=rid)
    row = (
        await pg.run(
            "super",
            "SELECT status, items <> '[]'::jsonb FROM assess_setup_check_reports WHERE id=:i",
            {"i": rid},
        )
    )[0]
    assert tuple(row) == ("done", True)
    assert (
        await count(
            pg,
            "SELECT count(*) FROM assess_setup_check_reports WHERE site_id=:s",
            s=site,
        )
        == 1
    )
    # a finished report, a report of another site and an unknown id are refused
    for bad in (rid, uuid.uuid4()):
        with pytest.raises(LookupError):
            await run(pg, site, report_id=bad)
    other = await make_site(pg)
    with pytest.raises(LookupError):
        await run(pg, other, report_id=rid)
    with pytest.raises(LookupError):
        await run(pg, uuid.uuid4())


async def test_a_report_whose_job_died_is_failed_not_queued_forever(pg):
    """Plan 12b: the hourly sweep closes a queued report no job will run; a report whose job
    is still ready (or a running one with a live job of its site) is left alone."""
    from vektra_assess.worker.setup_check import fail_abandoned_reports

    site = await make_site(pg)

    async def report(age_s: int, status: str = "queued") -> uuid.UUID:
        rid = (
            await pg.run(
                "super",
                "INSERT INTO assess_setup_check_reports (site_id, status, created_at) "
                "VALUES (:s, :st, now() - make_interval(secs => :a)) RETURNING id",
                {"s": site, "st": status, "a": age_s},
            )
        )[0][0]
        return rid

    dead, alive, young = await report(900), await report(900), await report(5)
    # a RUNNING report with no job of its own: kept while a live job of its site holds a lease,
    # failed once none does (nitpick: the running branch)
    running = await report(900, "running")
    lease = (
        await pg.run(
            "super",
            "INSERT INTO assess_jobs (kind, args, state, lease_until) VALUES ('setup_check', "
            "jsonb_build_object('site_id', CAST(:s AS text)), 'leased', now() + interval '5 min') "
            "RETURNING id",
            {"s": str(site)},
        )
    )[0][0]
    for rid, state in ((dead, "dead"), (alive, "ready")):
        await pg.run(
            "super",
            "INSERT INTO assess_jobs (kind, args, state) VALUES ('setup_check', "
            "jsonb_build_object('site_id', CAST(:s AS text), 'report_id', CAST(:r AS text)), :st)",
            {"s": str(site), "r": str(rid), "st": state},
        )
    async with worker_engine(pg) as engine:
        assert await fail_abandoned_reports(engine.connect, after_s=600) == 1
        assert await fail_abandoned_reports(engine.connect, after_s=600) == 0
        await pg.run(
            "super", "UPDATE assess_jobs SET state = 'dead' WHERE id = :j", {"j": lease}
        )
        assert await fail_abandoned_reports(engine.connect, after_s=600) == 1
    status = {
        r[0]: r[1]
        for r in await pg.run(
            "super",
            "SELECT id, status FROM assess_setup_check_reports WHERE site_id = :s",
            {"s": site},
        )
    }
    assert status == {
        dead: "failed",
        alive: "queued",
        young: "queued",
        running: "failed",
    }
