"""Resolve a course module id to (course, activity instance) for one student (plan 8 pre-flight).

The discussion token carries only the cmid; the plugin's course id is a HINT (REQ-095: Moodle ids
sent by a client are re-resolved). The module is looked up only inside the courses the student is
enrolled in (``core_enrol_get_users_courses``), the hinted course first, through the same
functions the capture readers already use (``mod_quiz_get_quizzes_by_courses``,
``mod_assign_get_assignments``). A module the student cannot reach this way is Unreadable
(``activity_not_in_student_courses``): the pre-flight rolls the Start back, it never guesses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from vektra_assess.moodle.client import MoodleWsClient, SiteRef
from vektra_assess.moodle.readers._support import fetch, reader
from vektra_assess.moodle.readers.enrol import read_user_courses
from vektra_assess.moodle.result import Readable, Reading, Unreadable, UnreadableReason

ActivityKind = Literal["quiz", "assign"]


@dataclass(frozen=True)
class ActivityRef:
    courseid: int
    instanceid: int  # quiz id or assignment id
    #: the course's short name, from the same course list the resolution read (plan 10: the
    #: transcript file name); not part of the identity
    course_shortname: str | None = field(default=None, compare=False)


async def _find(
    client: MoodleWsClient, site: SiteRef, kind: ActivityKind, courseid: int, cmid: int
) -> Reading[int | None]:
    if kind == "quiz":
        res = await fetch(
            client, site, "mod_quiz_get_quizzes_by_courses", {"courseids": [courseid]}
        )
        if isinstance(res, Unreadable):
            return res
        quizzes = [q.id for q in res.value.quizzes if q.coursemodule == cmid]
        return Readable(quizzes[0] if len(quizzes) == 1 else None, res.evidence)
    res = await fetch(
        client,
        site,
        "mod_assign_get_assignments",
        {"courseids": [courseid], "includenotenrolledcourses": 1},
    )
    if isinstance(res, Unreadable):
        return res
    found = [
        a.id
        for c in res.value.courses
        if c.id == courseid
        for a in c.assignments
        if a.cmid == cmid
    ]
    return Readable(found[0] if len(found) == 1 else None, res.evidence)


@reader
async def resolve_activity(
    client: MoodleWsClient,
    site: SiteRef,
    *,
    userid: int,
    cmid: int,
    kind: ActivityKind,
    courseid_hint: int | None = None,
) -> Reading[ActivityRef]:
    courses = await read_user_courses(client, site, userid)
    if isinstance(courses, Unreadable):
        return courses
    ids = [c.courseid for c in courses.value]
    order = ([courseid_hint] if courseid_hint in ids else []) + [
        c for c in ids if c != courseid_hint
    ]
    for courseid in order:
        found = await _find(client, site, kind, courseid, cmid)
        if isinstance(found, Unreadable):
            return found  # fail closed: an unreadable course is never skipped
        if found.value is not None:
            short = next(
                (c.shortname for c in courses.value if c.courseid == courseid), None
            )
            return Readable(ActivityRef(courseid, found.value, short), found.evidence)
    return Unreadable(UnreadableReason.PARTIAL, "activity_not_in_student_courses")
