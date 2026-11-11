"""The outbox against a real Postgres (plan assess-job-loop A4).

A NOTIFY sent inside a transaction that rolls back is never delivered and leaves no row; one
sent inside a transaction that commits is delivered exactly once with payload ''. Delivery
is observed on a separate asyncpg connection, not inferred from the helper's return value.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from vektra_assess.jobs import NOTIFY_CHANNEL, JobKind, enqueue_job

pytestmark = pytest.mark.integration


def _dsn(pg, who: str) -> str:
    return (
        make_url(pg.url(who))
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )


class _Listener:
    def __init__(self) -> None:
        self.payloads: list[str] = []
        self.conn: asyncpg.Connection | None = None

    async def start(self, dsn: str) -> None:
        self.conn = await asyncpg.connect(dsn)
        await self.conn.add_listener(
            NOTIFY_CHANNEL, lambda _c, _pid, _ch, payload: self.payloads.append(payload)
        )

    async def stop(self) -> None:
        assert self.conn is not None
        await self.conn.close()


@asynccontextmanager
async def _listening(pg):
    """Opened inside the test body so the callback runs on the test's own event loop."""
    lst = _Listener()
    await lst.start(_dsn(pg, "super"))
    try:
        yield lst
    finally:
        await lst.stop()


async def _settle() -> None:
    # delivery is asynchronous to the committing connection; a quiet 0.5 s is the check
    await asyncio.sleep(0.5)


async def test_rolled_back_transaction_delivers_nothing_and_leaves_no_row(pg):
    await pg.run("super", "DELETE FROM assess_jobs")
    async with _listening(pg) as listener:
        async with pg.conn("api") as c:
            assert (
                await enqueue_job(c, JobKind.SETUP_CHECK, {"site_id": "rolled-back"})
                is True
            )
            await c.rollback()
        await _settle()
        assert listener.payloads == []
    assert (await pg.run("super", "SELECT count(*) FROM assess_jobs"))[0][0] == 0


async def test_committed_transaction_delivers_exactly_once_with_empty_payload(pg):
    await pg.run("super", "DELETE FROM assess_jobs")
    async with _listening(pg) as listener:
        async with pg.conn("api") as c:
            assert (
                await enqueue_job(
                    c,
                    JobKind.TRANSCRIPT_GENERATE,
                    {"session_id": "s-1"},
                    dedupe_key="transcript_generate:s-1",
                )
                is True
            )
            await _settle()
            assert listener.payloads == [], "delivered before commit"
            await c.commit()
        await _settle()
        assert listener.payloads == [""]
    rows = await pg.run(
        "super", "SELECT kind, state, args->>'session_id', attempts FROM assess_jobs"
    )
    assert [tuple(r) for r in rows] == [("transcript_generate", "ready", "s-1", 0)]


async def test_duplicate_dedupe_key_adds_no_row_and_no_second_notification(pg):
    await pg.run("super", "DELETE FROM assess_jobs")
    async with _listening(pg) as listener:
        async with pg.conn("api") as c:
            first = await enqueue_job(
                c, JobKind.SETUP_CHECK, {"site_id": "x"}, dedupe_key="d"
            )
            await c.commit()
        await _settle()
        assert listener.payloads == [""]
        async with pg.conn("api") as c:
            second = await enqueue_job(
                c, JobKind.SETUP_CHECK, {"site_id": "x"}, dedupe_key="d"
            )
            await c.commit()
        await _settle()
        assert (first, second) == (True, False)
        assert listener.payloads == [""], (
            "a deduplicated enqueue must not wake the worker"
        )
    assert (await pg.run("super", "SELECT count(*) FROM assess_jobs"))[0][0] == 1


async def test_api_role_can_enqueue_but_not_read_or_claim(pg):
    """The helper runs as the API role: INSERT and the sequence, nothing else."""
    async with pg.conn("api") as c:
        await enqueue_job(c, JobKind.PURGE, {"k": 1})
        await c.commit()
    with pytest.raises(Exception, match="permission denied"):
        await pg.run("api", "SELECT * FROM assess_jobs")
    with pytest.raises(Exception, match="permission denied"):
        await pg.run("api", "UPDATE assess_jobs SET state='done'")


async def test_job_kind_enum_equals_the_table_check(pg):
    definition = (
        await pg.run(
            "super",
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname='ck_assess_jobs_kind'",
        )
    )[0][0]
    in_table = set(re.findall(r"'([a-z_]+)'", definition))
    assert in_table == {k.value for k in JobKind}, sorted(
        in_table ^ {k.value for k in JobKind}
    )


async def test_0044_drops_rotate_keys_and_the_downgrade_restores_it(pg):
    """``rotate_keys`` had no handler, so no production worker could start (2026-11-10): the
    kind leaves the CHECK; a stray row is deleted by the upgrade; the downgrade accepts it again."""
    from .conftest import _alembic

    rotate = "INSERT INTO assess_jobs (kind, args) VALUES ('rotate_keys', '{}'::jsonb)"
    with pytest.raises(Exception, match="ck_assess_jobs_kind"):
        await pg.run("super", rotate)
    down = _alembic(pg.super_url, "downgrade", "0043")
    assert down.returncode == 0, down.stderr
    try:
        await pg.run("super", rotate)
        up = _alembic(pg.super_url, "upgrade", "head")
        assert up.returncode == 0, up.stderr
    finally:
        _alembic(pg.super_url, "upgrade", "head")
    left = await pg.run(
        "super", "SELECT count(*) FROM assess_jobs WHERE kind = 'rotate_keys'"
    )
    assert left[0][0] == 0
    with pytest.raises(Exception, match="ck_assess_jobs_kind"):
        await pg.run("super", rotate)
