"""Half-open database connections: the failure a restart or a dropped network really leaves.

A blackholing proxy keeps every socket open and answers nothing. Without timeouts the control
connection's lock call and every pool query wait forever (found by the container-restart scenario:
the worker stayed alive but did nothing, 90 s after Postgres came back). With them each call fails
inside its bound, the dead connection is discarded, and the next call uses a fresh one.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from harness import Proxy
from sqlalchemy.engine import make_url

from vektra_assess.worker.listener import ControlConnection, ControlConnectionError

pytestmark = pytest.mark.integration


def _via(pg, port: int, driver: str) -> str:
    u = make_url(pg.url("worker"))
    return u.set(drivername=driver, host="127.0.0.1", port=port).render_as_string(
        hide_password=False
    )


async def _proxy(pg) -> Proxy:
    u = make_url(pg.super_url)
    p = Proxy(u.host or "127.0.0.1", int(u.port or 5432))
    await p.start()
    return p


async def test_control_connection_gives_up_on_a_half_open_socket_and_reconnects(pg):
    proxy = await _proxy(pg)
    lost = []
    conn = ControlConnection(
        _via(pg, proxy.port, "postgresql"),
        on_notify=lambda: None,
        on_lost=lambda: lost.append(1),
        connect_timeout_s=1.0,
        command_timeout_s=0.5,
        reconnect_base_s=0.1,
        reconnect_cap_s=0.3,
    )
    await conn.start()
    sup = asyncio.create_task(conn.supervise())
    try:
        assert await conn.try_lock(1) is True
        proxy.blackhole = True
        t0 = time.monotonic()
        with pytest.raises(ControlConnectionError, match="control connection lost"):
            await conn.try_lock(2)
        assert time.monotonic() - t0 < 3, "the call is bounded by the command timeout"
        assert lost == [1] and conn.held_locks == frozenset()
        assert not conn.connected

        proxy.blackhole = False
        async with asyncio.timeout(15):
            while not (conn.connected and conn.reconnects >= 1):
                await asyncio.sleep(0.1)
        assert await conn.try_lock(3) is True, "a fresh session serves locks again"
    finally:
        await conn.stop()
        await asyncio.wait_for(sup, timeout=3)
        await proxy.stop()


async def test_worker_process_behind_a_half_open_link_exits_70_and_a_restart_recovers(
    pg, tmp_path: Path
):
    """The backstop, end to end: blackhole the link under a running worker process.

    Its heartbeat can no longer be written, so after the wedge window it exits 70 (a supervisor
    would restart it); a fresh process on a healthy link reaches ``ready`` and works again.
    """
    from harness import (
        reset_tables,
        start_worker,
        wait_until,
        worker_ready,
        worker_secrets,
    )

    await reset_tables(pg)
    secrets = worker_secrets(tmp_path / "secrets")
    proxy = await _proxy(pg)

    class Via:  # the harness builds the worker DSN from pg.super_url: point it at the proxy
        super_url = (
            make_url(pg.super_url)
            .set(host="127.0.0.1", port=proxy.port)
            .render_as_string(hide_password=False)
        )

    w = start_worker(
        Via,
        tmp_path,
        "a",
        secrets,
        chaos_dir=tmp_path / "chaos",
        VEKTRA_ASSESS_WORKER_WEDGE_EXIT_S="10",
    )
    w2 = None
    try:
        await wait_until(
            lambda: worker_ready(pg), 90, "worker ready through the proxy", w
        )
        proxy.blackhole = True
        t0 = time.monotonic()
        code = await asyncio.to_thread(w.proc.wait, 60)
        assert code == 70, w.output()
        assert time.monotonic() - t0 < 40, (
            "the exit is bounded by the wedge window plus slack"
        )
        proxy.blackhole = False
        await proxy.stop()

        await pg.run("super", "DELETE FROM assess_heartbeat")
        w2 = start_worker(pg, tmp_path, "b", secrets, chaos_dir=tmp_path / "chaos-b")
        await wait_until(lambda: worker_ready(pg), 90, "a fresh worker is ready", w2)
    finally:
        w.stop()
        if w2 is not None:
            w2.stop()
        await proxy.stop()
