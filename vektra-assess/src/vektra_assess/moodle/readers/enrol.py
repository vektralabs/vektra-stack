"""Enrolment and grader readers (plan group C): NFR-014 re-check, REQ-082 recipients."""

from __future__ import annotations

from vektra_assess.moodle.client import MoodleWsClient, SiteRef
from vektra_assess.moodle.domain import (
    CourseEnrolment,
    CourseParticipant,
    EnrolmentView,
    GraderList,
    GraderView,
)
from vektra_assess.moodle.readers._support import fetch, reader
from vektra_assess.moodle.result import Readable, Reading, Unreadable, UnreadableReason

GRADE_CAPABILITY = "mod/assign:grade"
#: REQ-077 "activity editors only": who may change the discussion configuration (plan 11)
EDIT_CAPABILITY = "moodle/course:manageactivities"
#: REQ-076 gate panel (plan 11b): who may see which participant holds a quiz override
OVERRIDE_CAPABILITY = "mod/quiz:manageoverrides"


@reader
async def read_user_courses(
    client: MoodleWsClient, site: SiteRef, userid: int
) -> Reading[tuple[CourseEnrolment, ...]]:
    """The courses a user is enrolled in (``core_enrol_get_users_courses``)."""
    res = await fetch(client, site, "core_enrol_get_users_courses", {"userid": userid})
    if isinstance(res, Unreadable):
        return res
    return Readable(
        tuple(CourseEnrolment(c.id, c.shortname, c.hidden) for c in res.value),
        res.evidence,
    )


@reader
async def read_course_participants(
    client: MoodleWsClient, site: SiteRef, courseid: int
) -> Reading[tuple[CourseParticipant, ...]]:
    """Every enrolled user and their role shortnames (suspended enrolments included).

    The ``onlyactive`` option needs ``moodle/course:enrolreview`` (the minimal role answers
    ``nopermissions``), and the payload has no status field: use ``read_enrolment`` for activity.
    """
    res = await fetch(
        client, site, "core_enrol_get_enrolled_users", {"courseid": courseid}
    )
    if isinstance(res, Unreadable):
        return res
    return Readable(
        tuple(
            CourseParticipant(
                u.id, u.fullname, tuple(r.shortname for r in (u.roles or []))
            )
            for u in res.value
        ),
        res.evidence,
    )


@reader
async def read_enrolment(
    client: MoodleWsClient, site: SiteRef, courseid: int, userid: int
) -> Reading[EnrolmentView]:
    """Is ``userid`` an ACTIVE student of ``courseid`` right now (the NFR-014 re-check)?

    - activity: ``core_enrol_get_users_courses`` lists only ACTIVE enrolments (Moodle's
      ``enrol_get_users_courses(onlyactive=true)``); the course missing from that list is
      ``active=False`` (suspended or not enrolled), not Unreadable;
    - role: the role names of ``core_enrol_get_enrolled_users``.
    """
    courses = await read_user_courses(client, site, userid)
    if isinstance(courses, Unreadable):
        return courses
    participants = await read_course_participants(client, site, courseid)
    if isinstance(participants, Unreadable):
        return participants
    active = any(c.courseid == courseid for c in courses.value)
    roles = tuple(
        sorted({r for p in participants.value if p.userid == userid for r in p.roles})
    )
    return Readable(
        EnrolmentView(
            courseid=courseid,
            userid=userid,
            active=active,
            is_student=active and "student" in roles,
            roles=roles,
        ),
        courses.evidence,
    )


async def _with_capability(
    client: MoodleWsClient, site: SiteRef, courseid: int, capability: str
) -> Reading[GraderList]:
    res = await fetch(
        client,
        site,
        "core_enrol_get_enrolled_users_with_capability",
        {"coursecapabilities": [{"courseid": courseid, "capabilities": [capability]}]},
    )
    if isinstance(res, Unreadable):
        return res
    rows = [
        r for r in res.value if r.courseid == courseid and r.capability == capability
    ]
    if len(rows) != 1:
        return Unreadable(UnreadableReason.PARTIAL, "capability_row_missing")
    users = tuple(
        GraderView(
            userid=u.id,
            fullname=u.fullname,
            email=None,
            roles=tuple(r.shortname for r in u.roles),
        )
        for u in rows[0].users
    )
    return Readable(GraderList(courseid, users), res.evidence)


@reader
async def read_graders(
    client: MoodleWsClient, site: SiteRef, courseid: int
) -> Reading[GraderList]:
    """Users holding ``mod/assign:grade`` in the course (REQ-082 transcript recipients).

    Needs ``moodle/role:review``. Moodle returns NO email address to the service account (it
    has no ``moodle/user:viewalldetails``), so ``GraderView.email`` is None: the delivery
    address has to come from somewhere else.
    """
    return await _with_capability(client, site, courseid, GRADE_CAPABILITY)


@reader
async def read_editors(
    client: MoodleWsClient, site: SiteRef, courseid: int
) -> Reading[GraderList]:
    """Users holding ``moodle/course:manageactivities`` in the course: the activity editors who
    may change the discussion configuration (REQ-077, plan assess-teacher-ops). Same WS function
    and the same ``moodle/role:review`` as ``read_graders``: no new capability for the service
    account (REQ-079)."""
    return await _with_capability(client, site, courseid, EDIT_CAPABILITY)


@reader
async def read_override_managers(
    client: MoodleWsClient, site: SiteRef, courseid: int
) -> Reading[GraderList]:
    """Users holding ``mod/quiz:manageoverrides`` in the course: the teachers who may see which
    participant holds an override (REQ-076 gate panel, plan assess-teacher-ops 11b). Same WS
    function and ``moodle/role:review`` as ``read_graders``: no new service-account capability
    (REQ-079)."""
    return await _with_capability(client, site, courseid, OVERRIDE_CAPABILITY)
