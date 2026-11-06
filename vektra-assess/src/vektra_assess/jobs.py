"""Transactional outbox for the assess job loop (ARCH-068, INT-002).

``enqueue_job`` is the only way application code puts work on ``assess_jobs``: it inserts the
row and sends an **empty** ``NOTIFY assess_jobs`` on the caller's connection, so both
happen in the caller's transaction. A rolled-back transaction delivers no notification and
leaves no row; a committed one delivers exactly one wake-up. The notification is only a
latency hint, the worker's 1 s poll is the correctness backstop.

This module is importable by the API: it contains no worker code. A job carries ids only
(``args``, ``pg_column_size < 1024`` in the table): a handler must re-read the authoritative
rows before acting, so a stale job is a no-op that completes.
"""

from __future__ import annotations

import enum
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

# The channel carries no payload, ever (INT-002): a payload would be content that can leak
# into logs and would break at 8000 bytes.
NOTIFY_CHANNEL = "assess_jobs"

# A plain INSERT on purpose: the API role has INSERT on assess_jobs and nothing else, and both
# ``RETURNING`` and ``ON CONFLICT`` need SELECT. A duplicate ``dedupe_key`` therefore shows up
# as a unique violation on ``uq_assess_jobs_dedupe``, handled inside a savepoint below.
_INSERT = text(
    "INSERT INTO assess_jobs (kind, args, dedupe_key, not_before, max_attempts) "
    "VALUES (:kind, CAST(:args AS jsonb), :dedupe_key, "
    "COALESCE(:not_before, now()), COALESCE(:max_attempts, 5))"
)
_DEDUPE_INDEX = "uq_assess_jobs_dedupe"
_NOTIFY = text(f"SELECT pg_notify('{NOTIFY_CHANNEL}', '')")


class JobKind(enum.StrEnum):
    """The kinds the table accepts (``ck_assess_jobs_kind``). A kind that is not in the
    table's CHECK needs a migration first; a test compares the two lists."""

    SETUP_CHECK = "setup_check"
    CAPABILITY_PROBE = "capability_probe"
    GATE_REFRESH = "gate_refresh"
    RENDER_CHECK = "render_check"
    SESSION_DEADLINE = "session_deadline"
    TRANSCRIPT_GENERATE = "transcript_generate"
    TRANSCRIPT_DELIVER = "transcript_deliver"
    ENROLMENT_RECHECK = "enrolment_recheck"
    PURGE = "purge"
    ROTATE_KEYS = "rotate_keys"
    ERASURE = "erasure"
    PREFLIGHT = "preflight"  # plan assess-session-lifecycle (revision 0022)
    RECONCILE_REPLY = "reconcile_reply"  # plan assess-session-lifecycle (revision 0024)
    # enqueued by a service_failure close (revision 0026); delivered by plan transcript-delivery
    TEACHER_NOTIFY = "teacher_notify"


def render_dedupe_key(site_id: object, userid: int, cmid: int) -> str:
    """One pending render check per (site, user, cmid), whoever queues it (API or tick)."""
    return f"render_check:{site_id}:{userid}:{cmid}"


_ARG_TYPES = (str, int)


def _validate_args(args: Mapping[str, Any]) -> str:
    """Ids only: a job argument is a short string or an integer, never a nested value."""
    for key, value in args.items():
        if (
            not isinstance(key, str)
            or isinstance(value, bool)
            or not isinstance(value, _ARG_TYPES)
        ):
            raise ValueError(f"job argument {key!r} must be a string or an integer id")
    return json.dumps(dict(args), sort_keys=True, separators=(",", ":"))


async def enqueue_job(
    conn: AsyncConnection,
    kind: JobKind,
    args: Mapping[str, str | int] | None = None,
    *,
    dedupe_key: str | None = None,
    not_before: datetime | None = None,
    max_attempts: int | None = None,
) -> bool:
    """Insert a ready job and wake the worker, in the caller's transaction.

    Returns ``True`` when a job was inserted, ``False`` when ``dedupe_key`` matched a job that
    is still ``ready`` or ``leased`` (the existing job covers it; no notification is sent).

    **Choose the dedupe key with that in mind.** A ``ready`` job has not read anything yet, so
    covering a newer change is safe. A ``leased`` job is already running: its handler may have read
    its inputs before the change was committed, so the change is NOT covered and no new job is
    queued. A caller whose change must always be seen puts a version, epoch or generation in the
    key (a new value is a new key), or relies on a later periodic tick that re-reads the row.
    The id is not returned: reading it back would need SELECT, which the API role lacks.
    """
    if max_attempts is not None and max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    params = {
        "kind": JobKind(kind).value,
        "args": _validate_args(args or {}),
        "dedupe_key": dedupe_key,
        "not_before": not_before,
        "max_attempts": max_attempts,
    }
    try:
        async with conn.begin_nested():
            await conn.execute(_INSERT, params)
    except IntegrityError as exc:
        if _DEDUPE_INDEX in str(exc.orig):
            return False
        raise
    await conn.execute(_NOTIFY)
    return True
