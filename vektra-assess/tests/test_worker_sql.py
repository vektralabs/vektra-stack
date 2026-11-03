"""Statement shape and wrappers of ``worker/sql.py`` against a recorded fake (plan B2).

The behaviour itself (skip-locked, fencing, expiry) is proven on a real Postgres in
``integration/test_worker_sql_db.py``; here only the contract of the text and the arguments.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from vektra_assess.jobs import JobKind
from vektra_assess.worker import sql


class _Rec:
    def __init__(
        self, rows: Sequence[Mapping[str, Any]] = (), status: str = "UPDATE 1"
    ):
        self.rows, self.status = rows, status
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> Sequence[Mapping[str, Any]]:
        self.calls.append((query, args))
        return self.rows

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.calls.append((query, args))
        return 12.5

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append((query, args))
        return self.status


def test_claim_is_skip_locked_bounded_and_never_exceeds_max_attempts() -> None:
    q = sql.CLAIM
    assert "FOR UPDATE SKIP LOCKED" in q
    assert "LIMIT $4" in q
    assert "attempts < max_attempts" in q
    assert "state = 'ready' OR (state = 'leased' AND lease_until < now())" in q
    assert "not_before <= now()" in q


@pytest.mark.parametrize("stmt", [sql.COMPLETE, sql.FAIL, sql.DEAD, sql.RELEASE])
def test_every_post_claim_transition_is_fenced(stmt: str) -> None:
    for part in ("id = $1", "leased_by = $2", "attempts = $3", "state = 'leased'"):
        assert part in stmt, (part, stmt)


async def test_claim_passes_kind_values_worker_lease_and_free_slots() -> None:
    rec = _Rec(
        rows=[
            {
                "id": 5,
                "kind": "purge",
                "args": '{"k": 1}',
                "attempts": 1,
                "max_attempts": 5,
            }
        ]
    )
    jobs = await sql.claim(rec, [JobKind.PURGE, JobKind.SETUP_CHECK], "w1", 120.0, 3)
    assert rec.calls == [
        (sql.CLAIM, (["purge", "setup_check"], "w1", [120.0, 120.0], 3))
    ]
    assert jobs == [sql.ClaimedJob(5, JobKind.PURGE, {"k": 1}, 1, 5)]


async def test_claim_with_no_free_slot_does_not_touch_the_database() -> None:
    rec = _Rec()
    assert await sql.claim(rec, [JobKind.PURGE], "w1", 60.0, 0) == []
    assert rec.calls == []


async def test_a_rowcount_of_zero_means_the_fence_was_lost() -> None:
    assert await sql.complete(_Rec(status="UPDATE 1"), 1, "w", 1) is True
    assert await sql.complete(_Rec(status="UPDATE 0"), 1, "w", 1) is False
    assert await sql.fail(_Rec(status="UPDATE 0"), 1, "w", 1, 5.0, "x") is False
    assert await sql.release(_Rec(status="UPDATE 0"), 1, "w", 1) is False


async def test_reclaim_expired_kills_exhausted_rows_before_requeueing() -> None:
    rec = _Rec(status="UPDATE 2")
    assert await sql.reclaim_expired(rec) == (2, 2)
    assert [c[0] for c in rec.calls] == [sql.DEAD_EXPIRED, sql.RECLAIM_EXPIRED]


async def test_cleanup_only_touches_done_rows() -> None:
    assert "state = 'done'" in sql.CLEANUP_DONE
    assert "'dead'" not in sql.CLEANUP_DONE
    rec = _Rec(status="DELETE 42")
    assert await sql.cleanup_done(rec, 24, 1000) == 42
    assert rec.calls == [(sql.CLEANUP_DONE, (24, 1000))]


async def test_exhausted_expiry_is_listed_then_killed_one_row_at_a_time() -> None:
    assert "attempts >= max_attempts" in sql.EXHAUSTED_EXPIRED
    for part in (
        "id = $1",
        "state = 'leased'",
        "lease_until < now()",
        "attempts >= max_attempts",
    ):
        assert part in sql.KILL_EXHAUSTED, part
    rec = _Rec(rows=[{"id": 3, "kind": "purge", "attempts": 5}])
    assert await sql.list_exhausted_expired(rec) == [(3, JobKind.PURGE, 5)]
    assert await sql.kill_exhausted(_Rec(status="UPDATE 1"), 3) is True
    assert await sql.kill_exhausted(_Rec(status="UPDATE 0"), 3) is False
    assert await sql.requeue_expired(_Rec(status="UPDATE 4")) == 4


async def test_claim_passes_one_lease_per_kind_in_kind_order() -> None:
    rec = _Rec()
    await sql.claim(
        rec,
        [JobKind.PURGE, JobKind.SETUP_CHECK, JobKind.ERASURE],
        "w1",
        {JobKind.PURGE: 8.0, JobKind.SETUP_CHECK: 240.0, JobKind.ERASURE: 120.0},
        2,
    )
    assert rec.calls[0][1] == (
        ["purge", "setup_check", "erasure"],
        "w1",
        [8.0, 240.0, 120.0],
        2,
    )
    assert (
        "unnest($1::text[], $3::float8[])" in sql.CLAIM
        and "assess_jobs.kind" in sql.CLAIM
    )
