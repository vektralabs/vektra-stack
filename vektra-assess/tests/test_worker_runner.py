"""Worker contracts (plan assess-job-loop B1): validation, fail-closed unbound slots."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import pytest

from vektra_assess.jobs import JobKind
from vektra_assess.worker.runner import (
    HandlerNotBound,
    HandlerSpec,
    JobContext,
    JobRunner,
    TickSpec,
)


@asynccontextmanager
async def _no_db():  # type: ignore[no-untyped-def]
    raise AssertionError("not used")
    yield


def _ctx() -> JobContext:
    return JobContext(
        job_id=1,
        kind=JobKind.PURGE,
        args={"k": 1},
        attempt=1,
        acquire=_no_db,
        log=logging.getLogger("t"),
    )


def test_lease_must_be_at_least_twice_the_budget() -> None:
    HandlerSpec(JobKind.PURGE, "maintenance", budget_s=60, lease_s=120)
    with pytest.raises(ValueError, match="at least 2 x budget_s"):
        HandlerSpec(JobKind.PURGE, "maintenance", budget_s=60, lease_s=119)
    with pytest.raises(ValueError, match="budget_s must be positive"):
        HandlerSpec(JobKind.PURGE, "maintenance", budget_s=0, lease_s=10)
    with pytest.raises(ValueError, match="max_attempts"):
        HandlerSpec(JobKind.PURGE, "maintenance", 1, 2, max_attempts=0)


async def test_unbound_handler_fails_closed_and_bound_one_runs() -> None:
    spec = HandlerSpec(JobKind.PURGE, "maintenance", budget_s=1, lease_s=2)
    assert not spec.bound
    with pytest.raises(HandlerNotBound, match="purge"):
        await spec.run(_ctx())

    seen: list[int] = []

    async def body(ctx: JobContext) -> None:
        seen.append(ctx.job_id)

    bound = HandlerSpec(JobKind.PURGE, "maintenance", 1, 2, handle=body)
    assert bound.bound
    await bound.run(_ctx())
    assert seen == [1]


async def test_tick_validation_and_unbound_tick_fail_closed() -> None:
    with pytest.raises(ValueError, match="jitter_s"):
        TickSpec("t", interval_s=5, timeout_s=1, lock_key=1, jitter_s=5)
    with pytest.raises(ValueError, match="positive"):
        TickSpec("t", interval_s=0, timeout_s=1, lock_key=1)
    tick = TickSpec("heartbeat", interval_s=5, timeout_s=2, lock_key=7)
    assert not tick.bound
    with pytest.raises(HandlerNotBound, match="heartbeat"):
        await tick.run()


def test_job_context_is_frozen_and_carries_ids_only() -> None:
    ctx = _ctx()
    with pytest.raises(AttributeError):
        ctx.job_id = 2  # type: ignore[misc]
    assert set(JobContext.__slots__) == {
        "job_id", "kind", "args", "attempt", "acquire", "log",
    }  # fmt: skip


def test_runner_protocol_is_runtime_checkable() -> None:
    class Fake:
        def register_handler(self, spec: HandlerSpec) -> None: ...
        def register_tick(self, spec: TickSpec) -> None: ...
        async def run(self) -> None: ...
        async def stop(self) -> None: ...

    assert isinstance(Fake(), JobRunner)
    assert not isinstance(object(), JobRunner)
