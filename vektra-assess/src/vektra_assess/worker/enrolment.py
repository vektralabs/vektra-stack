"""Enrolment re-check (NFR-014, plan assess-session-lifecycle M3.7).

Every 60 s the tick queues one ``enrolment_recheck`` job per live session (ACTIVE or OUTAGE,
deduped per session, WS pool). The handler reads, fresh, whether the student is still an active
student of the activity's course (the plan-4 readers through the ``StartSource``) and appends one
``assess_enrolment_checks`` row: ``enrolled``, ``not_enrolled`` or ``unreadable``.

- ``not_enrolled`` (unenrolled, suspended, or the student role lost): the session is closed
  ``access_revoked`` through the close function (epoch bump, hand-off delete, transcript job).
- ``unreadable``: recorded only. The API's gate step 6 accepts a turn only on an ``enrolled`` row
  at most ``VEKTRA_ASSESS_ENROLMENT_FRESHNESS_S`` old, so an unreadable LMS, like a stopped
  worker, rejects turns (fail closed) without closing anything.

Older rows of a session are trimmed (the newest is always kept).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

import structlog
from sqlalchemy import text

from vektra_assess.jobs import JobKind, enqueue_job
from vektra_assess.worker.preflight import StartSource, StartTarget
from vektra_assess.worker.runner import AcquireDb, JobContext

log = structlog.get_logger(__name__)

#: the live sessions whose last check is oldest first, never-checked ones before all
#: (review fix 9): with more live sessions than one batch, every session is still reached
_LIVE = text(
    "SELECT s.id FROM assess_sessions s LEFT JOIN LATERAL (SELECT max(c.checked_at) AS last "
    "FROM assess_enrolment_checks c WHERE c.session_id = s.id) c ON true "
    "WHERE s.state IN ('ACTIVE', 'OUTAGE') "
    # a session whose re-check is still queued or running does not take a place in the batch
    # (so the batch reaches the others instead of re-selecting it)
    "AND NOT EXISTS (SELECT 1 FROM assess_jobs j WHERE j.dedupe_key = "
    "'enrolment_recheck:' || s.id::text AND j.state IN ('ready', 'leased')) "
    "ORDER BY c.last ASC NULLS FIRST, s.id LIMIT :n"
)
_SESSION = text(
    "SELECT s.id, s.site_id, s.cmid, s.student_userid, s.courseid_hint, s.session_no, s.epoch, "
    "s.state, c.activity_type, c.flags FROM assess_sessions s JOIN assess_activity_config c "
    "ON c.site_id = s.site_id AND c.cmid = s.cmid WHERE s.id = :sid"
)
#: dated when the LMS read STARTED (database clock), not when its answer is written: a
#: slow read must not make the observation look fresher
_RECORD = text(
    "INSERT INTO assess_enrolment_checks (session_id, result, checked_at) "
    "VALUES (:s, :r, :observed)"
)
_CLOCK = text("SELECT clock_timestamp()")
_TRIM = text(
    "DELETE FROM assess_enrolment_checks WHERE session_id = :s "
    "AND checked_at < now() - interval '1 hour' AND id <> (SELECT max(id) FROM "
    "assess_enrolment_checks WHERE session_id = :s)"
)
_CLOSE = text("SELECT assess_close_session(:sid, :epoch, 'access_revoked')")


async def queue_rechecks(acquire: AcquireDb, *, batch: int = 500) -> int:
    queued = 0
    async with acquire() as conn:
        async with conn.begin():
            for r in (await conn.execute(_LIVE, {"n": batch})).all():
                queued += await enqueue_job(
                    conn,
                    JobKind.ENROLMENT_RECHECK,
                    {"session_id": str(r.id)},
                    dedupe_key=f"enrolment_recheck:{r.id}",
                )
    return queued


async def recheck_one(acquire: AcquireDb, source: StartSource, sid: UUID) -> str | None:
    """Re-check one session; returns the recorded result (None for a stale job)."""
    async with acquire() as conn:
        row = (await conn.execute(_SESSION, {"sid": sid})).first()
        observed = (await conn.execute(_CLOCK)).scalar_one()  # before the read
    if row is None or row.state not in ("ACTIVE", "OUTAGE"):
        return None
    target = StartTarget(
        session_id=sid,
        site_id=row.site_id,
        cmid=int(row.cmid),
        student_userid=int(row.student_userid),
        courseid_hint=row.courseid_hint,
        activity_type=str(row.activity_type),
        session_no=int(row.session_no),
        flags=dict(row.flags or {}),
    )
    try:
        enrolled = await source.enrolled(target)
    except Exception as exc:  # unreadable, never a guess
        log.warning("assess_enrolment_read_failed", error_type=type(exc).__name__)
        enrolled = None
    result = (
        "enrolled"
        if enrolled is True
        else "not_enrolled"
        if enrolled is False
        else "unreadable"
    )
    async with acquire() as conn:
        async with conn.begin():
            await conn.execute(_RECORD, {"s": sid, "r": result, "observed": observed})
            await conn.execute(_TRIM, {"s": sid})
            if result == "not_enrolled":
                await conn.execute(_CLOSE, {"sid": sid, "epoch": row.epoch})
    if result != "enrolled":
        log.info("assess_enrolment_rechecked", session_id=str(sid), result=result)
    return result


def make_recheck_handler(
    source: StartSource,
) -> Callable[[JobContext], Awaitable[None]]:
    async def handle(ctx: JobContext) -> None:
        try:
            sid = UUID(str(ctx.args.get("session_id")))
        except ValueError:
            return
        await recheck_one(ctx.acquire, source, sid)

    return handle
