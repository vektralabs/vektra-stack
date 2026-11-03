"""The control connection on a real Postgres (plan assess-job-loop B5).

NOTIFY delivery, the startup self-test, session advisory locks (exclusive across sessions,
released when the session dies) and reconnect after the backend is killed.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from vektra_assess.worker.listener import (
    APPLICATION_NAME,
    ControlConnection,
    ControlConnectionError,
)

pytestmark = pytest.mark.integration


def _dsn(pg) -> str:
    return (
        make_url(pg.url("worker"))
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )


class _Counter:
    def __init__(self) -> None:
        self.notified = 0
        self.lost = 0

    def notify(self) -> None:
        self.notified += 1

    def on_lost(self) -> None:
        self.lost += 1


async def _wait(cond, timeout_s: float = 5.0) -> None:
    async with asyncio.timeout(timeout_s):
        while not cond():
            await asyncio.sleep(0.02)


async def _backend_pid(pg) -> int:
    rows = await pg.run(
        "super",
        "SELECT pid FROM pg_stat_activity WHERE application_name = :a",
        {"a": APPLICATION_NAME},
    )
    assert len(rows) == 1, rows
    return int(rows[0][0])


async def test_start_runs_the_selftest_and_names_the_connection(pg):
    c = _Counter()
    conn = ControlConnection(_dsn(pg), on_notify=c.notify)
    await conn.start()
    try:
        assert conn.connected
        await _backend_pid(pg)  # exactly one backend with the control application_name
        assert c.notified == 0, "the self-test must not look like a job wake-up"
    finally:
        await conn.stop()


async def test_a_job_notify_wakes_the_listener(pg):
    c = _Counter()
    conn = ControlConnection(_dsn(pg), on_notify=c.notify)
    await conn.start()
    try:
        await pg.run("super", "SELECT pg_notify('assess_jobs', '')")
        await _wait(lambda: c.notified == 1)
    finally:
        await conn.stop()


async def test_selftest_failure_refuses_to_start_and_names_the_pooler(pg, monkeypatch):
    conn = ControlConnection(_dsn(pg), on_notify=lambda: None, selftest_timeout_s=0.3)

    async def silent() -> None:  # a pooler that eats the NOTIFY
        return None

    monkeypatch.setattr(conn, "_send_probe_notify", silent)
    try:
        with pytest.raises(ControlConnectionError, match="transaction-mode pooler"):
            await conn.start()
    finally:
        await conn.stop()


async def test_unreachable_database_is_a_control_error(pg):
    bad = _dsn(pg).replace("@", "@nonexistent-host.invalid:1/x#", 1)  # never resolves
    conn = ControlConnection(bad, on_notify=lambda: None, connect_timeout_s=0.5)
    with pytest.raises(ControlConnectionError, match="control connection failed"):
        await conn.start()


async def test_advisory_locks_are_exclusive_across_sessions_and_released(pg):
    a = ControlConnection(_dsn(pg), on_notify=lambda: None)
    other = await asyncpg.connect(_dsn(pg))
    await a.start()
    try:
        assert await a.try_lock(4242) is True
        assert a.held_locks == {4242}
        assert await other.fetchval("SELECT pg_try_advisory_lock(4242)") is False
        await a.unlock(4242)
        assert a.held_locks == frozenset()
        assert await other.fetchval("SELECT pg_try_advisory_lock(4242)") is True
        await other.fetchval("SELECT pg_advisory_unlock(4242)")
    finally:
        await a.stop()
        await other.close()


async def test_lost_connection_releases_locks_calls_back_and_reconnects(pg):
    c = _Counter()
    conn = ControlConnection(
        _dsn(pg),
        on_notify=c.notify,
        on_lost=c.on_lost,
        reconnect_base_s=0.05,
        reconnect_cap_s=0.1,
    )
    other = await asyncpg.connect(_dsn(pg))
    await conn.start()
    sup = asyncio.create_task(conn.supervise())
    try:
        assert await conn.try_lock(777) is True
        assert await other.fetchval("SELECT pg_try_advisory_lock(777)") is False
        await pg.run(
            "super", "SELECT pg_terminate_backend(:p)", {"p": await _backend_pid(pg)}
        )
        await _wait(lambda: c.lost == 1)
        assert conn.held_locks == frozenset(), (
            "the tick it protected is no longer fenced"
        )
        # the server released the session lock: someone else can take it now
        assert await other.fetchval("SELECT pg_try_advisory_lock(777)") is True
        await other.fetchval("SELECT pg_advisory_unlock(777)")
        await _wait(lambda: conn.connected and conn.reconnects == 1)
        before = c.notified
        await pg.run("super", "SELECT pg_notify('assess_jobs', '')")
        await _wait(lambda: c.notified > before)  # listening again after the reconnect
        assert await conn.try_lock(777) is True
    finally:
        await conn.stop()
        await asyncio.wait_for(sup, timeout=2)
        await other.close()
