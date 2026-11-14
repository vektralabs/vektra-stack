"""The activity-level facts a teacher request is verified against (plan assess-teacher-ops 11a).

The API cannot ask the LMS (only the worker holds the provider's credential). A ``teacher_gate`` job (args:
site id, cmid, course id hint, activity type hint) resolves the cmid INSIDE the hinted course
(``resolve_in_course``: a cmid of another course is never found there) and upserts
``assess_teacher_gate(site_id, cmid)`` with:

- ``resolved``: whether the cmid was found in that course as that type (False: every fact absent,
  and the API refuses the actor; a wrong hint never verifies anybody); ``hint``: the
  ``[courseid, type]`` this row answers;
- ``courseid``, ``activity_type``;
- ``editors``: user ids holding the LMS's activity-editor capability in the course (None when
  unreadable: the API fails closed); ``graders``: user ids holding ``mod/assign:grade`` (11b:
  the session list, reset, terminate, extra time, opening fallback);
- for a quiz, ``override_holder`` (the participant whose user override sets the latest close,
  when the overrides are readable) and ``override_viewers`` (``mod/quiz:manageoverrides``): the
  REQ-076 gate panel names the holder only to a viewer; never the override's reason;
- the REQ-077 enable facts: ``close_at`` (quiz ``timeclose`` / assignment ``cutoffdate``, 0 = none
  configured: BR-007), ``team_submission``, ``grading_method``, ``lms_rubric`` (a rubric is
  defined for the LMS-rubric source); each None when unreadable.

Nothing else: no names, no addresses, no content (plus, for a job that says who queued it, that
``queued_by`` and the job's enqueue time ``queued_at``: the API's refusal finality, ``api.teachers``).
An upsert never replaces a newer read, nor a resolved row with an unresolved read for another
course hint. The API accepts the row only while it is at
most ``VEKTRA_ASSESS_TEACHER_GATE_MAX_AGE_S`` old, otherwise it queues this job and refuses.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import UUID

import structlog
from sqlalchemy import text

from vektra_assess.worker.runner import AcquireDb, JobContext

log = structlog.get_logger(__name__)

KINDS = ("quiz", "assign")


class TeacherGateSource(Protocol):
    async def teacher_facts(
        self, site_id: UUID, cmid: int, courseid: int, kind: str
    ) -> dict[str, Any]: ...


# ``refreshed_at`` is the database clock read BEFORE the LMS reads (the API compares it with
# the same clock): a slow read never makes its list look fresher than it is
_CLOCK = text("SELECT clock_timestamp()")
_UPSERT = text(
    "INSERT INTO assess_teacher_gate (site_id, cmid, inputs, refreshed_at) "
    "VALUES (:s, :c, CAST(:i AS jsonb), :t) ON CONFLICT (site_id, cmid) "
    "DO UPDATE SET inputs = EXCLUDED.inputs, refreshed_at = EXCLUDED.refreshed_at "
    # a read that resolved nothing (not in that course, or a transient failure) for ANOTHER
    # hint never replaces a resolved row: a wrong course in some request must not turn the
    # activity's facts into activity_not_in_course (review of #221). The row's own hint
    # still replaces it (the activity moved, or became unreadable): fail closed as before
    "WHERE NOT ((EXCLUDED.inputs->>'resolved') IS DISTINCT FROM 'true' "
    "AND assess_teacher_gate.inputs->>'resolved' = 'true' "
    "AND assess_teacher_gate.inputs->'hint' IS DISTINCT FROM EXCLUDED.inputs->'hint') "
    # monotonic: two refreshes finishing out of order never let the older read win
    "AND assess_teacher_gate.refreshed_at <= EXCLUDED.refreshed_at"
)
#: the job's enqueue time, stamped into the row with who queued it (``queued_by``): the API
#: takes the facts a refusal's own refresh read as that refusal's final answer
_QUEUED_AT = text(
    "SELECT extract(epoch FROM created_at) FROM assess_jobs WHERE id = :id"
)
QUEUED_BY = ("refusal", "page")


async def refresh_teacher_gate(
    source: TeacherGateSource,
    acquire: AcquireDb,
    site_id: UUID,
    cmid: int,
    courseid: int,
    kind: str,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read the facts fresh and store them, dated when the reads began; returns what was
    stored. ``provenance``: ``{queued_by, queued_at}`` of the job that asked for the read,
    stored with the facts (ids and a timestamp only)."""
    async with acquire() as conn:
        observed_at = (await conn.execute(_CLOCK)).scalar()
        await conn.rollback()
    try:
        facts = await source.teacher_facts(site_id, cmid, courseid, kind)
    except Exception as exc:  # a reader bug is "unreadable", never a guess
        log.warning("assess_teacher_gate_error", error_type=type(exc).__name__)
        facts = {"resolved": False, "reason": "error"}
    # the hint this row answers: the API re-queues when a request brings another one
    facts["hint"] = [int(courseid), kind]
    facts.update(provenance or {})
    async with acquire() as conn:
        async with conn.begin():
            await conn.execute(
                _UPSERT,
                {
                    "s": site_id,
                    "c": int(cmid),
                    "i": json.dumps(facts, sort_keys=True),
                    "t": observed_at,
                },
            )
    return facts


def make_teacher_gate_handler(
    source: TeacherGateSource,
) -> Callable[[JobContext], Awaitable[None]]:
    async def handle(ctx: JobContext) -> None:
        try:
            site_id = UUID(str(ctx.args["site_id"]))
            cmid = int(ctx.args["cmid"])
            courseid = int(ctx.args["courseid"])
            kind = str(ctx.args["activity_type"])
        except (KeyError, ValueError):
            return  # a malformed job is a stale no-op
        if kind not in KINDS or cmid < 1 or courseid < 1:
            return
        provenance: dict[str, Any] = {}
        queued_by = ctx.args.get("queued_by")
        if queued_by in QUEUED_BY:
            async with ctx.acquire() as conn:
                queued_at = (
                    await conn.execute(_QUEUED_AT, {"id": ctx.job_id})
                ).scalar()
                await conn.rollback()
            if queued_at is not None:
                provenance = {"queued_by": queued_by, "queued_at": float(queued_at)}
        await refresh_teacher_gate(
            source, ctx.acquire, site_id, cmid, courseid, kind, provenance
        )

    return handle
