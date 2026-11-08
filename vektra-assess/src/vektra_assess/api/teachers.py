"""Is this teacher an EDITOR of this activity, NOW? (plan assess-teacher-ops, INT-001, REQ-077.)

The plugin names the acting Moodle user (``actor_userid``); Vektra never trusts that claim on its
own. It reads the activity-level facts the worker last wrote into ``assess_teacher_gate`` for
(site, cmid) (``worker.teacher_gate``: the cmid resolved inside the hinted course, its editors,
graders and REQ-077 enable facts) and accepts them only while at most ``max_age_s`` old.

- absent, stale, or the editor list unreadable: refused (fail closed) and a ``teacher_gate`` job
  is queued for the given course / type hint, so a retry a few seconds later can succeed; with
  no usable hint nothing can be queued: ``HINT_MISSING`` (the route answers 400, it never
  promises a check that was not queued);
- fresh but the cmid was not found in the hinted course (``activity_not_in_course``, the one
  definitive unresolved answer): refused, nothing queued; when the hint differs from the one that
  failed, or the row records a transient failure (site, reader, Moodle), a refresh is queued and
  the request is told to retry (a wrong hint never blocks a right one for long);
- fresh and the actor is not listed: refused, nothing queued (Moodle must change first).

``require_fresh_grader`` and ``can_view_overrides`` (the list, reset, terminate, extra time and
opening-fallback routes) arrive with those routes in 11b; the grader check of the transcript
routes stays ``api.graders`` (per student, plan 10).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import text

from vektra_assess.jobs import JobKind, enqueue_job, teacher_gate_dedupe_key

KINDS = ("quiz", "assign")
#: the only unresolved outcome that is an answer: the worker read the hinted course and the cmid
#: is not among its activities of that type (``moodle.readers.resolve.resolve_in_course``). Every
#: other unresolved reason (site unknown, reader error, Moodle unreachable) is transient: retried.
NOT_IN_COURSE = "activity_not_in_course"

_GATE = text(
    "SELECT inputs, extract(epoch FROM now() - refreshed_at) AS age_s "
    "FROM assess_teacher_gate WHERE site_id = :s AND cmid = :c"
)


class TeacherCheck(StrEnum):
    OK = "ok"
    NOT_AN_EDITOR = "not_an_editor"
    STALE = "stale"  # absent, older than the bound, or unreadable: refresh queued
    # a refresh is needed but the request gives no usable course / type hint: nothing could be
    # queued, so the caller must not be told to retry (400, not "a check has been queued")
    HINT_MISSING = "hint_missing"


@dataclass(frozen=True)
class TeacherGate:
    """The outcome and, when fresh, the facts (ids and flags only)."""

    check: TeacherCheck
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.check is TeacherCheck.OK


def usable_hint(courseid: int | None, kind: str | None) -> bool:
    return bool(courseid) and int(courseid or 0) >= 1 and kind in KINDS


async def queue_teacher_gate(
    conn: Any, site: UUID, cmid: int, courseid: int | None, kind: str | None
) -> bool:
    """Queue a refresh for this hint (False: a refresh for it is already pending). The caller
    checks ``usable_hint`` first: without one nothing can be resolved."""
    if courseid is None or kind is None or not usable_hint(courseid, kind):
        raise ValueError("a teacher_gate refresh needs a course and type hint")
    return await enqueue_job(
        conn,
        JobKind.TEACHER_GATE,
        {
            "site_id": str(site),
            "cmid": int(cmid),
            "courseid": int(courseid),
            "activity_type": str(kind),
        },
        dedupe_key=teacher_gate_dedupe_key(site, cmid, int(courseid), str(kind)),
    )


async def check_fresh_editor(
    conn: Any,
    *,
    site: UUID,
    cmid: int,
    actor_userid: int,
    max_age_s: int,
    courseid_hint: int | None,
    kind_hint: str | None,
) -> TeacherGate:
    row = (await conn.execute(_GATE, {"s": site, "c": int(cmid)})).first()
    facts = dict(row.inputs) if row is not None and isinstance(row.inputs, dict) else {}
    fresh = row is not None and row.age_s is not None and float(row.age_s) <= max_age_s

    async def refresh() -> TeacherGate:
        if not usable_hint(courseid_hint, kind_hint):
            return TeacherGate(TeacherCheck.HINT_MISSING)
        await queue_teacher_gate(conn, site, cmid, courseid_hint, kind_hint)
        return TeacherGate(TeacherCheck.STALE)

    if not fresh:
        return await refresh()
    if facts.get("resolved") is not True:
        # a definitive answer for THIS hint: not an editor of anything here. Another hint, or a
        # transient failure: ask again (dedupe per hint)
        if (
            facts.get("hint") == [courseid_hint, kind_hint]
            and facts.get("reason") == NOT_IN_COURSE
        ):
            return TeacherGate(TeacherCheck.NOT_AN_EDITOR)
        return await refresh()
    editors = facts.get("editors")
    if not isinstance(editors, list):
        return await refresh()
    if int(actor_userid) not in {int(e) for e in editors if isinstance(e, int)}:
        return TeacherGate(TeacherCheck.NOT_AN_EDITOR, facts)
    return TeacherGate(TeacherCheck.OK, facts)
