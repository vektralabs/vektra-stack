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
    "COALESCE(:not_before, now()), COALESCE(:max_attempts, 5)) "
    # a duplicate dedupe key is skipped, not an error: no savepoint and no rollback per offer
    # (the delivery tick re-offers due transcripts every 30 s). No conflict target: inferring
    # the partial index would need SELECT on assess_jobs, which the API role does not have;
    # the only other unique key is the generated id, which cannot collide.
    "ON CONFLICT DO NOTHING"
)
_NOTIFY = text(f"SELECT pg_notify('{NOTIFY_CHANNEL}', '')")


class JobKind(enum.StrEnum):
    """The kinds the table accepts (``ck_assess_jobs_kind``). A kind that is not in the
    table's CHECK needs a migration first; a test compares the two lists."""

    # also writes the capability matrix (its probes); ``capability_probe`` was dropped in 0037
    SETUP_CHECK = "setup_check"
    GATE_REFRESH = "gate_refresh"
    RENDER_CHECK = "render_check"
    # ``session_deadline`` was dropped in 0037: the sweeper tick and the lazy close enforce it
    TRANSCRIPT_GENERATE = "transcript_generate"
    TRANSCRIPT_DELIVER = "transcript_deliver"
    ENROLMENT_RECHECK = "enrolment_recheck"
    PURGE = "purge"
    # ``rotate_keys`` was dropped in 0044: key rotation is the operator CLI ``keys rewrap``
    ERASURE = "erasure"
    PREFLIGHT = "preflight"  # plan assess-session-lifecycle (revision 0022)
    RECONCILE_REPLY = "reconcile_reply"  # plan assess-session-lifecycle (revision 0024)
    # enqueued by a service_failure close (revision 0026); delivered by plan transcript-delivery
    TEACHER_NOTIFY = "teacher_notify"
    # 3 consecutive system rollbacks (revision 0028); delivered by plan transcript-delivery
    OPERATOR_NOTIFY = "operator_notify"
    # the activity-level teacher facts (editors, graders, REQ-077 enable facts) a teacher request
    # is verified against (revision 0036, plan assess-teacher-ops)
    TEACHER_GATE = "teacher_gate"
    # the operator's SMTP test send (revision 0042, plan assess-privacy-retention 12b)
    SMTP_TEST_SEND = "smtp_test_send"


def teacher_gate_dedupe_key(
    site_id: object, cmid: int, courseid: int, activity_type: str
) -> str:
    """One pending teacher-gate refresh per (site, cmid, course hint, type hint): a pending job
    for a wrong hint never swallows the refresh a correct hint asks for."""
    return f"teacher_gate:{site_id}:{cmid}:{courseid}:{activity_type}"


def teacher_gate_followup_dedupe_key(
    site_id: object, cmid: int, courseid: int, activity_type: str
) -> str:
    """The ONE follow-up refresh allowed beside a pending one (review of #221): the API cannot
    tell a ``ready`` pending job (it will read after the request: it covers it) from a ``leased``
    one (it may have read before: it does not). A second key lets a refusal queue a job that
    surely reads after it; with both keys pending nothing more is queued, which also bounds
    the retry hints a worker that does not read can cause to two."""
    return teacher_gate_dedupe_key(site_id, cmid, courseid, activity_type) + ":next"


def render_dedupe_key(site_id: object, userid: int, cmid: int) -> str:
    """One pending render check per (site, user, cmid), whoever queues it (API or tick)."""
    return f"render_check:{site_id}:{userid}:{cmid}"


def render_followup_dedupe_key(site_id: object, userid: int, cmid: int) -> str:
    """The ONE follow-up render check allowed beside a pending one (as for the teacher gate,
    ``teacher_gate_followup_dedupe_key``): the pending one may already be running and have read
    before a change, so a refusal that promises a retry queues this one too."""
    return render_dedupe_key(site_id, userid, cmid) + ":next"


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
    inserted = (await conn.execute(_INSERT, params)).rowcount
    if not inserted:
        return False  # the pending job with this key covers it
    await conn.execute(_NOTIFY)
    return True


async def enqueue_with_followup(
    conn: AsyncConnection,
    kind: JobKind,
    args: Mapping[str, str | int],
    key: str,
    followup_key: str,
) -> bool:
    """Queue ``kind`` under ``key``, or, when a job with that key is still pending (it may
    already be running and have read before the caller's change: the API cannot tell, it may
    only INSERT), the single follow-up under ``followup_key``. Nothing more while both are
    pending, which bounds the reads per subject. True when a job was inserted now."""
    for k in (key, followup_key):
        if await enqueue_job(conn, kind, args, dedupe_key=k):
            return True
    return False
