"""``worker/sql.py`` on a real Postgres, as the real ``vektra_assess_worker`` role (plan B2).

Counts rows and reads states back from the table; a helper's return value is never taken as
proof. Covers claim bounds, skip-locked under concurrency, fencing after a reclaim, retry
backoff, exhaustion, shutdown release and the done-row cleanup.
"""

from __future__ import annotations

import asyncio
import json

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from vektra_assess.jobs import JobKind
from vektra_assess.worker import sql

pytestmark = pytest.mark.integration


def _dsn(pg, who: str) -> str:
    return (
        make_url(pg.url(who))
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )


async def _reset(pg) -> None:
    await pg.run("super", "DELETE FROM assess_jobs")


async def _add(pg, n: int, kind: str = "purge", **cols: object) -> None:
    for i in range(n):
        await pg.run(
            "super",
            "INSERT INTO assess_jobs (kind, args, not_before, max_attempts) VALUES "
            "(:k, CAST(:a AS jsonb), now() - make_interval(secs => :off), :m)",
            {
                "k": kind,
                "a": json.dumps({"n": i}),
                "off": n - i,  # older first
                "m": cols.get("max_attempts", 5),
            },
        )


async def _row(pg, job_id: int):
    return (
        await pg.run(
            "super",
            "SELECT state, attempts, leased_by, last_error_code, "
            "lease_until IS NULL FROM assess_jobs WHERE id = :i",
            {"i": job_id},
        )
    )[0]


async def test_claim_is_bounded_ordered_and_leases_the_rows(pg):
    await _reset(pg)
    await _add(pg, 5)
    c = await asyncpg.connect(_dsn(pg, "worker"))
    try:
        got = await sql.claim(c, [JobKind.PURGE], "w1", 60.0, 2)
        # which rows are claimed follows the subquery's ORDER BY; the order of the RETURNING list
        # is not specified, so compare the set (sorted) rather than the list order
        assert sorted(j.args["n"] for j in got) == [0, 1], "the two oldest not_before"
        assert all(j.attempt == 1 for j in got)
        states = await pg.run(
            "super", "SELECT state, count(*) FROM assess_jobs GROUP BY state ORDER BY 1"
        )
        assert [tuple(r) for r in states] == [("leased", 2), ("ready", 3)]
        assert await sql.claim(c, [JobKind.SETUP_CHECK], "w1", 60.0, 5) == []
    finally:
        await c.close()


async def test_concurrent_claims_never_return_the_same_job(pg):
    await _reset(pg)
    await _add(pg, 12)
    a = await asyncpg.connect(_dsn(pg, "worker"))
    b = await asyncpg.connect(_dsn(pg, "worker"))
    try:
        ra, rb = await asyncio.gather(
            sql.claim(a, [JobKind.PURGE], "wa", 60.0, 12),
            sql.claim(b, [JobKind.PURGE], "wb", 60.0, 12),
        )
        ids = [j.id for j in ra] + [j.id for j in rb]
        assert len(ids) == len(set(ids)) == 12, (len(ra), len(rb))
        owners = await pg.run(
            "super", "SELECT leased_by, count(*) FROM assess_jobs GROUP BY 1 ORDER BY 1"
        )
        assert sum(r[1] for r in owners) == 12
    finally:
        await a.close()
        await b.close()


async def test_a_stale_worker_cannot_complete_a_job_that_was_reclaimed(pg):
    await _reset(pg)
    await _add(pg, 1)
    a = await asyncpg.connect(_dsn(pg, "worker"))
    b = await asyncpg.connect(_dsn(pg, "worker"))
    try:
        (first,) = await sql.claim(a, [JobKind.PURGE], "wa", 0.2, 1)
        await asyncio.sleep(0.5)  # lease expired, nobody renewed it
        (second,) = await sql.claim(b, [JobKind.PURGE], "wb", 60.0, 1)
        assert first.id == second.id
        assert (first.attempt, second.attempt) == (1, 2)
        assert await sql.complete(a, first.id, "wa", first.attempt) is False
        state, attempts, owner, *_ = await _row(pg, first.id)
        assert (state, attempts, owner) == ("leased", 2, "wb"), "newer claim untouched"
        assert await sql.complete(b, second.id, "wb", second.attempt) is True
        assert (await _row(pg, first.id))[0] == "done"
    finally:
        await a.close()
        await b.close()


async def test_failed_attempt_backs_off_and_is_not_claimable_until_due(pg):
    await _reset(pg)
    await _add(pg, 1)
    c = await asyncpg.connect(_dsn(pg, "worker"))
    try:
        (job,) = await sql.claim(c, [JobKind.PURGE], "w", 60.0, 1)
        assert await sql.fail(c, job.id, "w", job.attempt, 3600.0, "timeout") is True
        state, attempts, owner, code, lease_null = await _row(pg, job.id)
        assert (state, attempts, owner, code, lease_null) == (
            "ready", 1, None, "timeout", True,
        )  # fmt: skip
        assert await sql.claim(c, [JobKind.PURGE], "w", 60.0, 1) == []
    finally:
        await c.close()


async def test_exhausted_job_is_never_claimed_again_and_becomes_dead(pg):
    await _reset(pg)
    await _add(pg, 1, max_attempts=1)
    c = await asyncpg.connect(_dsn(pg, "worker"))
    try:
        (job,) = await sql.claim(c, [JobKind.PURGE], "w", 0.1, 1)
        await asyncio.sleep(0.3)
        assert await sql.claim(c, [JobKind.PURGE], "w2", 60.0, 1) == []
        exhausted = await sql.list_exhausted_expired(c)
        assert [(j[0], j[2]) for j in exhausted] == [(job.id, 1)]
        assert await sql.requeue_expired(c) == 0, "no attempt left: not requeued"
        assert await sql.kill_exhausted(c, job.id) is True
        assert await sql.kill_exhausted(c, job.id) is False, "already dead"
        state, _, _, code, _ = await _row(pg, job.id)
        assert (state, code) == ("dead", "lease_expired_exhausted")
    finally:
        await c.close()


async def test_reclaim_requeues_expired_leases_with_attempts_left(pg):
    await _reset(pg)
    await _add(pg, 2)
    c = await asyncpg.connect(_dsn(pg, "worker"))
    try:
        await sql.claim(c, [JobKind.PURGE], "w", 0.1, 2)
        await asyncio.sleep(0.3)
        assert await sql.requeue_expired(c) == 2
        assert await sql.list_exhausted_expired(c) == []
        assert (
            await pg.run(
                "super", "SELECT count(*) FROM assess_jobs WHERE state='ready'"
            )
        )[0][0] == 2
    finally:
        await c.close()


async def test_release_returns_the_job_and_does_not_count_as_an_attempt(pg):
    await _reset(pg)
    await _add(pg, 1)
    c = await asyncpg.connect(_dsn(pg, "worker"))
    try:
        (job,) = await sql.claim(c, [JobKind.PURGE], "w", 60.0, 1)
        assert await sql.release(c, job.id, "w", job.attempt) is True
        state, attempts, owner, *_ = await _row(pg, job.id)
        assert (state, attempts, owner) == ("ready", 0, None)
        (again,) = await sql.claim(c, [JobKind.PURGE], "w2", 60.0, 1)
        assert again.attempt == 1
    finally:
        await c.close()


async def test_cleanup_deletes_old_done_rows_in_batches_and_keeps_dead(pg):
    await _reset(pg)
    for state in ("done", "done", "done", "dead"):
        await pg.run(
            "super",
            "INSERT INTO assess_jobs (kind, state, done_at) VALUES "
            "('purge', :s, now() - interval '48 hours')",
            {"s": state},
        )
    await pg.run(
        "super",
        "INSERT INTO assess_jobs (kind, state, done_at) VALUES ('purge','done', now())",
    )
    c = await asyncpg.connect(_dsn(pg, "worker"))
    try:
        assert await sql.cleanup_done(c, 24, batch=2) == 2
        assert await sql.cleanup_done(c, 24, batch=2) == 1
        assert await sql.cleanup_done(c, 24, batch=2) == 0
        left = await pg.run(
            "super", "SELECT state, count(*) FROM assess_jobs GROUP BY 1 ORDER BY 1"
        )
        assert [tuple(r) for r in left] == [("dead", 1), ("done", 1)]
    finally:
        await c.close()


async def test_oldest_ready_age_ignores_future_and_leased_rows(pg):
    await _reset(pg)
    c = await asyncpg.connect(_dsn(pg, "worker"))
    try:
        assert await sql.oldest_ready_age_s(c) == 0.0
        await pg.run(
            "super",
            "INSERT INTO assess_jobs (kind, not_before) VALUES "
            "('purge', now() - interval '90 seconds'), "
            "('purge', now() + interval '1 hour')",
        )
        age = await sql.oldest_ready_age_s(c)
        assert 85 <= age <= 120, age
    finally:
        await c.close()


async def test_each_kind_gets_the_lease_of_its_own_kind_not_its_pools_longest(pg):
    """Found by the chaos harness: one lease per pool made a short kind wait for the longest."""
    await pg.run("super", "DELETE FROM assess_jobs")
    await pg.run(
        "super", "INSERT INTO assess_jobs (kind) VALUES ('purge'), ('setup_check')"
    )
    c = await asyncpg.connect(_dsn(pg, "worker"))
    try:
        got = await sql.claim(
            c,
            [JobKind.PURGE, JobKind.SETUP_CHECK],
            "w",
            {JobKind.PURGE: 8.0, JobKind.SETUP_CHECK: 240.0},
            5,
        )
        assert len(got) == 2
        rows = await pg.run(
            "super",
            "SELECT kind, extract(epoch FROM lease_until - now()) FROM assess_jobs ORDER BY kind",
        )
        by = {r[0]: float(r[1]) for r in rows}
        assert 5 <= by["purge"] <= 8.5, by
        assert 230 <= by["setup_check"] <= 240.5, by
    finally:
        await c.close()


async def test_the_attempts_policy_lowers_what_a_row_allows_and_never_raises_it(pg):
    """Review finding: VEKTRA_ASSESS_JOB_<KIND>_MAX_ATTEMPTS had no effect on retries."""
    await pg.run("super", "DELETE FROM assess_jobs")
    await pg.run(
        "super",
        "INSERT INTO assess_jobs (kind, max_attempts) VALUES ('purge', 5), ('setup_check', 2)",
    )
    c = await asyncpg.connect(_dsn(pg, "worker"))
    try:
        got = await sql.claim(
            c,
            [JobKind.PURGE, JobKind.SETUP_CHECK],
            "w",
            60.0,
            5,
            {JobKind.PURGE: 2, JobKind.SETUP_CHECK: 9},
        )
        eff = {j.kind: j.max_attempts for j in got}
        assert eff == {JobKind.PURGE: 2, JobKind.SETUP_CHECK: 2}, (
            "policy 2 lowers the stored 5; policy 9 cannot raise the stored 2"
        )
        stored = {
            r[0]: r[1]
            for r in await pg.run("super", "SELECT kind, max_attempts FROM assess_jobs")
        }
        assert stored == {"purge": 2, "setup_check": 2}, (
            "the effective limit is written back"
        )
        # with the policy at 2 a job that already used 2 attempts is no longer claimable
        await pg.run(
            "super",
            "UPDATE assess_jobs SET state='ready', lease_until=NULL, leased_by=NULL, attempts=2 "
            "WHERE kind='purge'",
        )
        again = await sql.claim(c, [JobKind.PURGE], "w", 60.0, 5, {JobKind.PURGE: 2})
        assert again == []
    finally:
        await c.close()
