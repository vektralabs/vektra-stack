"""Is this teacher a grader of this activity, NOW? (plan 10; reused by plan 11.)

The API cannot ask Moodle (only the worker holds the WS token). It reads the grader user ids the
render worker last wrote into ``assess_gate_inputs`` for (site, student, cmid) and accepts them
only when they are at most ``max_age_s`` old. Absent, stale or not listed: refused (fail closed),
and when absent or stale a render check is queued so a retry a few seconds later can succeed.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import text

_GRADERS = text(
    "SELECT inputs->'graders' AS graders, "
    "extract(epoch FROM now() - refreshed_at) AS age_s FROM assess_gate_inputs "
    "WHERE site_id = :s AND student_userid = :u AND cmid = :c"
)


class GraderCheck(StrEnum):
    OK = "ok"
    NOT_A_GRADER = "not_a_grader"
    STALE = "stale"  # absent or older than the bound: refresh queued


async def check_fresh_grader(
    conn: Any,
    *,
    site: UUID,
    student_userid: int,
    cmid: int,
    actor_userid: int,
    max_age_s: int,
    courseid_hint: int | None = None,
) -> GraderCheck:
    row = (
        await conn.execute(_GRADERS, {"s": site, "u": student_userid, "c": cmid})
    ).first()
    graders = row.graders if row is not None else None
    if (
        row is None
        or not isinstance(graders, list)
        or row.age_s is None
        or float(row.age_s) > max_age_s
    ):
        from vektra_assess.api.state import queue_render_check

        await queue_render_check(conn, site, student_userid, cmid, courseid_hint)
        return GraderCheck.STALE
    if actor_userid == student_userid or actor_userid not in {
        int(g) for g in graders if isinstance(g, int)
    }:
        return GraderCheck.NOT_A_GRADER
    return GraderCheck.OK
