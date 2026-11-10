"""Chaos on the database under a running worker: container restart and backend termination.

The worker must reconnect without manual action (control connection, pool, claim loop), LISTEN
must be re-established, and every job must still end ``done`` with exactly one effect. Jobs that
were in flight when the connection died are settled by lease expiry and re-run: their effects are
idempotent (``runs`` > 1), never duplicated rows and never lost.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from harness import (
    Supervisor,
    counts,
    enqueue,
    reset_tables,
    start_worker,
    wait_until,
    worker_ready,
    worker_secrets,
)
from invariants import assert_invariants

pytestmark = pytest.mark.integration

N = 40


async def _done_at_least(pg, k: int) -> bool:
    return (await counts(pg))["done"] >= k


async def _postgres_answers(pg) -> bool:
    try:
        await pg.run("super", "SELECT 1")
        return True
    except Exception:
        return False


async def _assert_exactly_once(pg, *, what: str) -> None:
    c = await counts(pg)
    assert c["done"] == N, (what, c)
    assert (c["effects"], c["effects_distinct"]) == (N, N), (what, c)
    assert c["dead"] == 0 and c["ready"] == 0 and c["leased"] == 0, (what, c)
    assert c["expired_leases"] == 0, (what, c)


async def test_postgres_container_restart_under_load_recovers_without_manual_action(
    pg_fixed_port, tmp_path: Path
):
    """In-process reconnect where the sockets die cleanly; the watchdog restart where they go
    half-open (what Docker's port proxy leaves behind). Either way no manual action."""
    pg = pg_fixed_port
    import docker

    await reset_tables(pg)
    secrets = worker_secrets(tmp_path / "secrets")
    await enqueue(pg, N, sleep_ms=400)
    # a short wedge window keeps the half-open case inside the test's patience
    sup = Supervisor(pg, tmp_path, secrets, VEKTRA_ASSESS_WORKER_WEDGE_EXIT_S="10")
    try:
        await wait_until(lambda: worker_ready(pg), 90, "worker ready", sup.current)
        await wait_until(
            lambda: _done_at_least(pg, 3), 30, "load under way", sup.current
        )

        docker.from_env().containers.get(pg.container_id).restart(timeout=5)
        await wait_until(lambda: _postgres_answers(pg), 60, "postgres answers again")

        async def all_done() -> bool:
            sup.tick()  # the supervisor restarts a worker that exited non-zero
            return (await counts(pg))["done"] == N

        await wait_until(all_done, 120, f"all {N} jobs done after the restart")
        await _assert_exactly_once(pg, what="container restart")
        assert all(code == 70 for code in sup.exit_codes), (
            f"the only acceptable exit is the watchdog's: {sup.exit_codes}"
        )
        # whichever way it recovered, the worker is serving again
        await wait_until(lambda: worker_ready(pg), 60, "worker ready after recovery")
        control = (
            await pg.run(
                "super",
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE application_name = 'vektra-assess-worker-control'",
            )
        )[0][0]
        assert control == 1, "LISTEN is re-established: one control connection is back"
        print(f"CHAOS restart recovered with {sup.restarts} supervisor restart(s)")
        await assert_invariants(
            pg, "postgres-restart:job-load", expect_sessions=0, only=["no_stale_lease"]
        )
    finally:
        sup.stop()


async def test_terminating_the_worker_backends_repeatedly_under_load_never_loses_a_job(
    pg, tmp_path: Path
):
    await reset_tables(pg)
    secrets = worker_secrets(tmp_path / "secrets")
    await enqueue(pg, N, sleep_ms=400)
    w = start_worker(pg, tmp_path, "a", secrets, chaos_dir=tmp_path / "chaos")
    try:
        await wait_until(lambda: worker_ready(pg), 90, "worker ready", w)
        await wait_until(lambda: _done_at_least(pg, 2), 30, "load under way", w)
        killed = 0
        deadline = time.monotonic() + 40
        while (await counts(pg))["done"] < N and time.monotonic() < deadline:
            rows = await pg.run(
                "super",
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE usename = 'vektra_assess_worker' AND pid <> pg_backend_pid()",
            )
            killed += len(rows)
            await asyncio.sleep(1.5)
        assert killed >= 3, (
            f"the test must actually have cut connections (cut {killed})"
        )
        assert w.proc.poll() is None, f"the worker must survive: {w.output()}"

        async def all_done() -> bool:
            return (await counts(pg))["done"] == N

        await wait_until(all_done, 90, f"all {N} jobs done after repeated cuts", w)
        await _assert_exactly_once(pg, what="backend termination")
    finally:
        w.stop()
