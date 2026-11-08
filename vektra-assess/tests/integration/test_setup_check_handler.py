"""The ``setup_check`` job handler and the daily tick against a real Postgres, as the WORKER role.

Plan assess-moodle-ws task F4 (carried over to plan assess-job-loop, bound 2026-11-08): the
handler re-validates the report row before acting, runs ``run_setup_check`` and keeps the last
30 reports of the site; the daily tick queues one deduplicated job per enabled site. The Moodle
side is the recorded 5.2 responses (no network).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest
from assess_moodle_support import Replay, make_client, now
from sqlalchemy.ext.asyncio import create_async_engine

from vektra_assess.jobs import JobKind
from vektra_assess.worker import setup_check as handler_mod
from vektra_assess.worker.runner import JobContext

from .test_setup_check_run import (
    SETTINGS,
    count,
    full_registry,
    make_site,
    serve_files,
)

pytestmark = pytest.mark.integration

BRANCH = "5.2"


def _ctx(engine: Any, args: dict[str, str | int]) -> JobContext:
    return JobContext(
        job_id=1,
        kind=JobKind.SETUP_CHECK,
        args=args,
        attempt=1,
        acquire=engine.connect,
        log=logging.getLogger("test"),
    )


async def handle(pg, args: dict[str, str | int]) -> None:  # type: ignore[no-untyped-def]
    replay = Replay(BRANCH)
    serve_files(replay)
    engine = create_async_engine(pg.url("worker"))
    try:
        h = handler_mod.make_setup_check_handler(
            engine,
            make_client(replay),
            SETTINGS,
            registry=full_registry(),
            now=lambda: now(BRANCH),
        )
        await h(_ctx(engine, args))
    finally:
        await engine.dispose()


async def reports(pg, site) -> list[tuple[Any, ...]]:  # type: ignore[no-untyped-def]
    rows = await pg.run(
        "super",
        "SELECT id, status FROM assess_setup_check_reports WHERE site_id=:s "
        "ORDER BY created_at",
        {"s": site},
    )
    return [tuple(r) for r in rows]


async def queued_report(pg, site) -> uuid.UUID:  # type: ignore[no-untyped-def]
    """As the API queues it (``POST /sites/{site}/setup-check``), as the API role."""
    return (
        await pg.run(
            "api",
            "INSERT INTO assess_setup_check_reports (site_id) VALUES (:s) RETURNING id",
            {"s": site},
        )
    )[0][0]


async def test_a_queued_report_is_run_to_done(pg) -> None:  # type: ignore[no-untyped-def]
    site = await make_site(pg)
    rid = await queued_report(pg, site)
    await handle(pg, {"site_id": str(site), "report_id": str(rid)})
    assert await reports(pg, site) == [(rid, "done")]
    assert await count(
        pg,
        "SELECT count(*) FROM assess_setup_checks WHERE site_id=:s AND result='ok'",
        s=site,
    ) == len(full_registry().names())


async def test_a_job_without_a_report_writes_its_own(pg) -> None:  # type: ignore[no-untyped-def]
    """The daily tick and ``observe_release`` queue ``{site_id}`` only."""
    site = await make_site(pg)
    await handle(pg, {"site_id": str(site)})
    assert [s for _i, s in await reports(pg, site)] == ["done"]


@pytest.mark.parametrize("case", ["finished", "unknown_report", "other_site"])
async def test_a_stale_report_is_a_no_op_that_completes(pg, case: str) -> None:  # type: ignore[no-untyped-def]
    site = await make_site(pg)
    rid = await queued_report(pg, site)
    await handle(pg, {"site_id": str(site), "report_id": str(rid)})
    before = await reports(pg, site)
    target_site, target_report = {
        "finished": (site, rid),  # already done: a retry of a finished job
        "unknown_report": (site, uuid.uuid4()),
        "other_site": (await make_site(pg), rid),
    }[case]
    await handle(pg, {"site_id": str(target_site), "report_id": str(target_report)})
    assert await reports(pg, site) == before
    assert await reports(pg, target_site) == (before if target_site == site else [])


@pytest.mark.parametrize(
    "args",
    [
        {"site_id": "not-a-uuid"},
        {},
        {"site_id": str(uuid.UUID(int=7))},  # no such site
        {"site_id": "{site}", "report_id": "not-a-uuid"},
    ],
)
async def test_malformed_or_unknown_ids_complete_without_acting(pg, args) -> None:  # type: ignore[no-untyped-def]
    site = await make_site(pg)
    args = {k: str(v).replace("{site}", str(site)) for k, v in args.items()}
    await handle(pg, args)
    assert await reports(pg, site) == []
    assert (
        await count(
            pg, "SELECT count(*) FROM assess_setup_checks WHERE site_id=:s", s=site
        )
        == 0
    )


async def test_the_last_30_reports_of_the_site_are_kept(pg) -> None:  # type: ignore[no-untyped-def]
    site = await make_site(pg)
    other = await make_site(pg)
    for who in (site, other):
        await pg.run(
            "super",
            "INSERT INTO assess_setup_check_reports (site_id, status, created_at, finished_at) "
            "SELECT :s, 'done', now() - make_interval(days => g), now() - make_interval(days => g) "
            "FROM generate_series(1, 32) g",
            {"s": who},
        )
    # an old report still queued is never trimmed: the API answered 202 with its id
    old_queued = (
        await pg.run(
            "super",
            "INSERT INTO assess_setup_check_reports (site_id, created_at) "
            "VALUES (:s, now() - interval '90 days') RETURNING id",
            {"s": site},
        )
    )[0][0]
    await handle(pg, {"site_id": str(site)})
    rows = await reports(pg, site)
    finished = [r for r in rows if r[1] in ("done", "failed")]
    assert len(finished) == handler_mod.KEEP_REPORTS
    assert (old_queued, "queued") in rows
    # the newest (this run) is among the kept; the other site is untouched
    assert rows[-1][1] == "done"
    assert len(await reports(pg, other)) == 32


async def test_the_daily_tick_queues_one_job_per_enabled_site(pg) -> None:  # type: ignore[no-untyped-def]
    a, b, off = await make_site(pg), await make_site(pg), await make_site(pg)
    await pg.run(
        "super", "UPDATE assess_sites SET disabled_at = now() WHERE id = :s", {"s": off}
    )
    engine = create_async_engine(pg.url("worker"))
    try:
        await handler_mod.queue_setup_checks(engine.connect)
        await handler_mod.queue_setup_checks(engine.connect)  # pending: deduplicated
    finally:
        await engine.dispose()
    rows = await pg.run(
        "super",
        "SELECT args->>'site_id', dedupe_key, args ? 'report_id' FROM assess_jobs "
        "WHERE kind = 'setup_check' AND state = 'ready' AND args->>'site_id' IN (:a, :b, :o)",
        {"a": str(a), "b": str(b), "o": str(off)},
    )
    got = sorted(tuple(r) for r in rows)
    # the same dedupe key as the release-change enqueue (capabilities.observe_release)
    assert got == sorted((str(s), f"setup_check:{s}", False) for s in (a, b))
