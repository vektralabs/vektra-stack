"""The production ``StartSource``: Moodle readers + parser sidecar -> snapshot (plan 8 pre-flight).

For one STARTING session it reads, fresh and server-side only:

- the site (base URL, release) and its two ARCH-070 capability flags from the database;
- the teacher configuration view: ``flags.show_solution`` / ``flags.rubric_source`` (plan 11 owns
  the form that writes them; until then the defaults are the safe ones, solution OFF), and the
  solution text only when the toggle is ON and a sealed value exists (OQ-034);
- the activity's course and instance, resolved from the cmid inside the student's own courses
  (``resolve_activity``; the plugin's course id is only a hint, REQ-095);

then runs ``capture_quiz`` / ``capture_assign`` (plan 6), which selects the attempt server-side
(BR-012) and projects it through the disclosure policy. Client-sent attempt ids do not exist on
this path at all.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vektra_assess.context.capture.capture import (
    AssignTarget,
    Capabilities,
    CaptureParser,
    CaptureResult,
    Failed,
    FailReason,
    QuizTarget,
    TeacherConfigView,
    capture_assign,
    capture_quiz,
)
from vektra_assess.context.capture.readers import MoodleCaptureReaders
from vektra_assess.crypto.teacher_config import open_teacher_config
from vektra_assess.keys import AssessKeyring
from vektra_assess.moodle import readers as r
from vektra_assess.moodle.client import MoodleWsClient, SiteRef
from vektra_assess.moodle.domain import AttemptState
from vektra_assess.moodle.readers.resolve import ActivityRef, resolve_activity
from vektra_assess.moodle.result import Readable, Unreadable
from vektra_assess.worker.preflight import MoodleGateFacts, StartTarget

_SITE = text("SELECT base_url, release FROM assess_sites WHERE id = :s")
_CAPS = text(
    "SELECT flag, state FROM assess_site_capabilities WHERE site_id = :s "
    "AND flag IN ('review_html_filterable', 'rubric_display_settings')"
)
_SEALED = text(
    "SELECT teacher_config_sealed FROM assess_activity_config WHERE site_id = :s AND cmid = :c"
)
SOLUTION_FIELD = "solution"


def teacher_view(
    flags: dict[str, Any], kind: str, solution_text: str | None
) -> TeacherConfigView:
    show = flags.get("show_solution") is True
    source = flags.get("rubric_source")
    if source not in ("none", "pasted", "moodle"):
        source = "moodle" if kind == "assign" else "none"
    return TeacherConfigView(
        show_solution=show,
        rubric_source=str(source),
        solution_text=solution_text if show else None,
    )


class MoodleStartSource:
    def __init__(
        self,
        client: MoodleWsClient,
        parser: CaptureParser,
        acquire: Callable[[], AbstractAsyncContextManager[AsyncConnection]],
        *,
        teacher_private: AssessKeyring | None = None,
    ) -> None:
        self._client = client
        self._parser = parser
        self._acquire = acquire
        self._teacher_private = teacher_private
        # the course and instance resolved by ``gate_facts``, reused by ``capture`` (same Start)
        self._resolved: dict[object, ActivityRef] = {}

    async def _site(self, target: StartTarget) -> SiteRef | None:
        async with self._acquire() as conn:
            site = (await conn.execute(_SITE, {"s": target.site_id})).first()
        if site is None or not site.release:
            return None
        return SiteRef(str(target.site_id), str(site.base_url), str(site.release))

    async def _resolve(
        self, site: SiteRef, target: StartTarget
    ) -> tuple[ActivityRef | None, bool]:
        """(the activity, or None; and whether None means "not in the student's courses").

        Per call: the source is shared by every concurrent pre-flight of the worker."""
        ref = self._resolved.get(target.session_id)
        if ref is not None:
            return ref, False
        kind = "quiz" if target.activity_type == "quiz" else "assign"
        res = await resolve_activity(
            self._client,
            site,
            userid=target.student_userid,
            cmid=target.cmid,
            kind=kind,
            courseid_hint=target.courseid_hint,
        )
        if isinstance(res, Unreadable):
            return None, res.detail_code == "activity_not_in_student_courses"
        self._resolved[target.session_id] = res.value
        return res.value, False

    async def gate_facts(self, target: StartTarget) -> MoodleGateFacts:
        """The Moodle half of the START gates, read fresh; anything unreadable stays None."""
        site = await self._site(target)
        if site is None:
            return MoodleGateFacts()
        ref, not_found = await self._resolve(site, target)
        if ref is None:
            # NOT FOUND in the student's active courses: not enrolled (state 7); the course list
            # unreadable: every fact unknown. Both fail closed.
            return MoodleGateFacts(enrolled=False if not_found else None)
        c, uid = self._client, target.student_userid
        enrol = await r.read_enrolment(c, site, ref.courseid, uid)
        enrolled = enrol.value.is_student if isinstance(enrol, Readable) else None
        if target.activity_type == "quiz":
            return await self._quiz_facts(site, ref, target, enrolled)
        return await self._assign_facts(site, ref, target, enrolled)

    async def _quiz_facts(
        self, site: SiteRef, ref: ActivityRef, t: StartTarget, enrolled: bool | None
    ) -> MoodleGateFacts:
        c, uid = self._client, t.student_userid
        close = await r.read_quiz_close_inputs(c, site, ref.courseid, ref.instanceid)
        attempts = await r.read_user_attempts(c, site, ref.instanceid, uid)
        release = await r.read_grade_release(c, site, ref.courseid, uid, t.cmid)
        activity_close = grace = participants = own = None
        if isinstance(close, Readable):
            q = close.value
            activity_close = q.timeclose
            if isinstance(q.grace, Readable):
                g = q.grace.value
                grace = g.graceperiod_s if g.overduehandling == "graceperiod" else 0
            if isinstance(q.overrides, Readable):
                participants = tuple(
                    o.timeclose for o in q.overrides.value if o.timeclose
                )
                mine = [
                    o.timeclose
                    for o in q.overrides.value
                    if o.userid == uid and o.timeclose
                ]
                own = max(mine) if mine else None
        in_progress = submitted = graded = None
        if isinstance(attempts, Readable):
            real = [a for a in attempts.value if not a.preview]
            in_progress = any(
                a.state in (AttemptState.IN_PROGRESS, AttemptState.OVERDUE)
                for a in real
            )
            finished = [a for a in real if a.state is AttemptState.FINISHED]
            submitted = bool(finished)
            # the cheap signal; capture re-checks every item of the selected attempt (BR-014)
            graded = bool(finished) and all(a.sumgrades is not None for a in finished)
        released = (
            not release.value.hidden and not release.value.hidden_by_date
            if isinstance(release, Readable)
            else None
        )
        return MoodleGateFacts(
            attempt_in_progress=in_progress,
            has_submitted_attempt=submitted,
            # a question mid-regrade is not in a final state: the per-item BR-014 check of the
            # capture step catches it; here it is not separately readable
            regrade_in_progress=False if graded is not None else None,
            fully_graded=graded,
            released=released,
            activity_close_at=activity_close,
            grace_s=grace,
            participant_closes=participants,
            own_close_at=own,
            owner_verified=True,  # attempts are listed for the token's own user id (BR-012)
            enrolled=enrolled,
        )

    async def _assign_facts(
        self, site: SiteRef, ref: ActivityRef, t: StartTarget, enrolled: bool | None
    ) -> MoodleGateFacts:
        c, uid = self._client, t.student_userid
        settings = await r.read_assign_settings(c, site, ref.courseid, ref.instanceid)
        grading = await r.read_grading_view(c, site, t.cmid)
        dates = await r.read_assign_close_inputs(c, site, ref.instanceid, uid)
        status = await r.read_submission_status(c, site, ref.instanceid, uid)
        release = await r.read_assign_release(
            c, site, ref.courseid, ref.instanceid, uid
        )
        team = cutoff = None
        if isinstance(settings, Readable):
            team = settings.value.teamsubmission
            cutoff = settings.value.cutoffdate
        own = None
        if isinstance(dates, Readable) and cutoff is not None:
            mine = max(dates.value.cutoffdate, dates.value.extensionduedate or 0)
            own = mine if mine > cutoff else None
        in_progress = submitted = graded = None
        if isinstance(status, Readable):
            st = status.value
            in_progress = st.state in ("draft", "reopened")
            submitted = st.state == "submitted" or bool(st.files) or bool(st.onlinetext)
            graded = st.graded  # capture re-checks the rubric levels (BR-014)
        return MoodleGateFacts(
            team_submission=team,
            grading_method=grading.value.method
            if isinstance(grading, Readable)
            else None,
            attempt_in_progress=in_progress,
            has_submitted_attempt=submitted,
            regrade_in_progress=False if graded is not None else None,
            fully_graded=graded,
            released=release.value.released if isinstance(release, Readable) else None,
            activity_close_at=cutoff,
            grace_s=0,
            # the user overrides and extensions of ALL participants are not listable through the
            # WS with the minimal role (OQ-025): the REQ-091 fallback always applies here
            participant_closes=None,
            own_close_at=own,
            owner_verified=True,  # the submission is read for the token's own user id
            enrolled=enrolled,
        )

    async def _context(
        self, target: StartTarget
    ) -> tuple[SiteRef, Capabilities, str | None] | None:
        async with self._acquire() as conn:
            site = (await conn.execute(_SITE, {"s": target.site_id})).first()
            if site is None or not site.release:
                return None
            caps = {
                r.flag: r.state
                for r in (await conn.execute(_CAPS, {"s": target.site_id})).all()
            }
            solution = None
            if (
                target.flags.get("show_solution") is True
                and self._teacher_private is not None
            ):
                sealed = (
                    await conn.execute(_SEALED, {"s": target.site_id, "c": target.cmid})
                ).scalar()
                if sealed is not None:
                    solution = open_teacher_config(
                        self._teacher_private,
                        bytes(sealed),
                        target.site_id,
                        target.cmid,
                        SOLUTION_FIELD,
                    )
        ref = SiteRef(str(target.site_id), str(site.base_url), str(site.release))
        capabilities = Capabilities(
            review_html_filterable=caps.get("review_html_filterable", "unreadable"),
            rubric_display_settings=caps.get("rubric_display_settings", "unreadable"),
        )
        return ref, capabilities, solution

    async def capture(self, target: StartTarget) -> CaptureResult:
        ctx = await self._context(target)
        if ctx is None:
            return Failed(FailReason.POLICY_INPUT_UNREADABLE, "site_release_unknown")
        site, caps, solution = ctx
        kind = "quiz" if target.activity_type == "quiz" else "assign"
        ref, _not_found = await self._resolve(site, target)
        self._resolved.pop(target.session_id, None)
        if ref is None:
            return Failed(FailReason.POLICY_INPUT_UNREADABLE, "activity_unresolved")
        readers = MoodleCaptureReaders(self._client, site)
        teacher = teacher_view(target.flags, kind, solution)
        release = str(site.release)
        if kind == "quiz":
            return await capture_quiz(
                QuizTarget(
                    ref.courseid, ref.instanceid, target.cmid, target.student_userid
                ),
                readers,
                self._parser,
                teacher,
                caps,
                moodle_release=release,
            )
        return await capture_assign(
            AssignTarget(
                ref.courseid, ref.instanceid, target.cmid, target.student_userid
            ),
            readers,
            self._parser,
            teacher,
            caps,
            moodle_release=release,
        )
