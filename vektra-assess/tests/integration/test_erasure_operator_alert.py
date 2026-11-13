"""OQ-033: an erasure the LMS cannot see fail is reported to the operator by Vektra.

The LMS's privacy API completes its own request whatever the provider raises, so the loop closes
here, with one ``operator_notify`` per event:

- ``erasure_verification_failed``: the request was recorded ``failed`` by its run;
- ``erasure_overdue``: still not completed ``VEKTRA_ASSESS_ERASURE_ALERT_AFTER_S`` after it was
  made (the worker's ``delivery_retry`` tick); the marker is on the request
  (``overdue_alerted_at``), so pruned job rows never bring a second email;
- ``erasure_abandoned``: still open a day later, recorded ``failed`` by the daily purge.

The job carries the site, the request id and the scope: no user id, no subject hash, no cmid.
A request that completes never notifies.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.world import world

from vektra_assess.privacy.erasure import (
    OVERDUE_BATCH,
    alert_overdue_erasures,
    record_failure,
    run_erasure,
)
from vektra_assess.privacy.purge import run_purge

pytestmark = pytest.mark.integration


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    from integration.test_session_preflight import _reset

    await _reset(pg)
    # the sweep reads every site: start each test without other tests' requests or alerts
    await pg.run("super", "DELETE FROM assess_erasure_records")
    await pg.run("super", "DELETE FROM assess_jobs WHERE kind = 'operator_notify'")
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


async def _notifies(pg, site: uuid.UUID | None = None) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    rows = await pg.run(
        "super",
        "SELECT args FROM assess_jobs WHERE kind = 'operator_notify' "
        "AND (CAST(:s AS text) IS NULL OR args->>'site_id' = :s) ORDER BY id",
        {"s": None if site is None else str(site)},
    )
    return [r[0] for r in rows]


async def _record(  # type: ignore[no-untyped-def]
    pg, site: uuid.UUID, *, scope: str = "activity", age: str = "0 seconds", **cols: Any
) -> int:
    if scope == "activity":
        cols.setdefault("cmid", 9_901)
    else:
        cols.setdefault("subject_userid_hash", "h" * 64)
    names = ", ".join(cols)
    values = ", ".join(f":{k}" for k in cols)
    return int(
        (
            await pg.run(
                "super",
                f"INSERT INTO assess_erasure_records (site_id, scope, requested_at, {names}) "
                f"VALUES (:s, :scope, now() - interval '{age}', {values}) RETURNING id",
                {"s": site, "scope": scope, **cols},
            )
        )[0][0]
    )


def _alert(kind: str, site: uuid.UUID, rid: int, scope: str) -> dict[str, Any]:
    return {"site_id": str(site), "kind": kind, "erasure_id": rid, "scope": scope}


async def test_a_failed_record_keeps_the_rows_left_by_known_table(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4558071155: the runbook's fallback when the log line is gone. Only names
    of the erasure's own table list are kept; anything else counts as ``other``."""
    async with world(pg) as w:
        rid = await _record(pg, w.site, age="1 minute")
        left = {"assess_turns": 2, "assess_sessions": 1, "not a table": 5}
        assert await record_failure(worker.connect, rid, left) == "failed"
        rows = await pg.run(
            "super",
            "SELECT outcome, error_code, rows_deleted FROM assess_erasure_records "
            "WHERE id = :i",
            {"i": rid},
        )
        assert [tuple(r) for r in rows] == [
            (
                "failed",
                "verification_failed",
                {"left": {"assess_turns": 2, "assess_sessions": 1, "other": 5}},
            )
        ]


async def test_a_failed_erasure_queues_exactly_one_operator_alert(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        # a subject request without its user id fails its own verification (no deletion)
        rid = await _record(pg, w.site, scope="subject", subject_userids=None)
        assert await run_erasure(worker.connect, rid) == "failed"
        assert await run_erasure(worker.connect, rid) == "done"  # a retried job
        assert await _notifies(pg, w.site) == [
            _alert("erasure_verification_failed", w.site, rid, "subject")
        ]
        # a failed request is completed: the overdue sweep never adds a second alert
        await pg.run(
            "super",
            "UPDATE assess_erasure_records SET requested_at = now() - interval '5 hours' "
            "WHERE id = :i",
            {"i": rid},
        )
        assert await alert_overdue_erasures(worker.connect, after_s=3600) == 0
        assert len(await _notifies(pg, w.site)) == 1


async def test_two_overlapping_runs_alert_once(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """Review finding 4: the failure is recorded, and alerted, by one run only."""
    async with world(pg) as w:
        rid = await _record(pg, w.site, scope="subject", subject_userids=None)
        got = await asyncio.gather(
            run_erasure(worker.connect, rid), run_erasure(worker.connect, rid)
        )
        assert sorted(got) == ["done", "failed"], got
        assert len(await _notifies(pg, w.site)) == 1


async def test_a_request_another_run_finished_is_not_recorded_failed(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """Review finding 4: a run that reaches the failure step after another run completed the
    request leaves it as it is (``completed_at IS NULL`` on the update) and alerts nothing."""
    async with world(pg) as w:
        rid = await _record(pg, w.site, age="1 minute")
        assert await run_erasure(worker.connect, rid) == "erased"
        assert await record_failure(worker.connect, rid) == "done"
        rows = await pg.run(
            "super",
            "SELECT outcome, error_code FROM assess_erasure_records WHERE id = :i",
            {"i": rid},
        )
        assert [tuple(r) for r in rows] == [("erased", None)]
        assert await _notifies(pg, w.site) == []


async def test_an_overdue_erasure_alerts_once_after_the_threshold(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        rid = await _record(pg, w.site, age="30 minutes")
        assert await alert_overdue_erasures(worker.connect, after_s=3600) == 0
        assert await _notifies(pg, w.site) == []
        await pg.run(
            "super",
            "UPDATE assess_erasure_records SET requested_at = now() - interval '2 hours' "
            "WHERE id = :i",
            {"i": rid},
        )
        assert await alert_overdue_erasures(worker.connect, after_s=3600) == 1
        assert await alert_overdue_erasures(worker.connect, after_s=3600) == 0
        assert await _notifies(pg, w.site) == [
            _alert("erasure_overdue", w.site, rid, "activity")
        ]
        marked = await pg.run(
            "super",
            "SELECT overdue_alerted_at IS NOT NULL FROM assess_erasure_records WHERE id = :i",
            {"i": rid},
        )
        assert marked[0][0] is True
        # the job rows were pruned since: the marker on the request still holds
        await pg.run("super", "DELETE FROM assess_jobs WHERE kind = 'operator_notify'")
        assert await alert_overdue_erasures(worker.connect, after_s=3600) == 0
        assert await _notifies(pg, w.site) == []
        # a second overdue request is its own alert
        other = await _record(pg, w.site, age="3 hours", cmid=9_902)
        assert await alert_overdue_erasures(worker.connect, after_s=3600) == 1
        assert await _notifies(pg, w.site) == [
            _alert("erasure_overdue", w.site, other, "activity")
        ]


async def test_more_overdue_requests_than_a_batch_are_all_alerted(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """Review finding 1: a batch never gets stuck on requests already alerted."""
    async with world(pg) as w:
        n = OVERDUE_BATCH + 5
        await pg.run(
            "super",
            "INSERT INTO assess_erasure_records (site_id, scope, cmid, requested_at) "
            "SELECT :s, 'activity', 9000 + g, now() - interval '2 hours' "
            "FROM generate_series(1, :n) AS g",
            {"s": w.site, "n": n},
        )
        first = await alert_overdue_erasures(worker.connect, after_s=3600)
        second = await alert_overdue_erasures(worker.connect, after_s=3600)
        third = await alert_overdue_erasures(worker.connect, after_s=3600)
        assert (first, second, third) == (OVERDUE_BATCH, 5, 0)
        alerted = {a["erasure_id"] for a in await _notifies(pg, w.site)}
        assert len(alerted) == n


async def test_an_abandoned_request_alerts_from_the_purge(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """Review finding 3: the daily purge records a day-old open request failed (abandoned),
    and says so to the operator, even when it was already reported overdue."""
    async with world(pg) as w:
        rid = await _record(pg, w.site, scope="subject", age="2 days")
        assert await alert_overdue_erasures(worker.connect, after_s=3600) == 1
        await run_purge(worker.connect)
        rows = await pg.run(
            "super",
            "SELECT outcome, error_code FROM assess_erasure_records WHERE id = :i",
            {"i": rid},
        )
        assert [tuple(r) for r in rows] == [("failed", "abandoned")]
        assert await _notifies(pg, w.site) == [
            _alert("erasure_overdue", w.site, rid, "subject"),
            _alert("erasure_abandoned", w.site, rid, "subject"),
        ]
        await run_purge(worker.connect)  # the next day: nothing again
        assert len(await _notifies(pg, w.site)) == 2


async def test_a_completed_erasure_never_notifies(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        rid = await _record(pg, w.site, age="3 hours")
        assert await run_erasure(worker.connect, rid) == "erased"
        assert await alert_overdue_erasures(worker.connect, after_s=3600) == 0
        assert await _notifies(pg, w.site) == []


async def test_the_operator_job_names_no_subject(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """The alert carries ids and codes: no user id, no subject hash, no cmid."""
    async with world(pg) as w:
        rid = await _record(
            pg, w.site, scope="subject", age="2 hours", subject_userids=[4242]
        )
        assert await alert_overdue_erasures(worker.connect, after_s=3600) == 1
        (args,) = await _notifies(pg, w.site)
        assert set(args) == {"site_id", "kind", "erasure_id", "scope"}
        assert "4242" not in str(args) and "h" * 64 not in str(args)
        assert args["erasure_id"] == rid
