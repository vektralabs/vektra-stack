"""Startup reconciliation on a real Postgres (plan assess-job-loop B8).

Expired leases are requeued or, when exhausted, audited and marked dead; a failing audit leaves
the job leased; two concurrent passes serialise on the lock and the second finds nothing to do.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from vektra_assess.worker import locks
from vektra_assess.worker.listener import ControlConnection
from vektra_assess.worker.reconcile import ReconcileError, reconcile

pytestmark = pytest.mark.integration


def _dsn(pg) -> str:
    return (
        make_url(pg.url("worker"))
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )


async def _seed(pg) -> dict[str, int]:
    """ready, leased-expired-with-attempts, leased-expired-exhausted, leased-still-valid."""
    await pg.run("super", "DELETE FROM assess_jobs")
    ids: dict[str, int] = {}
    rows = {
        "ready": ("ready", "now() + interval '1 hour'", 0, 5),
        "retry": ("leased", "now() - interval '5 minutes'", 2, 5),
        "exhausted": ("leased", "now() - interval '5 minutes'", 3, 3),
        "live": ("leased", "now() + interval '5 minutes'", 3, 3),
    }
    for name, (state, lease, attempts, mx) in rows.items():
        ids[name] = (
            await pg.run(
                "super",
                "INSERT INTO assess_jobs (kind, state, lease_until, leased_by, attempts, max_attempts) "
                f"VALUES ('purge', :s, {lease}, 'old-worker', :a, :m) RETURNING id",
                {"s": state, "a": attempts, "m": mx},
            )
        )[0][0]
    return ids


async def _state(pg, job_id: int):
    return tuple(
        (
            await pg.run(
                "super",
                "SELECT state, last_error_code, lease_until IS NULL FROM assess_jobs WHERE id=:i",
                {"i": job_id},
            )
        )[0]
    )


@asynccontextmanager
async def _acquire(dsn: str):
    conn = await asyncpg.connect(dsn)
    try:
        yield conn
    finally:
        await conn.close()


class _Audit:
    def __init__(self, fail: bool = False) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    async def __call__(self, event: str, data: Any) -> None:
        if self.fail:
            raise RuntimeError("audit unavailable")
        self.events.append((event, dict(data)))


class _Ticks:
    def __init__(self) -> None:
        self.ran = 0

    async def run_startup_ticks(self) -> None:
        self.ran += 1


async def test_requeues_retryable_kills_exhausted_with_audit_and_leaves_live_leases(pg):
    ids = await _seed(pg)
    ctl = ControlConnection(_dsn(pg), on_notify=lambda: None)
    await ctl.start()
    audit, ticks = _Audit(), _Ticks()
    try:
        report = await reconcile(ctl, lambda: _acquire(_dsn(pg)), audit, ticks)
    finally:
        await ctl.stop()
    assert (report.requeued, report.marked_dead, report.audit_failed) == (1, 1, 0)
    assert (await _state(pg, ids["ready"]))[0] == "ready"
    assert (await _state(pg, ids["retry"])) == ("ready", None, True)
    assert (await _state(pg, ids["exhausted"])) == (
        "dead",
        "lease_expired_exhausted",
        True,
    )
    assert (await _state(pg, ids["live"]))[0] == "leased", (
        "a valid lease is never touched"
    )
    assert audit.events == [
        (
            "assess_job_dead",
            {
                "job_id": ids["exhausted"],
                "kind": "purge",
                "attempts": 3,
                "error_code": "lease_expired_exhausted",
            },
        )
    ]
    assert ticks.ran == 1
    assert locks.RECONCILE not in ctl.held_locks


async def test_a_failing_audit_leaves_the_job_leased_and_the_next_pass_retries(pg):
    ids = await _seed(pg)
    ctl = ControlConnection(_dsn(pg), on_notify=lambda: None)
    await ctl.start()
    try:
        bad = await reconcile(ctl, lambda: _acquire(_dsn(pg)), _Audit(fail=True))
        assert (bad.marked_dead, bad.audit_failed) == (0, 1)
        assert (await _state(pg, ids["exhausted"]))[0] == "leased", (
            "no audit row, no dead job"
        )
        good = _Audit()
        again = await reconcile(ctl, lambda: _acquire(_dsn(pg)), good)
    finally:
        await ctl.stop()
    assert (again.marked_dead, again.audit_failed) == (1, 0)
    assert (await _state(pg, ids["exhausted"]))[0] == "dead"
    assert len(good.events) == 1


async def test_two_concurrent_passes_serialise_and_audit_once(pg):
    await _seed(pg)
    a = ControlConnection(_dsn(pg), on_notify=lambda: None)
    b = ControlConnection(_dsn(pg), on_notify=lambda: None)
    await a.start()
    await b.start()
    audit = _Audit()
    try:
        ra, rb = await asyncio.gather(
            reconcile(a, lambda: _acquire(_dsn(pg)), audit, poll_s=0.02),
            reconcile(b, lambda: _acquire(_dsn(pg)), audit, poll_s=0.02),
        )
    finally:
        await a.stop()
        await b.stop()
    assert sorted((ra.requeued, rb.requeued)) == [0, 1]
    assert sorted((ra.marked_dead, rb.marked_dead)) == [0, 1]
    assert len(audit.events) == 1, "the second pass must not audit the same job again"


async def test_a_lock_that_stays_taken_refuses_to_start(pg):
    held = await asyncpg.connect(_dsn(pg))
    ctl = ControlConnection(_dsn(pg), on_notify=lambda: None)
    await ctl.start()
    try:
        assert (
            await held.fetchval(f"SELECT pg_try_advisory_lock({locks.RECONCILE})")
            is True
        )
        with pytest.raises(ReconcileError, match="held the reconcile lock"):
            await reconcile(
                ctl,
                lambda: _acquire(_dsn(pg)),
                _Audit(),
                lock_timeout_s=0.3,
                poll_s=0.05,
            )
    finally:
        await held.close()
        await ctl.stop()


async def test_lease_reaper_finishes_what_a_failed_audit_left_leased(pg):
    """Review finding: without a periodic pass the row would stay leased until a restart."""
    from vektra_assess.worker.reconcile import reap_exhausted

    ids = await _seed(pg)
    ok, bad = _Audit(), _Audit(fail=True)
    first = await reap_exhausted(lambda: _acquire(_dsn(pg)), bad)
    assert (first.marked_dead, first.audit_failed) == (0, 1)
    assert (await _state(pg, ids["exhausted"]))[0] == "leased"
    second = await reap_exhausted(lambda: _acquire(_dsn(pg)), ok)
    assert (second.marked_dead, second.audit_failed) == (1, 0)
    assert (await _state(pg, ids["exhausted"]))[0] == "dead"
    assert len(ok.events) == 1
    assert (await _state(pg, ids["retry"]))[0] == "leased", (
        "the reaper only handles exhausted jobs"
    )


async def test_the_exhausted_pass_pages_by_id_and_a_failing_row_does_not_block_the_rest(
    pg, monkeypatch
):
    from vektra_assess.worker import reconcile as rec

    monkeypatch.setattr(rec, "_BATCH", 2)
    await pg.run("super", "DELETE FROM assess_jobs")
    ids = []
    for _ in range(5):
        ids.append(
            (
                await pg.run(
                    "super",
                    "INSERT INTO assess_jobs (kind, state, lease_until, leased_by, attempts, max_attempts) "
                    "VALUES ('purge', 'leased', now() - interval '1 minute', 'x', 1, 1) RETURNING id",
                )
            )[0][0]
        )
    seen: list[int] = []

    async def flaky(event, data) -> None:
        seen.append(data["job_id"])
        if data["job_id"] == ids[1]:
            raise RuntimeError("audit unavailable for this one")

    report = await rec.reap_exhausted(lambda: _acquire(_dsn(pg)), flaky)
    assert (report.marked_dead, report.audit_failed) == (4, 1)
    assert seen == ids, "every row was visited once, in id order, across three batches"
    states = {
        r[0]: r[1] for r in await pg.run("super", "SELECT id, state FROM assess_jobs")
    }
    assert states[ids[1]] == "leased" and all(
        states[i] == "dead" for i in ids if i != ids[1]
    )
