"""OQ-033: an erasure the LMS cannot see fail is reported to the operator by Vektra.

The LMS's privacy API completes its own request whatever the provider raises, so
the loop closes here: an erasure request that ends ``failed`` (verification) queues one
``operator_notify``, and a request still not completed after
``VEKTRA_ASSESS_ERASURE_ALERT_AFTER_S`` queues one, deduplicated per request, from the worker's
``delivery_retry`` tick. The job carries the site, the request id, the scope and a reason code:
no user id, no subject hash, no cmid. A completed request never notifies.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.world import world

from vektra_assess.privacy.erasure import alert_overdue_erasures, run_erasure

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


async def _notifies(pg, site: uuid.UUID) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    rows = await pg.run(
        "super",
        "SELECT args FROM assess_jobs WHERE kind = 'operator_notify' "
        "AND args->>'site_id' = :s ORDER BY id",
        {"s": str(site)},
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


async def test_a_failed_erasure_queues_exactly_one_operator_alert(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        # a subject request without its user id fails its own verification (no deletion)
        rid = await _record(pg, w.site, scope="subject", subject_userids=None)
        assert await run_erasure(worker.connect, rid) == "failed"
        assert await run_erasure(worker.connect, rid) == "done"  # a retried job
        jobs = await _notifies(pg, w.site)
        assert jobs == [
            {
                "site_id": str(w.site),
                "kind": "erasure_verification_failed",
                "erasure_id": rid,
                "scope": "subject",
                "reason": "verification_failed",
            }
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
        # even once the alert was delivered (the job is done), never again for this request
        await pg.run(
            "super",
            "UPDATE assess_jobs SET state = 'done' WHERE kind = 'operator_notify'",
        )
        assert await alert_overdue_erasures(worker.connect, after_s=3600) == 0
        jobs = await _notifies(pg, w.site)
        assert jobs == [
            {
                "site_id": str(w.site),
                "kind": "erasure_overdue",
                "erasure_id": rid,
                "scope": "activity",
                "reason": "not_completed",
            }
        ]
        # a second overdue request is its own alert
        other = await _record(pg, w.site, age="3 hours", cmid=9_902)
        assert await alert_overdue_erasures(worker.connect, after_s=3600) == 1
        assert [j["erasure_id"] for j in await _notifies(pg, w.site)] == [rid, other]


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
        assert set(args) == {"site_id", "kind", "erasure_id", "scope", "reason"}
        assert "4242" not in str(args) and "h" * 64 not in str(args)
        assert args["erasure_id"] == rid
