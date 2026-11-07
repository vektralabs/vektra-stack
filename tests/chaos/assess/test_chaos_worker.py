"""Chaos on the real worker process: kill -9 mid-handler and SIGTERM with in-flight jobs.

The assertions are row counts: jobs done = N, effects = N, no leased row with an expired lease,
no dead job. The worker is a subprocess started the way the deployment starts it; only the
``purge`` body is the chaos body (see ``worker_main.py``).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from harness import (
    counts,
    enqueue,
    reset_tables,
    start_worker,
    wait_until,
    worker_ready,
    worker_secrets,
)

pytestmark = pytest.mark.integration

N = 12


async def test_kill9_mid_handler_then_restart_completes_every_job_exactly_once(
    pg, tmp_path: Path
):
    await reset_tables(pg)
    secrets = worker_secrets(tmp_path / "secrets")
    await enqueue(pg, N, sleep_ms=1500)
    a = start_worker(pg, tmp_path, "a", secrets, chaos_dir=tmp_path / "chaos-a")
    b = None
    try:
        await wait_until(lambda: worker_ready(pg), 90, "worker a ready", a)
        await wait_until(
            lambda: _async(len(a.started()) >= 4), 30, "four handlers in flight", a
        )
        leased_before = (await counts(pg))["leased"]
        assert leased_before >= 4, "jobs are mid-handler at the moment of the kill"
        a.kill9()
        mid = await counts(pg)
        assert mid["leased"] >= 4, "kill -9 released nothing: the leases are still held"

        b = start_worker(pg, tmp_path, "b", secrets, chaos_dir=tmp_path / "chaos-b")
        await wait_until(lambda: worker_ready(pg), 90, "worker b ready", b)

        async def all_done() -> bool:
            return (await counts(pg))["done"] == N

        await wait_until(all_done, 60, f"all {N} jobs done after lease expiry", b)
        c = await counts(pg)
        assert (
            c["done"] == N and c["dead"] == 0 and c["ready"] == 0 and c["leased"] == 0
        )
        assert (c["effects"], c["effects_distinct"]) == (N, N), (
            "every job's effect exactly once"
        )
        assert c["expired_leases"] == 0
        reclaimed = (
            await pg.run("super", "SELECT count(*) FROM assess_jobs WHERE attempts = 2")
        )[0][0]
        assert reclaimed >= 1, "at least one job was re-run after its lease expired"
    finally:
        a.stop()
        if b is not None:
            b.stop()


async def test_sigterm_with_in_flight_jobs_exits_zero_releases_and_a_second_worker_finishes(
    pg, tmp_path: Path
):
    await reset_tables(pg)
    secrets = worker_secrets(tmp_path / "secrets")
    # long handlers: still running when SIGTERM arrives, longer than the 2 s drain window
    await enqueue(pg, 6, sleep_ms=20000)
    a = start_worker(
        pg,
        tmp_path,
        "a",
        secrets,
        chaos_dir=tmp_path / "chaos-a",
        VEKTRA_ASSESS_WORKER_DRAIN_S="2",
        VEKTRA_ASSESS_JOB_PURGE_BUDGET_S="30",
        VEKTRA_ASSESS_JOB_PURGE_LEASE_S="60",
    )
    b = None
    try:
        await wait_until(lambda: worker_ready(pg), 90, "worker a ready", a)
        await wait_until(
            lambda: _async(len(a.started()) >= 6), 30, "six handlers in flight", a
        )
        t0 = time.monotonic()
        code = a.sigterm(timeout_s=30)
        assert code == 0, a.output()
        assert time.monotonic() - t0 < 30, "inside the 30 s stop_grace_period"

        released = await pg.run(
            "super",
            "SELECT state, attempts, lease_until IS NULL, leased_by IS NULL FROM assess_jobs",
        )
        assert [tuple(r) for r in released] == [("ready", 0, True, True)] * 6, released
        hb = (await pg.run("super", "SELECT details->>'state' FROM assess_heartbeat"))[
            0
        ][0]
        assert hb == "stopped"

        # the same jobs, now quick: a second worker takes them at once, no lease wait
        await pg.run(
            "super", "UPDATE assess_jobs SET args = jsonb_set(args, '{sleep_ms}', '50')"
        )
        b = start_worker(pg, tmp_path, "b", secrets, chaos_dir=tmp_path / "chaos-b")
        await wait_until(lambda: worker_ready(pg), 90, "worker b ready", b)

        async def done() -> bool:
            return (await counts(pg))["done"] == 6

        await wait_until(done, 20, "second worker completes the released jobs", b)
        c = await counts(pg)
        assert (
            c["effects"],
            c["effects_distinct"],
            c["dead"],
            c["expired_leases"],
        ) == (6, 6, 0, 0)
    finally:
        a.stop()
        if b is not None:
            b.stop()


async def _async(value: bool) -> bool:
    return value
