"""Ticks on the real control connection (plan assess-job-loop B6).

Two schedulers (two worker processes) contend for one tick: the advisory lock lets only one run
it. Killing the control backend cancels the running tick, because its lock died with the session.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from vektra_assess.worker import locks
from vektra_assess.worker.listener import APPLICATION_NAME, ControlConnection
from vektra_assess.worker.runner import TickSpec
from vektra_assess.worker.ticks import TickScheduler

pytestmark = pytest.mark.integration


def _dsn(pg) -> str:
    return (
        make_url(pg.url("worker"))
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )


async def test_only_one_process_runs_a_tick_at_a_time(pg):
    a = ControlConnection(_dsn(pg), on_notify=lambda: None)
    b = ControlConnection(_dsn(pg), on_notify=lambda: None)
    await a.start()
    await b.start()
    inside, peak, runs = [0], [0], [0]

    async def body() -> None:
        inside[0] += 1
        peak[0] = max(peak[0], inside[0])
        runs[0] += 1
        await asyncio.sleep(0.12)
        inside[0] -= 1

    def spec() -> TickSpec:
        return TickSpec(
            "t", interval_s=0.03, timeout_s=2, lock_key=locks.SWEEPER, body=body
        )

    sa, sb = TickScheduler(a, [spec()]), TickScheduler(b, [spec()])
    try:
        sa.start()
        sb.start()
        await asyncio.sleep(0.8)
    finally:
        await sa.stop()
        await sb.stop()
        await a.stop()
        await b.stop()
    assert runs[0] >= 3
    assert peak[0] == 1, "two processes ran the same tick at once"
    skipped = (
        sa.counters[("t", "skipped_locked")] + sb.counters[("t", "skipped_locked")]
    )
    assert skipped >= 1


async def test_losing_the_control_session_cancels_the_running_tick(pg):
    cancelled = asyncio.Event()
    holder: list[TickScheduler] = []

    async def long() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    conn = ControlConnection(
        _dsn(pg),
        on_notify=lambda: None,
        on_lost=lambda: holder[0].cancel_running(),
        reconnect_base_s=0.05,
        reconnect_cap_s=0.1,
    )
    await conn.start()
    sch = TickScheduler(
        conn,
        [TickSpec("t", interval_s=0.05, timeout_s=60, lock_key=locks.PURGE, body=long)],
    )
    holder.append(sch)
    sup = asyncio.create_task(conn.supervise())
    try:
        sch.start()
        async with asyncio.timeout(5):
            while "t" not in sch.running:
                await asyncio.sleep(0.02)
        other = await asyncpg.connect(_dsn(pg))
        try:
            assert (
                await other.fetchval(f"SELECT pg_try_advisory_lock({locks.PURGE})")
                is False
            )
            pid = (
                await pg.run(
                    "super",
                    "SELECT pid FROM pg_stat_activity WHERE application_name = :a",
                    {"a": APPLICATION_NAME},
                )
            )[0][0]
            await pg.run("super", "SELECT pg_terminate_backend(:p)", {"p": pid})
            await asyncio.wait_for(cancelled.wait(), timeout=5)
            assert (
                await other.fetchval(f"SELECT pg_try_advisory_lock({locks.PURGE})")
                is True
            )
            await other.fetchval(f"SELECT pg_advisory_unlock({locks.PURGE})")
        finally:
            await other.close()
        # after the reconnect the tick starts again on its next schedule
        async with asyncio.timeout(5):
            while "t" not in sch.running:
                await asyncio.sleep(0.02)
    finally:
        await sch.stop()
        await conn.stop()
        await asyncio.wait_for(sup, timeout=2)
