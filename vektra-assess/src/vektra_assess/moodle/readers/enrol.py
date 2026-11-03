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


@reader
async def read_graders(
    client: MoodleWsClient, site: SiteRef, courseid: int
) -> Reading[GraderList]:
    """Users holding ``mod/assign:grade`` in the course (REQ-082 transcript recipients).

    Needs ``moodle/role:review``. Moodle returns NO email address to the service account (it
    has no ``moodle/user:viewalldetails``), so ``GraderView.email`` is None: the delivery
    address has to come from somewhere else.
    """
    res = await fetch(
        client,
        site,
        "core_enrol_get_enrolled_users_with_capability",
        {
            "coursecapabilities": [
                {"courseid": courseid, "capabilities": [GRADE_CAPABILITY]}
            ]
        },
    )
    if isinstance(res, Unreadable):
        return res
    rows = [
        r
        for r in res.value
        if r.courseid == courseid and r.capability == GRADE_CAPABILITY
    ]
    if len(rows) != 1:
        return Unreadable(UnreadableReason.PARTIAL, "capability_row_missing")
    graders = tuple(
        GraderView(
            userid=u.id,
            fullname=u.fullname,
            email=None,
            roles=tuple(r.shortname for r in u.roles),
        )
        for u in rows[0].users
    )
    return Readable(GraderList(courseid, graders), res.evidence)
