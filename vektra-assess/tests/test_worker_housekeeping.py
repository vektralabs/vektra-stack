"""Heartbeat file ordering and cleanup batching without a database (plan B, housekeeping)."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from vektra_assess.worker.cleanup import cleanup_done_jobs
from vektra_assess.worker.heartbeat import UPSERT, Heartbeat


class _Conn:
    def __init__(self, deletes: list[int] | None = None) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self._deletes = list(deletes or [])

    async def fetch(self, q: str, *a: Any) -> Sequence[Mapping[str, Any]]:
        return []

    async def fetchval(self, q: str, *a: Any) -> Any:
        return 4.2

    async def execute(self, q: str, *a: Any) -> str:
        self.executed.append((q, a))
        return f"DELETE {self._deletes.pop(0)}" if self._deletes else "UPDATE 1"


async def test_the_file_is_touched_even_when_the_database_is_down(
    tmp_path: Path,
) -> None:
    f = tmp_path / "run" / "heartbeat"  # parent created on demand (tmpfs mount point)

    @asynccontextmanager
    async def broken():  # type: ignore[no-untyped-def]
        raise ConnectionError("db down")
        yield

    hb = Heartbeat(broken, worker_id="w", version="1", file_path=f)
    with pytest.raises(ConnectionError):
        await hb.beat()
    assert f.exists(), "the loop-alive evidence must not depend on the database"


async def test_beat_publishes_state_age_and_stats_without_content(
    tmp_path: Path,
) -> None:
    conn = _Conn()
    f = tmp_path / "hb"

    @asynccontextmanager
    async def acquire():  # type: ignore[no-untyped-def]
        yield conn

    hb = Heartbeat(
        acquire,
        worker_id="w1",
        version="0.9",
        file_path=f,
        stats=lambda: {"pools_in_use": {"ws": 1}, "jobs": {"claimed:purge": 3}},
    )
    old = time.time() - 3600
    os.utime(tmp_path, None)
    f.touch()
    os.utime(f, (old, old))
    hb.set_state("ready")
    await hb.beat()
    assert f.stat().st_mtime > old + 3000, "mtime advanced"
    (sql, (payload,)) = conn.executed[0]
    assert sql == UPSERT
    import json

    d = json.loads(payload)
    assert (
        d["state"] == "ready"
        and d["worker_id"] == "w1"
        and d["oldest_ready_age_s"] == 4.2
    )
    assert d["pools_in_use"] == {"ws": 1} and d["jobs"] == {"claimed:purge": 3}
    assert set(d) == {
        "worker_id", "started_at", "version", "state", "oldest_ready_age_s", "pools_in_use", "jobs",
    }  # fmt: skip


async def test_an_unwritable_file_does_not_stop_the_row_beat(tmp_path: Path) -> None:
    conn = _Conn()

    @asynccontextmanager
    async def acquire():  # type: ignore[no-untyped-def]
        yield conn

    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory is needed")
    hb = Heartbeat(acquire, worker_id="w", version="1", file_path=blocker / "hb")
    await hb.beat()
    assert len(conn.executed) == 1


async def test_cleanup_stops_after_a_short_batch_and_caps_the_run() -> None:
    @asynccontextmanager
    async def acquire_for(deletes: list[int]):  # type: ignore[no-untyped-def]
        yield _Conn(deletes)

    calls = {"n": 0}
    plan = [1000, 1000, 250]

    @asynccontextmanager
    async def acquire():  # type: ignore[no-untyped-def]
        calls["n"] += 1
        yield _Conn([plan.pop(0)])

    assert await cleanup_done_jobs(acquire, 24, batch=1000) == 2250
    assert calls["n"] == 3

    calls["n"] = 0

    @asynccontextmanager
    async def endless():  # type: ignore[no-untyped-def]
        calls["n"] += 1
        yield _Conn([1000])

    assert await cleanup_done_jobs(endless, 24, batch=1000, max_batches=5) == 5000
    assert calls["n"] == 5, "a run never outlasts its tick: the next hour continues"
