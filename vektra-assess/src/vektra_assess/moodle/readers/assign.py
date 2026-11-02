"""Assignment readers (plan group C): settings, effective dates, user flags, submission status,
grade release (BR-010).

Traps handled here:
- trap 1: ``mod_assign_get_assignments`` is always called with ``includenotenrolledcourses=1``
  (without it Moodle answers HTTP 200, ``courses: []`` and a warning) and every ``warnings[]``
  entry is Unreadable;
- trap 3: release comes from the grade report (``gradeishidden``) and ``gradingstatus``, NEVER
  from ``mod_assign_get_grades`` (which returns unreleased grades) nor from
  ``mod_quiz_get_user_best_grade`` (which ignores a hidden grade item). Those two functions are
  not used by any reader.
- ``mod_assign_get_user_flags`` needs ``mod/assign:grade``, which the minimal service role does
  not hold: Moodle then answers with an "access rights" warning, hence Unreadable. The student's
  extension is read from ``get_submission_status`` instead (``AssignCloseInputs``).
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from vektra_assess.moodle.client import MoodleWsClient, SiteRef
from vektra_assess.moodle.domain import (
    AssignCloseInputs,
    AssignReleaseState,
    AssignSettings,
    FeedbackView,
    GradeReleaseState,
    SubmissionFileRef,
    SubmissionStatus,
    UserFlag,
)
from vektra_assess.moodle.models.base import Strict
from vektra_assess.moodle.readers._support import fetch, reader
from vektra_assess.moodle.result import Readable, Reading, Unreadable, UnreadableReason


@reader
async def read_assign_settings(
    client: MoodleWsClient, site: SiteRef, courseid: int, assignid: int
) -> Reading[AssignSettings]:
    """Activity-level settings (duedate, cutoffdate, team submission, marking workflow...)."""
    res = await fetch(
        client,
        site,
        "mod_assign_get_assignments",
        {"courseids": [courseid], "includenotenrolledcourses": 1},
    )
    if isinstance(res, Unreadable):
        return res
    found = [
        a
        for c in res.value.courses
        if c.id == courseid
        for a in c.assignments
        if a.id == assignid
    ]
    if len(found) != 1:
        return Unreadable(UnreadableReason.PARTIAL, "assign_not_found")
    a = found[0]
    if a.grade > 0:
        grade_type, scale_id, grade_max = "point", None, float(a.grade)
    elif a.grade < 0:
        grade_type, scale_id, grade_max = "scale", -int(a.grade), None
    else:
        grade_type, scale_id, grade_max = "none", None, None
    return Readable(
        AssignSettings(
            assignid=a.id,
            cmid=a.cmid,
            courseid=a.course,
            name=a.name,
            duedate=a.duedate,
            cutoffdate=a.cutoffdate,
            gradingduedate=a.gradingduedate,
            allowsubmissionsfromdate=a.allowsubmissionsfromdate,
            teamsubmission=bool(a.teamsubmission),
            markingworkflow=bool(a.markingworkflow),
            grade_type=grade_type,
            scale_id=scale_id,
            grade_max=grade_max,
        ),
        res.evidence,
    )


class _FileRow(Strict):
    """One submission file. Written from Moodle's definition: team file lists were only ever
    recorded empty, so the generated models type them as ``list[Any]``."""

    filename: str
    filepath: str
    filesize: int
    fileurl: str
    isexternalfile: bool
    mimetype: str
    timemodified: int
    icon: str | None = None  # 5.x only


class _FlagRow(Strict):
    id: int
    userid: int
    locked: int
    mailed: int
    extensionduedate: int | None = None
    workflowstate: str | None = None
    allocatedmarker: int | None = None


class _FlagAssignment(Strict):
    assignmentid: int
    userflags: list[_FlagRow]


@reader
async def read_user_flags(
    client: MoodleWsClient, site: SiteRef, assignid: int
) -> Reading[tuple[UserFlag, ...]]:
    """All-user flags and extensions of one assignment (needs ``mod/assign:grade``).

    With the minimal service role (no grade capability) Moodle answers with a warning, which is
    Unreadable. The per-user extension is then taken from ``read_assign_close_inputs``.
    The row models are written from Moodle's external definition: the dev envs only ever
    returned the warning for this role, so no recording of a non-empty answer exists.
    """
    res = await fetch(
        client, site, "mod_assign_get_user_flags", {"assignmentids": [assignid]}
    )
    if isinstance(res, Unreadable):
        return res
    try:
        entries = [_FlagAssignment.model_validate(e) for e in res.value.assignments]
    except ValidationError:
        return Unreadable(UnreadableReason.UNKNOWN_SHAPE, "shape_mismatch")
    rows = tuple(
        UserFlag(
            userid=f.userid,
            extensionduedate=f.extensionduedate or None,
            locked=bool(f.locked),
            workflowstate=f.workflowstate,
        )
        for e in entries
        if e.assignmentid == assignid
        for f in e.userflags
    )
    return Readable(rows, res.evidence)


@reader
async def read_assign_close_inputs(
    client: MoodleWsClient, site: SiteRef, assignid: int, userid: int
) -> Reading[AssignCloseInputs]:
    """The EFFECTIVE dates for one student: ``get_participant`` applies the student's override
    (``get_assignments`` shows activity-level dates only); the extension comes from the
    student's submission status. 0 means "no date"."""
    part = await fetch(
        client,
        site,
        "mod_assign_get_participant",
        {"assignid": assignid, "userid": userid},
    )
    if isinstance(part, Unreadable):
        return part
    status = await _status(client, site, assignid, userid)
    if isinstance(status, Unreadable):
        return status
    p = part.value
    return Readable(
        AssignCloseInputs(
            assignid=assignid,
            userid=userid,
            duedate=p.duedate,
            cutoffdate=p.cutoffdate,
            allowsubmissionsfromdate=p.allowsubmissionsfromdate,
            extensionduedate=status.value.lastattempt.extensionduedate or None,
            granted_extension=p.grantedextension,
        ),
        part.evidence,
    )


async def _status(
    client: MoodleWsClient, site: SiteRef, assignid: int, userid: int
) -> Reading[Any]:
    return await fetch(
        client,
        site,
        "mod_assign_get_submission_status",
        {"assignid": assignid, "userid": userid},
    )


def _grade(text: str) -> float | None:
    return float(text) if text.strip() else None


@reader
async def read_submission_status(
    client: MoodleWsClient, site: SiteRef, assignid: int, userid: int
) -> Reading[SubmissionStatus]:
    """State, online text, file references, grading status and (when released) feedback."""
    res = await _status(client, site, assignid, userid)
    if isinstance(res, Unreadable):
        return res
    doc = res.value
    last = doc.lastattempt
    sub = last.submission
    team = last.teamsubmission
    if team is not None:
        # team assignment: the content lives in the team's submission (userid 0, a group)
        if team.assignment != assignid or team.groupid not in last.usergroups:
            return Unreadable(UnreadableReason.PARTIAL, "submission_owner_mismatch")
        content = team
    else:
        content = sub
        if sub is not None and (sub.userid != userid or sub.assignment != assignid):
            return Unreadable(UnreadableReason.PARTIAL, "submission_owner_mismatch")
    onlinetext: str | None = None
    files: list[SubmissionFileRef] = []
    if content is not None:
        for plugin in content.plugins:
            for ef in plugin.editorfields or []:
                if plugin.type == "onlinetext" and ef.name == "onlinetext":
                    onlinetext = ef.text
            for area in plugin.fileareas or []:
                for raw in area.files:
                    try:
                        f = _FileRow.model_validate(
                            raw if isinstance(raw, dict) else raw.model_dump()
                        )
                    except ValidationError:
                        return Unreadable(
                            UnreadableReason.UNKNOWN_SHAPE, "shape_mismatch"
                        )
                    files.append(
                        SubmissionFileRef(
                            filename=f.filename,
                            filepath=f.filepath,
                            size=f.filesize,
                            mimetype=f.mimetype,
                            fileurl=f.fileurl,
                            timemodified=f.timemodified,
                        )
                    )
    feedback: FeedbackView | None = None
    if doc.feedback is not None:
        comments = tuple(
            ef.text
            for p in doc.feedback.plugins
            for ef in (getattr(p, "editorfields", None) or [])
        )
        feedback = FeedbackView(
            grade_id=doc.feedback.grade.id,
            grade=_grade(doc.feedback.grade.grade),
            grade_for_display=doc.feedback.gradefordisplay,
            graded_date=doc.feedback.gradeddate,
            comments=comments,
        )
    return Readable(
        SubmissionStatus(
            assignid=assignid,
            userid=userid,
            submission_id=None if content is None else content.id,
            team_submission=team is not None,
            state=None if content is None else content.status,
            timemodified=None if content is None else content.timemodified,
            onlinetext=onlinetext,
            files=tuple(files),
            grading_status=last.gradingstatus,
            graded=last.graded,
            extensionduedate=last.extensionduedate or None,
            feedback=feedback,
        ),
        res.evidence,
    )


@reader
async def read_grade_release(
    client: MoodleWsClient, site: SiteRef, courseid: int, userid: int, cmid: int
) -> Reading[GradeReleaseState]:
    """Release state of one activity's grade item for one student, from the grade report.

    Works for assignments and quizzes alike. When the item is hidden (``gradeishidden``, which
    covers hidden-until) no value is copied out: an unreleased grade never leaves the reader.
    """
    res = await fetch(
        client,
        site,
        "gradereport_user_get_grade_items",
        {"courseid": courseid, "userid": userid},
    )
    if isinstance(res, Unreadable):
        return res
    users = [
        u for u in res.value.usergrades if u.userid == userid and u.courseid == courseid
    ]
    if len(users) != 1:
        return Unreadable(UnreadableReason.PARTIAL, "user_not_in_report")
    items = [i for i in users[0].gradeitems if i.cmid == cmid and i.itemtype == "mod"]
    if len(items) != 1:
        return Unreadable(UnreadableReason.PARTIAL, "grade_item_not_found")
    i = items[0]
    hidden = i.gradeishidden or i.gradehiddenbydate
    return Readable(
        GradeReleaseState(
            cmid=cmid,
            itemmodule=str(i.itemmodule),
            hidden=hidden,
            hidden_by_date=i.gradehiddenbydate,
            locked=i.gradeislocked,
            grade=None if hidden or i.graderaw is None else float(i.graderaw),
            grade_formatted=None if hidden else i.gradeformatted,
            grade_max=float(i.grademax),
            range_formatted=i.rangeformatted,
            feedback=None if hidden else i.feedback,
        ),
        res.evidence,
    )


@reader
async def read_assign_release(
    client: MoodleWsClient, site: SiteRef, courseid: int, assignid: int, userid: int
) -> Reading[AssignReleaseState]:
    """BR-010 for an assignment: grade item visible, graded, and with a marking workflow the
    workflow state is ``released``."""
    settings = await read_assign_settings(client, site, courseid, assignid)
    if isinstance(settings, Unreadable):
        return settings
    status = await read_submission_status(client, site, assignid, userid)
    if isinstance(status, Unreadable):
        return status
    item = await read_grade_release(client, site, courseid, userid, settings.value.cmid)
    if isinstance(item, Unreadable):
        return item
    workflow = settings.value.markingworkflow
    released = (
        not item.value.hidden
        and status.value.graded
        and (not workflow or status.value.grading_status == "released")
    )
    return Readable(
        AssignReleaseState(
            assignid=assignid,
            userid=userid,
            grade_item=item.value,
            grading_status=status.value.grading_status,
            marking_workflow=workflow,
            released=released,
        ),
        item.evidence,
    )
