"""``WorkerRuntime`` against an in-memory ``JobStore`` (plan B3).

The store mimics the fenced SQL (claim by kind and free slots, fenced complete/fail/dead); the
SQL itself is proven on Postgres in ``integration/test_worker_sql_db.py`` and the whole loop in
``integration/test_worker_runtime_db.py``.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Sequence
from contextlib import asynccontextmanager
from typing import Any

import pytest

from vektra_assess.jobs import JobKind
from vektra_assess.worker import sql
from vektra_assess.worker.runner import HandlerSpec, JobContext
from vektra_assess.worker.runtime import PoolSpec, WorkerRuntime, backoff_s


class MemStore:
    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.claim_calls: list[tuple[list[str], int]] = []
        self._next = 1

    def add(self, kind: JobKind, n: int = 1, *, max_attempts: int = 5) -> list[int]:
        ids = []
        for _ in range(n):
            self.rows[self._next] = {
                "kind": kind, "state": "ready", "attempts": 0, "max": max_attempts,
                "owner": None, "code": None, "backoff": None,
            }  # fmt: skip
            ids.append(self._next)
            self._next += 1
        return ids

    async def claim(
        self,
        kinds: Sequence[JobKind],
        worker_id: str,
        lease_s: Any,
        free: int,
        max_attempts: Any = None,
    ) -> list[sql.ClaimedJob]:
        self.claim_calls.append(([k.value for k in kinds], free))
        out: list[sql.ClaimedJob] = []
        for jid, r in self.rows.items():
            if len(out) >= free:
                break
            if (
                r["kind"] in kinds
                and r["state"] == "ready"
                and r["attempts"] < r["max"]
            ):
                r.update(state="leased", owner=worker_id, attempts=r["attempts"] + 1)
                out.append(
                    sql.ClaimedJob(jid, r["kind"], {"n": jid}, r["attempts"], r["max"])
                )
        return out

    def _fenced(self, jid: int, worker: str, attempt: int) -> dict[str, Any] | None:
        r = self.rows[jid]
        ok = (
            r["owner"] == worker and r["attempts"] == attempt and r["state"] == "leased"
        )
        return r if ok else None

    async def complete(self, jid: int, worker: str, attempt: int) -> bool:
        r = self._fenced(jid, worker, attempt)
        if r:
            r["state"] = "done"
        return r is not None

    async def fail(
        self, jid: int, worker: str, attempt: int, backoff: float, code: str
    ) -> bool:
        r = self._fenced(jid, worker, attempt)
        if r:
            r.update(state="ready", owner=None, code=code, backoff=backoff)
        return r is not None

    async def release(self, jid: int, worker: str, attempt: int) -> bool:
        r = self._fenced(jid, worker, attempt)
        if r:
            r.update(state="ready", owner=None, attempts=r["attempts"] - 1)
        return r is not None

    async def mark_dead(self, jid: int, worker: str, attempt: int, code: str) -> bool:
        r = self._fenced(jid, worker, attempt)
        if r:
            r.update(state="dead", code=code)
        return r is not None


@asynccontextmanager
async def _no_db():  # type: ignore[no-untyped-def]
    raise AssertionError("handlers in these tests do not touch the DB")
    yield


class Audit:
    def __init__(self, fail: bool = False) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    async def __call__(self, event: str, data: Any) -> None:
        if self.fail:
            raise RuntimeError("audit table unavailable")
        self.events.append((event, dict(data)))


def _rt(store: MemStore, audit: Audit | None = None, **kw: Any) -> WorkerRuntime:
    return WorkerRuntime(
        store,
        worker_id="w1",
        pools=kw.pop("pools", [PoolSpec("maintenance", 2), PoolSpec("ws", 1)]),
        acquire_db=_no_db,
        audit=audit or Audit(),
        rng=random.Random(1),
        **kw,
    )


def _spec(
    kind: JobKind, handle: Any, pool: str = "maintenance", **kw: Any
) -> HandlerSpec:
    return HandlerSpec(
        kind,
        pool,
        budget_s=kw.pop("budget_s", 1),
        lease_s=kw.pop("lease_s", 2),
        handle=handle,
        **kw,
    )


async def test_a_successful_job_is_completed_once() -> None:
    store, seen = MemStore(), []

    async def ok(ctx: JobContext) -> None:
        seen.append((ctx.job_id, ctx.attempt, dict(ctx.args)))

    (jid,) = store.add(JobKind.PURGE)
    rt = _rt(store)
    rt.register_handler(_spec(JobKind.PURGE, ok))
    assert await rt.claim_once() == 1
    await rt.wait_idle()
    assert seen == [(jid, 1, {"n": jid})]
    assert store.rows[jid]["state"] == "done"
    assert rt.counters[("completed", JobKind.PURGE)] == 1
    assert rt.free_slots("maintenance") == 2


async def test_a_pool_is_never_over_claimed() -> None:
    store, gate = MemStore(), asyncio.Event()

    async def slow(ctx: JobContext) -> None:
        await gate.wait()

    store.add(JobKind.PURGE, 5)
    rt = _rt(store)
    rt.register_handler(_spec(JobKind.PURGE, slow, budget_s=30, lease_s=60))
    assert await rt.claim_once() == 2  # concurrency 2
    assert store.claim_calls[-1][1] == 2
    assert await rt.claim_once() == 0  # no free slot: the store is not even asked
    assert len(store.claim_calls) == 1
    assert rt.in_flight == 2 and rt.free_slots("maintenance") == 0
    gate.set()
    await rt.wait_idle()
    assert await rt.claim_once() == 2  # slots freed, the next two are claimed
    gate.set()
    await rt.wait_idle()


async def test_a_stalled_pool_does_not_starve_another() -> None:
    store, gate, done = MemStore(), asyncio.Event(), []

    async def stuck(ctx: JobContext) -> None:
        await gate.wait()

    async def quick(ctx: JobContext) -> None:
        done.append(ctx.job_id)

    store.add(JobKind.ENROLMENT_RECHECK, 3)
    (fast,) = store.add(JobKind.PURGE)
    rt = _rt(store)
    rt.register_handler(
        _spec(JobKind.ENROLMENT_RECHECK, stuck, pool="ws", budget_s=30, lease_s=60)
    )
    rt.register_handler(_spec(JobKind.PURGE, quick))
    await rt.claim_once()
    await asyncio.sleep(0.05)
    assert fast in done, "maintenance ran while the ws pool was stuck"
    assert rt.free_slots("ws") == 0
    gate.set()
    await rt.wait_idle()


async def test_failure_goes_back_to_ready_with_backoff_and_a_fixed_error_code() -> None:
    store = MemStore()

    async def boom(ctx: JobContext) -> None:
        raise ValueError("student 4711 wrote something private")

    (jid,) = store.add(JobKind.PURGE)
    rt = _rt(store)
    rt.register_handler(_spec(JobKind.PURGE, boom))
    await rt.claim_once()
    await rt.wait_idle()
    row = store.rows[jid]
    assert (row["state"], row["attempts"], row["code"]) == ("ready", 1, "handler_error")
    assert 2.5 <= row["backoff"] <= 5.0  # first attempt: base 5 s, jitter 0.5-1.0
    assert "4711" not in repr(row), "exception text must never reach the job row"
    assert rt.counters[("failed", JobKind.PURGE)] == 1


async def test_timeout_is_a_failure_with_the_timeout_code() -> None:
    store = MemStore()

    async def hang(ctx: JobContext) -> None:
        await asyncio.sleep(10)

    (jid,) = store.add(JobKind.PURGE)
    rt = _rt(store)
    rt.register_handler(_spec(JobKind.PURGE, hang, budget_s=0.05, lease_s=0.1))
    await rt.claim_once()
    await rt.wait_idle()
    assert (store.rows[jid]["state"], store.rows[jid]["code"]) == ("ready", "timeout")


async def test_last_attempt_writes_audit_then_marks_dead() -> None:
    store, audit = MemStore(), Audit()

    async def boom(ctx: JobContext) -> None:
        raise RuntimeError("x")

    (jid,) = store.add(JobKind.PURGE, max_attempts=1)
    rt = _rt(store, audit)
    rt.register_handler(_spec(JobKind.PURGE, boom))
    await rt.claim_once()
    await rt.wait_idle()
    assert store.rows[jid]["state"] == "dead"
    assert audit.events == [
        (
            "assess_job_dead",
            {
                "job_id": jid,
                "kind": "purge",
                "attempts": 1,
                "error_code": "handler_error",
            },
        )
    ]
    assert rt.counters[("dead", JobKind.PURGE)] == 1


async def test_audit_failure_leaves_the_job_leased_and_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = MemStore()

    async def boom(ctx: JobContext) -> None:
        raise RuntimeError("x")

    (jid,) = store.add(JobKind.PURGE, max_attempts=1)
    rt = _rt(store, Audit(fail=True))
    rt.register_handler(_spec(JobKind.PURGE, boom))
    with caplog.at_level(logging.WARNING, logger="vektra_assess.worker"):
        await rt.claim_once()
        await rt.wait_idle()
    assert store.rows[jid]["state"] == "leased", "no audit row, so no dead job"
    assert rt.counters[("dead", JobKind.PURGE)] == 0
    assert "assess_job_settle_failed" in caplog.text
    assert rt.free_slots("maintenance") == 2, (
        "the slot is released even when settling failed"
    )


async def test_a_fence_lost_completion_is_counted_and_changes_nothing() -> None:
    store = MemStore()
    jid = store.add(JobKind.PURGE)[0]

    async def steal(ctx: JobContext) -> None:
        store.rows[jid]["owner"] = "someone-else"  # the lease was reclaimed meanwhile

    rt = _rt(store)
    rt.register_handler(_spec(JobKind.PURGE, steal))
    await rt.claim_once()
    await rt.wait_idle()
    assert (
        store.rows[jid]["state"] == "leased"
        and store.rows[jid]["owner"] == "someone-else"
    )
    assert rt.counters[("fence_lost", JobKind.PURGE)] == 1
    assert rt.counters[("completed", JobKind.PURGE)] == 0


async def test_unbound_handler_fails_the_job_closed() -> None:
    store = MemStore()
    (jid,) = store.add(JobKind.PURGE)
    rt = _rt(store)
    rt.register_handler(HandlerSpec(JobKind.PURGE, "maintenance", 1, 2))
    await rt.claim_once()
    await rt.wait_idle()
    assert store.rows[jid]["code"] == "handler_not_bound"


async def test_run_loop_picks_up_work_on_wake_and_stops() -> None:
    store, done = MemStore(), []

    async def ok(ctx: JobContext) -> None:
        done.append(ctx.job_id)

    rt = _rt(store, poll_interval_s=30.0)  # only wake() can make it fast
    rt.register_handler(_spec(JobKind.PURGE, ok))
    task = asyncio.create_task(rt.run())
    await asyncio.sleep(0.05)
    store.add(JobKind.PURGE)
    rt.wake()
    await asyncio.sleep(0.1)
    assert len(done) == 1
    await rt.stop()
    await asyncio.wait_for(task, timeout=1)
    await rt.wait_idle()


def test_registration_rules_and_backoff_shape() -> None:
    rt = _rt(MemStore())
    rt.register_handler(HandlerSpec(JobKind.PURGE, "maintenance", 1, 2))
    with pytest.raises(ValueError, match="already registered"):
        rt.register_handler(HandlerSpec(JobKind.PURGE, "maintenance", 1, 2))
    with pytest.raises(ValueError, match="unknown pool"):
        rt.register_handler(HandlerSpec(JobKind.ERASURE, "nope", 1, 2))
    with pytest.raises(ValueError, match="concurrency"):
        PoolSpec("p", 0)
    rng = random.Random(7)
    steps = [backoff_s(n, base_s=5, cap_s=40, rng=rng) for n in (1, 2, 3, 4, 5, 9)]
    assert all(2.5 <= steps[0] <= 5 for _ in [0])
    assert max(steps) <= 40 and steps[-1] >= 20, (
        "capped at 40, jitter keeps at least half"
    )


async def test_runtime_stats_carry_the_counters_the_plan_lists() -> None:
    store = MemStore()

    async def ok(ctx: JobContext) -> None: ...

    async def boom(ctx: JobContext) -> None:
        raise RuntimeError("x")

    store.add(JobKind.PURGE)
    store.add(JobKind.SETUP_CHECK, max_attempts=1)
    rt = _rt(store, pools=[PoolSpec("maintenance", 2)])
    rt.register_handler(_spec(JobKind.PURGE, ok))
    rt.register_handler(_spec(JobKind.SETUP_CHECK, boom))
    await rt.claim_once()
    await rt.wait_idle()
    stats = rt.stats()
    assert stats["pools_in_use"] == {"maintenance": 0}
    assert {
        "claimed:purge",
        "completed:purge",
        "claimed:setup_check",
        "dead:setup_check",
    } <= set(stats["jobs"])


async def test_drain_waits_for_quick_jobs_and_releases_the_stuck_ones() -> None:
    store, gate = MemStore(), asyncio.Event()
    quick_done: list[int] = []

    async def stuck(ctx: JobContext) -> None:
        await gate.wait()  # never set: only cancellation ends it

    async def quick(ctx: JobContext) -> None:
        await asyncio.sleep(0.05)
        quick_done.append(ctx.job_id)

    stuck_ids = store.add(JobKind.PURGE, 2)
    (q,) = store.add(JobKind.SETUP_CHECK)
    rt = _rt(store, pools=[PoolSpec("maintenance", 3)])
    rt.register_handler(_spec(JobKind.PURGE, stuck, budget_s=60, lease_s=120))
    rt.register_handler(_spec(JobKind.SETUP_CHECK, quick))
    await rt.claim_once()
    released = await rt.drain(timeout_s=0.5)
    assert quick_done == [q] and store.rows[q]["state"] == "done"
    assert released == 2
    for jid in stuck_ids:
        assert (
            store.rows[jid]["state"],
            store.rows[jid]["owner"],
            store.rows[jid]["attempts"],
        ) == ("ready", None, 0)
    assert rt.in_flight == 0 and rt.free_slots("maintenance") == 3
    assert rt.counters[("released", JobKind.PURGE)] == 2


async def test_drain_with_nothing_in_flight_is_a_no_op() -> None:
    assert await _rt(MemStore()).drain(0.1) == 0


async def test_a_job_that_finished_while_cancelling_is_not_counted_as_released() -> (
    None
):
    store = MemStore()
    (jid,) = store.add(JobKind.PURGE)

    async def finishes_late(ctx: JobContext) -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            store.rows[jid]["state"] = (
                "done"  # it completed elsewhere before the release
            )
            raise

    rt = _rt(store, pools=[PoolSpec("maintenance", 1)])
    rt.register_handler(_spec(JobKind.PURGE, finishes_late, budget_s=60, lease_s=120))
    await rt.claim_once()
    assert await rt.drain(timeout_s=0.05) == 0
    assert store.rows[jid]["state"] == "done"


async def test_run_loop_survives_database_errors_and_keeps_claiming() -> None:
    class Flaky(MemStore):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next = 3

        async def claim(self, kinds, worker_id, lease_s, free, max_attempts=None):  # type: ignore[no-untyped-def]
            if self.fail_next:
                self.fail_next -= 1
                raise ConnectionError("student 4711: the server closed the connection")
            return await super().claim(kinds, worker_id, lease_s, free, max_attempts)

    store, done = Flaky(), []

    async def ok(ctx: JobContext) -> None:
        done.append(ctx.job_id)

    store.add(JobKind.PURGE)
    rt = _rt(store, poll_interval_s=0.02)
    rt.register_handler(_spec(JobKind.PURGE, ok))
    task = asyncio.create_task(rt.run())
    await asyncio.sleep(0.3)
    await rt.stop()
    await asyncio.wait_for(task, timeout=1)
    await rt.wait_idle()
    assert len(done) == 1, "the loop outlived three failed claims"
    assert rt.claim_errors == 3 and rt.stats()["claim_errors"] == 3


async def test_jobs_returned_by_a_claim_that_finishes_after_stop_are_released_not_started() -> (
    None
):
    """Review finding: the drain's snapshot would not know them, and they would stay leased."""
    gate = asyncio.Event()

    class Slow(MemStore):
        async def claim(self, kinds, worker_id, lease_s, free, max_attempts=None):  # type: ignore[no-untyped-def]
            await gate.wait()  # the claim is in flight while shutdown starts
            return await super().claim(kinds, worker_id, lease_s, free, max_attempts)

    store, ran = Slow(), []

    async def ok(ctx: JobContext) -> None:
        ran.append(ctx.job_id)

    (jid,) = store.add(JobKind.PURGE)
    rt = _rt(store)
    rt.register_handler(_spec(JobKind.PURGE, ok))
    claim = asyncio.create_task(rt.claim_once())
    await asyncio.sleep(0.02)
    await rt.stop()
    gate.set()
    assert await claim == 0
    assert ran == [] and rt.in_flight == 0
    assert store.rows[jid]["state"] == "ready", "returned to ready, not left leased"
    assert await rt.claim_once() == 0, "nothing new starts once stopping"


async def test_a_failing_pool_does_not_stop_the_pools_after_it() -> None:
    store = MemStore()
    real_claim = store.claim

    async def claim(kinds: Any, *a: Any, **k: Any) -> Any:
        if JobKind.PURGE in kinds:
            raise RuntimeError("statement timeout")
        return await real_claim(kinds, *a, **k)

    store.claim = claim  # type: ignore[method-assign]
    (q,) = store.add(JobKind.SETUP_CHECK)
    store.add(JobKind.PURGE)
    ran: list[int] = []

    async def ok(ctx: JobContext) -> None:
        ran.append(ctx.job_id)

    rt = _rt(store, pools=[PoolSpec("maintenance", 2), PoolSpec("setup", 2)])
    rt.register_handler(_spec(JobKind.PURGE, ok))
    rt.register_handler(_spec(JobKind.SETUP_CHECK, ok, pool="setup"))
    started = await rt.claim_once()
    await rt.drain(timeout_s=1)
    assert rt.claim_errors == 1 and q in ran
    assert started == 0, "a pass with a failed pool reports 0 so run() backs off"


async def test_drain_releases_each_lease_even_when_one_handler_will_not_cancel() -> (
    None
):
    store = MemStore()
    stubborn_release = asyncio.Event()

    async def stubborn(ctx: JobContext) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            await stubborn_release.wait()  # cleanup that never finishes

    async def polite(ctx: JobContext) -> None:
        await asyncio.Event().wait()

    (s_id,) = store.add(JobKind.PURGE)
    (p_id,) = store.add(JobKind.SETUP_CHECK)
    rt = _rt(store, pools=[PoolSpec("maintenance", 2)])
    rt.register_handler(_spec(JobKind.PURGE, stubborn, budget_s=60, lease_s=120))
    rt.register_handler(_spec(JobKind.SETUP_CHECK, polite))
    await rt.claim_once()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):
            await rt.drain(timeout_s=0.05)
    assert store.rows[p_id]["state"] == "ready"  # released although the other task hung
    assert store.rows[s_id]["state"] == "leased"
    stubborn_release.set()
