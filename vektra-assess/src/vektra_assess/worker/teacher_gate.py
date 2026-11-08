"""The activity-level facts a teacher request is verified against (plan assess-teacher-ops 11a).

The API cannot ask Moodle (only the worker holds the WS token). A ``teacher_gate`` job (args:
site id, cmid, course id hint, activity type hint) resolves the cmid INSIDE the hinted course
(``resolve_in_course``: a cmid of another course is never found there) and upserts
``assess_teacher_gate(site_id, cmid)`` with:

- ``resolved``: whether the cmid was found in that course as that type (False: every fact absent,
  and the API refuses the actor; a wrong hint never verifies anybody); ``hint``: the
  ``[courseid, type]`` this row answers;
- ``courseid``, ``activity_type``;
- ``editors``: user ids holding ``moodle/course:manageactivities`` in the course (None when
  unreadable: the API fails closed); the graders are added by 11b with the routes that read them;
- the REQ-077 enable facts: ``close_at`` (quiz ``timeclose`` / assignment ``cutoffdate``, 0 = none
  configured: BR-007), ``team_submission``, ``grading_method``, ``moodle_rubric`` (a rubric is
  defined for the Moodle-rubric source); each None when unreadable.

Nothing else: no names, no addresses, no content. The API accepts the row only while it is at
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


# ``refreshed_at`` is the database clock read BEFORE the Moodle reads (the API compares it with
# the same clock): a slow read never makes its list look fresher than it is
_CLOCK = text("SELECT clock_timestamp()")
_UPSERT = text(
    "INSERT INTO assess_teacher_gate (site_id, cmid, inputs, refreshed_at) "
    "VALUES (:s, :c, CAST(:i AS jsonb), :t) ON CONFLICT (site_id, cmid) "
    "DO UPDATE SET inputs = EXCLUDED.inputs, refreshed_at = EXCLUDED.refreshed_at"
)


async def refresh_teacher_gate(
    source: TeacherGateSource,
    acquire: AcquireDb,
    site_id: UUID,
    cmid: int,
    courseid: int,
    kind: str,
) -> dict[str, Any]:
    """Read the facts fresh and store them, dated when the reads began; returns what was
    stored."""
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
        await refresh_teacher_gate(source, ctx.acquire, site_id, cmid, courseid, kind)

    return handle
