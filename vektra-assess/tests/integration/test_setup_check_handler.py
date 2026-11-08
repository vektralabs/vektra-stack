"""The ``setup_check`` job handler and the daily tick against a real Postgres, as the WORKER role.

Plan assess-moodle-ws task F4 (carried over to plan assess-job-loop, bound 2026-11-08): the
handler re-validates the report row before acting, runs ``run_setup_check`` and keeps the last
30 reports of the site; the daily tick queues one deduplicated job per enabled site. The Moodle
side is the recorded 5.2 responses (no network).
"""

from __future__ import annotations

import json
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
        job_id=0,  # never a real id (BIGSERIAL starts at 1)
        kind=JobKind.SETUP_CHECK,
        args=args,
        attempt=1,
        acquire=engine.connect,
        log=logging.getLogger("test"),
    )


async def handle(pg, args: dict[str, str | int], replay: Replay | None = None) -> None:  # type: ignore[no-untyped-def]
    replay = replay or Replay(BRANCH)
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


async def test_a_real_lookup_error_fails_the_job_instead_of_completing_it(  # type: ignore[no-untyped-def]
    pg, monkeypatch
) -> None:
    """Review finding 5: a KeyError from a bug is not "the report changed": it propagates (the
    job fails and is retried) and the report is marked failed, never treated as stale."""
    from vektra_assess.moodle import setup_check as sc

    async def broken(*_a: Any, **_k: Any) -> Any:
        raise KeyError("a bug")

    monkeypatch.setattr(sc, "evaluate", broken)
    site = await make_site(pg)
    rid = await queued_report(pg, site)
    with pytest.raises(KeyError):
        await handle(pg, {"site_id": str(site), "report_id": str(rid)})
    assert await reports(pg, site) == [(rid, "failed")]


async def test_a_report_taken_by_another_run_is_stale_not_an_error(  # type: ignore[no-untyped-def]
    pg, monkeypatch
) -> None:
    """The dedicated exception still covers the race the handler's own check cannot see."""
    from vektra_assess.moodle import setup_check as sc

    async def raced(*_a: Any, **_k: Any) -> Any:
        raise sc.SetupCheckStale("setup-check report is not runnable")

    monkeypatch.setattr(handler_mod, "run_setup_check", raced)
    site = await make_site(pg)
    rid = await queued_report(pg, site)
    await handle(
        pg, {"site_id": str(site), "report_id": str(rid)}
    )  # completes, no raise
    assert await reports(pg, site) == [(rid, "queued")]


@pytest.mark.parametrize("with_report", [True, False])
async def test_a_disabled_site_is_never_checked(pg, with_report: bool) -> None:  # type: ignore[no-untyped-def]
    """Review finding 7: no Moodle call, no matrix, no item rows for a disabled site. A report
    the API queued before the site was disabled is closed as failed (it would stay queued)."""
    site = await make_site(pg)
    rid = await queued_report(pg, site) if with_report else None
    await pg.run(
        "super",
        "UPDATE assess_sites SET disabled_at = now() WHERE id = :s",
        {"s": site},
    )
    replay = Replay(BRANCH)
    args = {"site_id": str(site)} | ({"report_id": str(rid)} if rid else {})
    await handle(pg, args, replay)
    assert replay.calls == []
    for table in ("assess_site_capabilities", "assess_setup_checks"):
        assert (
            await count(pg, f"SELECT count(*) FROM {table} WHERE site_id=:s", s=site)
            == 0
        )
    assert await reports(pg, site) == ([(rid, "failed")] if rid else [])


async def test_a_report_finished_before_the_real_claim_is_left_as_it_is(  # type: ignore[no-untyped-def]
    pg, monkeypatch
) -> None:
    """CodeRabbit nitpick (round 1): the report passes the handler's pre-check, then another
    run finishes it before ``run_setup_check`` claims it. The REAL claim refuses it
    (``SetupCheckStale``), the handler completes, and the finished report is untouched."""
    real = handler_mod.run_setup_check
    site = await make_site(pg)
    rid = await queued_report(pg, site)
    marker = '[{"name": "other_run", "result": "OK", "reason_code": null}]'

    async def finished_meanwhile(*a: Any, **k: Any) -> Any:
        await pg.run(
            "super",
            "UPDATE assess_setup_check_reports SET status = 'done', finished_at = now(), "
            "items = CAST(:m AS jsonb) WHERE id = :r",
            {"m": marker, "r": rid},
        )
        return await real(*a, **k)

    monkeypatch.setattr(handler_mod, "run_setup_check", finished_meanwhile)
    replay = Replay(BRANCH)
    await handle(pg, {"site_id": str(site), "report_id": str(rid)}, replay)  # no raise
    row = await pg.run(
        "super",
        "SELECT status, items FROM assess_setup_check_reports WHERE id = :r",
        {"r": rid},
    )
    assert row[0][0] == "done" and row[0][1] == json.loads(marker)
    assert replay.calls == []  # refused before any Moodle call
    assert (
        await count(
            pg, "SELECT count(*) FROM assess_setup_checks WHERE site_id=:s", s=site
        )
        == 0
    )


async def test_a_site_disabled_during_the_check_gets_nothing_written(  # type: ignore[no-untyped-def]
    pg, monkeypatch
) -> None:
    """CodeRabbit 4506450163: the site is disabled after the handler's own check, while the
    Moodle calls run. Nothing is persisted (no matrix, no item rows, no release) and the
    report is closed ``failed``; the job completes as stale."""
    from vektra_assess.moodle import setup_check as sc

    real = sc.evaluate
    site = await make_site(pg)

    async def then_disable(*a: Any, **k: Any) -> Any:
        out = await real(*a, **k)
        await pg.run(
            "super",
            "UPDATE assess_sites SET disabled_at = now() WHERE id = :s",
            {"s": site},
        )
        return out

    monkeypatch.setattr(sc, "evaluate", then_disable)
    rid = await queued_report(pg, site)
    await handle(pg, {"site_id": str(site), "report_id": str(rid)})  # no raise
    assert await reports(pg, site) == [(rid, "failed")]
    for table in ("assess_site_capabilities", "assess_setup_checks"):
        assert (
            await count(pg, f"SELECT count(*) FROM {table} WHERE site_id=:s", s=site)
            == 0
        )
    release = await pg.run(
        "super",
        "SELECT release, last_seen_release FROM assess_sites WHERE id = :s",
        {"s": site},
    )
    assert tuple(release[0]) == (None, None)


async def test_two_persisting_checks_of_a_site_serialise_on_the_site_row(pg) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4506662217: the persistence transaction takes the site row lock it will need
    (``observe_release`` asks FOR UPDATE) up front. Two transactions that both held FOR SHARE
    and then asked FOR UPDATE would deadlock and fail one report; with FOR UPDATE first, the
    second simply waits for the first."""
    from sqlalchemy import text as sql
    from sqlalchemy.exc import DBAPIError

    from vektra_assess.moodle import setup_check as sc

    site = await make_site(pg)
    async with pg.conn("worker") as a, pg.conn("worker") as b:
        await a.execute(sc._SITE_ENABLED, {"s": site})  # first persist holds the row
        await b.execute(sql("SET lock_timeout = '300ms'"))
        with pytest.raises(DBAPIError, match="lock timeout"):
            await b.execute(sc._SITE_ENABLED, {"s": site})  # the second one waits
        await b.rollback()
        await a.rollback()


async def _report(pg, site, status: str, age_s: float) -> uuid.UUID:  # type: ignore[no-untyped-def]
    return (
        await pg.run(
            "super",
            "INSERT INTO assess_setup_check_reports (site_id, status, created_at) "
            "VALUES (:s, :st, now() - make_interval(secs => :a)) RETURNING id",
            {"s": site, "st": status, "a": age_s},
        )
    )[0][0]


async def test_an_old_running_report_with_a_live_lease_is_left_alone(pg) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4506450159: age alone does not prove a run is dead. While another
    ``setup_check`` job of the site holds a live lease (a report that waited queued longer
    than the lease, now running), the old ``running`` report is not failed."""
    site = await make_site(pg)
    running = await _report(pg, site, "running", handler_mod.ORPHAN_AFTER_S + 60)
    await pg.run(
        "super",
        "INSERT INTO assess_jobs (kind, args, state, lease_until, leased_by, attempts) "
        "VALUES ('setup_check', jsonb_build_object('site_id', CAST(:s AS text), "
        "'report_id', CAST(:r AS text)), 'leased', now() + interval '3 minutes', "
        "'other-worker', 1)",
        {"s": str(site), "r": str(running)},
    )
    try:
        await handle(pg, {"site_id": str(site)})
        assert dict(await reports(pg, site))[running] == "running"
    finally:
        await pg.run(
            "super",
            "DELETE FROM assess_jobs WHERE kind = 'setup_check' AND args->>'site_id' = :s",
            {"s": str(site)},
        )


async def test_a_report_orphaned_running_by_a_crash_is_failed(pg) -> None:  # type: ignore[no-untyped-def]
    """Review finding 6: a worker killed mid-run (no ``except`` runs) leaves its report
    ``running``; the next run of the site closes it ``failed``. A recent one is left alone."""
    site = await make_site(pg)
    orphan = await _report(pg, site, "running", handler_mod.ORPHAN_AFTER_S + 60)
    recent = await _report(pg, site, "running", 5)
    await handle(pg, {"site_id": str(site)})
    by_id = dict(await reports(pg, site))
    assert by_id[orphan] == "failed" and by_id[recent] == "running"
    finished = await count(
        pg,
        "SELECT count(*) FROM assess_setup_check_reports WHERE id=:r "
        "AND finished_at IS NOT NULL",
        r=orphan,
    )
    assert finished == 1


async def test_the_item_rows_are_trimmed_with_the_reports(pg) -> None:  # type: ignore[no-untyped-def]
    """Review finding 4: ``assess_setup_checks`` keeps the item rows of the last 30 runs (one
    run's rows share ``run_at``, written in one transaction); another site is untouched."""
    site, other = await make_site(pg), await make_site(pg)
    for who in (site, other):
        await pg.run(
            "super",
            "INSERT INTO assess_setup_checks (site_id, item, result, run_at) "
            "SELECT :s, i, 'ok', now() - make_interval(days => g) "
            "FROM generate_series(1, 35) g, unnest(ARRAY['token_works', 'ws_tls_ok']) i",
            {"s": who},
        )
    await handle(pg, {"site_id": str(site)})
    runs = await count(
        pg,
        "SELECT count(DISTINCT run_at) FROM assess_setup_checks WHERE site_id=:s",
        s=site,
    )
    assert runs == handler_mod.KEEP_REPORTS
    rows = await count(
        pg, "SELECT count(*) FROM assess_setup_checks WHERE site_id=:s", s=site
    )
    assert rows == (handler_mod.KEEP_REPORTS - 1) * 2 + len(full_registry().names())
    assert (
        await count(
            pg, "SELECT count(*) FROM assess_setup_checks WHERE site_id=:s", s=other
        )
        == 70
    )


async def test_the_keep_30_counts_finished_reports_only(pg) -> None:  # type: ignore[no-untyped-def]
    """Review finding 6: queued reports newer than the finished ones must not take places in
    the 30 kept, or fewer than 30 finished reports survive."""
    site = await make_site(pg)
    await pg.run(
        "super",
        "INSERT INTO assess_setup_check_reports (site_id, status, created_at, finished_at) "
        "SELECT :s, 'done', now() - make_interval(days => g), now() - make_interval(days => g) "
        "FROM generate_series(1, 40) g",
        {"s": site},
    )
    for _ in range(3):
        await _report(pg, site, "queued", 1)
    await handle(pg, {"site_id": str(site)})
    statuses = [s for _i, s in await reports(pg, site)]
    assert statuses.count("done") == handler_mod.KEEP_REPORTS
    assert statuses.count("queued") == 3
