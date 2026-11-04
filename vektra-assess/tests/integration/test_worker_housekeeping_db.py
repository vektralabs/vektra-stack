"""Heartbeat row and done-job cleanup on a real Postgres (plan assess-job-loop).

The worker role writes the row; readiness reads it as the API role, which has no access to
``assess_jobs``, so the oldest-ready-job age must travel in the row.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from vektra_assess.health import make_readiness_check
from vektra_assess.worker.cleanup import cleanup_done_jobs
from vektra_assess.worker.heartbeat import Heartbeat

pytestmark = pytest.mark.integration


def _dsn(pg) -> str:
    return (
        make_url(pg.url("worker"))
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )


def _acquire(pg):
    @asynccontextmanager
    async def acquire():
        c = await asyncpg.connect(_dsn(pg))
        try:
            yield c
        finally:
            await c.close()

    return acquire


async def test_readiness_follows_the_heartbeat_state_and_the_published_backlog(
    pg, tmp_path: Path
):
    await pg.run("super", "DELETE FROM assess_heartbeat")
    await pg.run("super", "DELETE FROM assess_jobs")
    hb = Heartbeat(_acquire(pg), worker_id="w1", version="t", file_path=tmp_path / "hb")
    engine = create_async_engine(pg.url("api"))
    try:
        check = make_readiness_check(
            enabled=True, get_engine=lambda: engine, max_age_s=60
        )
        await hb.beat()  # state=starting: reconciliation has not finished
        assert (await check()).message == "worker_not_ready"
        hb.set_state("ready")
        await hb.beat()
        assert (await check()).status == "healthy"
        # an old ready job: the worker publishes its age, the API cannot read the table itself
        await pg.run(
            "super",
            "INSERT INTO assess_jobs (kind, not_before) VALUES ('purge', now() - interval '2 minutes')",
        )
        await hb.beat()
        assert (await check()).message == "job_backlog"
        await pg.run("super", "DELETE FROM assess_jobs")
        await hb.beat()
        assert (await check()).status == "healthy"
        hb.set_state("stopped")
        await hb.beat()
        assert (await check()).message == "worker_not_ready", (
            "a stopping worker drops readiness at once"
        )
        row = (
            await pg.run(
                "super",
                "SELECT count(*), max(details->>'worker_id') FROM assess_heartbeat",
            )
        )[0]
        assert tuple(row) == (1, "w1"), "one row per component, upserted"
    finally:
        await engine.dispose()


async def test_cleanup_deletes_old_done_rows_in_batches_and_keeps_dead_and_recent(pg):
    await pg.run("super", "DELETE FROM assess_jobs")
    for _ in range(7):
        await pg.run(
            "super",
            "INSERT INTO assess_jobs (kind, state, done_at) VALUES ('purge','done', now() - interval '48 hours')",
        )
    for state, age in (("done", "1 hour"), ("dead", "48 hours"), ("ready", "48 hours")):
        await pg.run(
            "super",
            f"INSERT INTO assess_jobs (kind, state, done_at) VALUES ('purge', :s, now() - interval '{age}')",
            {"s": state},
        )
    assert await cleanup_done_jobs(_acquire(pg), 24, batch=3) == 7
    left = await pg.run(
        "super", "SELECT state, count(*) FROM assess_jobs GROUP BY 1 ORDER BY 1"
    )
    assert [tuple(r) for r in left] == [("dead", 1), ("done", 1), ("ready", 1)]
