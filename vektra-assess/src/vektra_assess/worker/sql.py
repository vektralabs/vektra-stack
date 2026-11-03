"""Raw SQL for the leased job loop: claim, fenced complete / fail / release / dead (ARCH-068).

Statements use asyncpg placeholders (``$1``) and run as ``vektra_assess_worker``. Every state
change after a claim is **fenced**: it carries ``leased_by = $worker AND attempts = $attempt AND
state = 'leased'``. A worker whose lease was reclaimed holds a stale ``attempts`` token, so its
late ``complete`` matches no row and cannot overwrite the newer claim; the caller logs that as
``assess_job_fence_lost``.

Job states used here: ``ready`` -> ``leased`` -> ``done``; a failed attempt goes back to ``ready``
with a ``not_before`` backoff, and an exhausted job becomes ``dead``. The table's ``failed``
state is not used by the loop.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from vektra_assess.jobs import JobKind


class Conn(Protocol):
    """The slice of ``asyncpg.Connection`` / ``Pool`` the loop needs."""

    async def fetch(self, query: str, *args: Any) -> Sequence[Mapping[str, Any]]: ...

    async def fetchval(self, query: str, *args: Any) -> Any: ...

    async def execute(self, query: str, *args: Any) -> str: ...


# A job is claimable when ready, or leased with an expired lease, and it still has attempts
# left; exhausted-and-expired rows are moved to ``dead`` by ``DEAD_EXPIRED``, never re-run.
CLAIM = """
UPDATE assess_jobs SET
    state = 'leased',
    lease_until = now() + make_interval(secs => (
        SELECT t.lease FROM unnest($1::text[], $3::float8[]) AS t(kind, lease)
        WHERE t.kind = assess_jobs.kind
    )),
    attempts = attempts + 1,
    leased_by = $2
WHERE id IN (
    SELECT id FROM assess_jobs
    WHERE kind = ANY($1::text[])
      AND (state = 'ready' OR (state = 'leased' AND lease_until < now()))
      AND attempts < max_attempts
      AND not_before <= now()
    ORDER BY not_before, id
    LIMIT $4
    FOR UPDATE SKIP LOCKED
)
RETURNING id, kind, args, attempts, max_attempts
"""

_FENCE = "WHERE id = $1 AND leased_by = $2 AND attempts = $3 AND state = 'leased'"

COMPLETE = f"""
UPDATE assess_jobs SET state = 'done', done_at = now(), lease_until = NULL
{_FENCE}
"""

# A failed attempt returns to ``ready`` after ``$4`` seconds, keeping ``attempts`` so the
# next claim sees how many tries were spent. ``$5`` is a content-free error code.
FAIL = f"""
UPDATE assess_jobs SET
    state = 'ready', lease_until = NULL, leased_by = NULL,
    not_before = now() + make_interval(secs => $4), last_error_code = $5
{_FENCE}
"""

DEAD = f"""
UPDATE assess_jobs SET
    state = 'dead', lease_until = NULL, done_at = now(), last_error_code = $4
{_FENCE}
"""

# Shutdown drain: the job goes back to ``ready`` at once and the aborted run does not count
# as an attempt.
RELEASE = f"""
UPDATE assess_jobs SET
    state = 'ready', lease_until = NULL, leased_by = NULL,
    attempts = GREATEST(attempts - 1, 0)
{_FENCE}
"""

# Startup reconciliation: expired leases go back to ``ready`` (or ``dead`` when no attempt is
# left), before the first claim.
RECLAIM_EXPIRED = """
UPDATE assess_jobs SET state = 'ready', lease_until = NULL, leased_by = NULL
WHERE state = 'leased' AND lease_until < now() AND attempts < max_attempts
"""

DEAD_EXPIRED = """
UPDATE assess_jobs SET
    state = 'dead', lease_until = NULL, done_at = now(),
    last_error_code = 'lease_expired_exhausted'
WHERE state = 'leased' AND lease_until < now() AND attempts >= max_attempts
"""

# Reconciliation splits the exhausted case in two so the audit event comes first: list the
# candidates, write the audit row for each, then kill that one row if it is still exactly that.
EXHAUSTED_EXPIRED = """
SELECT id, kind, attempts FROM assess_jobs
WHERE state = 'leased' AND lease_until < now() AND attempts >= max_attempts
ORDER BY id
"""

KILL_EXHAUSTED = """
UPDATE assess_jobs SET
    state = 'dead', lease_until = NULL, done_at = now(),
    last_error_code = 'lease_expired_exhausted'
WHERE id = $1 AND state = 'leased' AND lease_until < now() AND attempts >= max_attempts
"""

CLEANUP_DONE = """
DELETE FROM assess_jobs WHERE id IN (
    SELECT id FROM assess_jobs
    WHERE state = 'done' AND done_at < now() - make_interval(hours => $1)
    ORDER BY id LIMIT $2
)
"""

OLDEST_READY_AGE_S = """
SELECT COALESCE(EXTRACT(EPOCH FROM now() - min(not_before)), 0)::float8
FROM assess_jobs WHERE state = 'ready' AND not_before <= now()
"""


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: int
    kind: JobKind
    args: Mapping[str, str | int]
    attempt: int
    max_attempts: int


def _rowcount(status: str) -> int:
    """asyncpg ``execute`` returns a command tag such as ``UPDATE 1`` or ``DELETE 42``."""
    return int(status.rsplit(" ", 1)[-1])


def _args(raw: Any) -> Mapping[str, str | int]:
    value = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    return cast(Mapping[str, str | int], dict(value))


async def claim(
    conn: Conn,
    kinds: Sequence[JobKind],
    worker_id: str,
    lease_s: float | Mapping[JobKind, float],
    free: int,
) -> list[ClaimedJob]:
    """Claim up to ``free`` jobs of ``kinds``: never more than the pool has free slots.

    ``lease_s`` is one number for all kinds or a mapping per kind: each claimed row gets the lease
    of ITS kind (a short-lived kind must not inherit the longest lease of its pool, or a crashed
    worker's jobs would wait for that longer expiry).
    """
    if free <= 0:
        return []
    leases = [
        float(lease_s[k] if isinstance(lease_s, Mapping) else lease_s) for k in kinds
    ]
    rows = await conn.fetch(CLAIM, [k.value for k in kinds], worker_id, leases, free)
    return [
        ClaimedJob(
            id=r["id"],
            kind=JobKind(r["kind"]),
            args=_args(r["args"]),
            attempt=r["attempts"],
            max_attempts=r["max_attempts"],
        )
        for r in rows
    ]


async def complete(conn: Conn, job_id: int, worker_id: str, attempt: int) -> bool:
    return _rowcount(await conn.execute(COMPLETE, job_id, worker_id, attempt)) == 1


async def fail(
    conn: Conn,
    job_id: int,
    worker_id: str,
    attempt: int,
    backoff_s: float,
    error_code: str,
) -> bool:
    return (
        _rowcount(
            await conn.execute(FAIL, job_id, worker_id, attempt, backoff_s, error_code)
        )
        == 1
    )


async def mark_dead(
    conn: Conn, job_id: int, worker_id: str, attempt: int, error_code: str
) -> bool:
    return (
        _rowcount(await conn.execute(DEAD, job_id, worker_id, attempt, error_code)) == 1
    )


async def release(conn: Conn, job_id: int, worker_id: str, attempt: int) -> bool:
    return _rowcount(await conn.execute(RELEASE, job_id, worker_id, attempt)) == 1


async def reclaim_expired(conn: Conn) -> tuple[int, int]:
    """``(returned_to_ready, marked_dead)`` for leases that expired while no worker ran."""
    dead = _rowcount(await conn.execute(DEAD_EXPIRED))
    ready = _rowcount(await conn.execute(RECLAIM_EXPIRED))
    return ready, dead


async def cleanup_done(conn: Conn, retention_h: int, batch: int = 1000) -> int:
    """Delete ``done`` rows older than the retention, in batches; ``dead`` rows are kept."""
    return _rowcount(await conn.execute(CLEANUP_DONE, retention_h, batch))


async def oldest_ready_age_s(conn: Conn) -> float:
    return float(await conn.fetchval(OLDEST_READY_AGE_S))


async def requeue_expired(conn: Conn) -> int:
    """Expired leases with attempts left go back to ``ready``; returns how many."""
    return _rowcount(await conn.execute(RECLAIM_EXPIRED))


async def list_exhausted_expired(conn: Conn) -> list[tuple[int, JobKind, int]]:
    """``(id, kind, attempts)`` of leased jobs that expired with no attempt left."""
    rows = await conn.fetch(EXHAUSTED_EXPIRED)
    return [(r["id"], JobKind(r["kind"]), r["attempts"]) for r in rows]


async def kill_exhausted(conn: Conn, job_id: int) -> bool:
    """Mark one exhausted-and-expired job dead; False if it changed in the meantime."""
    return _rowcount(await conn.execute(KILL_EXHAUSTED, job_id)) == 1
