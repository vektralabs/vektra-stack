"""The production ``StartSource``: Moodle readers + parser sidecar -> snapshot (plan 8 pre-flight).

For one STARTING session it reads, fresh and server-side only:

- the site (base URL, release) and its two ARCH-070 capability flags from the database;
- the teacher configuration view: ``flags.show_solution`` / ``flags.rubric_source`` (plan 11's
  config PUT writes them; absent, the defaults are the safe ones, solution OFF), the solution
  text only when the toggle is ON and a sealed value exists (OQ-034), and the pasted rubric only
  when the rubric source is ``pasted`` (each sealed field opened on its own, revision 0036);
- the activity's course and instance, resolved from the cmid inside the student's own courses
  (``resolve_activity``; the plugin's course id is only a hint, REQ-095);

then runs ``capture_quiz`` / ``capture_assign`` (plan 6), which selects the attempt server-side
(BR-012) and projects it through the disclosure policy. Client-sent attempt ids do not exist on
this path at all.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from uuid import UUID

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
from vektra_assess.context.regrade import GradeReading
from vektra_assess.context.snapshot import mark_str
from vektra_assess.crypto.teacher_config import (
    PASTED_RUBRIC,
    SOLUTION,
    read_teacher_config,
)
from vektra_assess.keys import AssessKeyring
from vektra_assess.moodle import readers as r
from vektra_assess.moodle.client import MoodleWsClient, SiteRef
from vektra_assess.moodle.domain import AttemptState, GraderView
from vektra_assess.moodle.readers.resolve import (
    ActivityRef,
    resolve_activity,
    resolve_in_course,
)
from vektra_assess.moodle.result import Readable, Unreadable
from vektra_assess.transcript.facts import CloseFacts
from vektra_assess.worker.preflight import MoodleGateFacts, StartTarget

_SITE = text("SELECT base_url, release FROM assess_sites WHERE id = :s")
_CAPS = text(
    "SELECT flag, state FROM assess_site_capabilities WHERE site_id = :s "
    "AND flag IN ('review_html_filterable', 'rubric_display_settings')"
)


class _BoundedCache:
    """key -> ActivityRef with a size bound (oldest out) and a TTL (review fix 6)."""

    def __init__(self, max_size: int, ttl_s: float, clock: Callable[[], float]) -> None:
        self._max = max_size
        self._ttl = ttl_s
        self._clock = clock
        self._items: OrderedDict[object, tuple[ActivityRef, float]] = OrderedDict()

    def get(self, key: object) -> ActivityRef | None:
        item = self._items.get(key)
        if item is None:
            return None
        if self._clock() >= item[1]:
            del self._items[key]
            return None
        return item[0]

    def put(self, key: object, value: ActivityRef) -> None:
        self.sweep()
        self._items[key] = (value, self._clock() + self._ttl)
        self._items.move_to_end(key)
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def pop(self, key: object) -> None:
        self._items.pop(key, None)

    def sweep(self) -> int:
        now = self._clock()
        expired = [k for k, (_v, exp) in self._items.items() if now >= exp]
        for k in expired:
            del self._items[k]
        return len(expired)

    def __len__(self) -> int:
        return len(self._items)


def rubric_source_of(flags: dict[str, Any], kind: str) -> str:
    source = flags.get("rubric_source")
    if source not in ("none", "pasted", "moodle"):
        source = "moodle" if kind == "assign" else "none"
    return str(source)


def teacher_view(
    flags: dict[str, Any],
    kind: str,
    solution_text: str | None,
    pasted_rubric: str | None = None,
) -> TeacherConfigView:
    show = flags.get("show_solution") is True
    source = rubric_source_of(flags, kind)
    return TeacherConfigView(
        show_solution=show,
        rubric_source=source,
        solution_text=solution_text if show else None,
        pasted_rubric=pasted_rubric if source == "pasted" else None,
    )


class MoodleStartSource:
    def __init__(
        self,
        client: MoodleWsClient,
        parser: CaptureParser,
        acquire: Callable[[], AbstractAsyncContextManager[AsyncConnection]],
        *,
        teacher_private: AssessKeyring | None = None,
        resolved_max: int = 256,
        resolved_ttl_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._parser = parser
        self._acquire = acquire
        self._teacher_private = teacher_private
        # the course and instance resolved by ``gate_facts``, reused by ``capture`` (same Start).
        # Bounded: a render check, or a Start a gate refused, never reaches ``capture``.
        self._resolved = _BoundedCache(resolved_max, resolved_ttl_s, clock)

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
        self._resolved.put(target.session_id, res.value)
        return res.value, False

    async def enrolled(self, target: StartTarget) -> bool | None:
        """The enrolment re-check (M3.7): the activity inside the student's ACTIVE courses and the
        student role there. Not found in those courses is False; unreadable is None."""
        site = await self._site(target)
        if site is None:
            return None
        ref, not_found = await self._resolve(site, target)
        self._resolved.pop(target.session_id)
        if ref is None:
            return False if not_found else None
        enrol = await r.read_enrolment(
            self._client, site, ref.courseid, target.student_userid
        )
        return enrol.value.is_student if isinstance(enrol, Readable) else None

    def forget(self, session_id: UUID) -> None:
        """Drop a resolution cached under ``session_id`` (a render check's scratch key)."""
        self._resolved.pop(session_id)

    @property
    def cached(self) -> int:
        return len(self._resolved)

    async def _course_ref(
        self, site: SiteRef, target: StartTarget
    ) -> ActivityRef | None:
        """The activity's course for the GRADER side (graders, close facts): resolved inside the
        student's courses, or, once the student has left the course, inside the hinted course
        when the cmid is found there (CodeRabbit 4503025969). Gates never use this fallback."""
        ref, not_found = await self._resolve(site, target)
        if ref is not None or not not_found or not target.courseid_hint:
            return ref
        kind = "quiz" if target.activity_type == "quiz" else "assign"
        res = await resolve_in_course(
            self._client,
            site,
            cmid=target.cmid,
            kind=kind,
            courseid=int(target.courseid_hint),
        )
        return res.value if isinstance(res, Readable) else None

    async def graders(self, target: StartTarget) -> tuple[GraderView, ...] | None:
        """The course's current graders (REQ-082); None when unreadable (never a guess)."""
        site = await self._site(target)
        if site is None:
            return None
        ref = await self._course_ref(site, target)
        if ref is None:
            return None
        res = await r.read_graders(self._client, site, ref.courseid)
        return res.value.graders if isinstance(res, Readable) else None

    async def teacher_facts(
        self, site_id: UUID, cmid: int, courseid: int, kind: str
    ) -> dict[str, Any]:
        """The activity-level facts of ``assess_teacher_gate`` (plan assess-teacher-ops): the
        cmid resolved INSIDE the hinted course only, then its editors, graders and the REQ-077
        enable facts. Anything unreadable is None (the API fails closed on it)."""
        target = StartTarget(
            session_id=UUID(int=0),
            site_id=site_id,
            cmid=cmid,
            student_userid=0,
            courseid_hint=courseid,
            activity_type=kind,
            session_no=1,
            flags={},
        )
        site = await self._site(target)
        if site is None:
            return {"resolved": False, "reason": "site_release_unknown"}
        res = await resolve_in_course(
            self._client,
            site,
            cmid=cmid,
            kind="quiz" if kind == "quiz" else "assign",
            courseid=courseid,
        )
        if isinstance(res, Unreadable):
            return {"resolved": False, "reason": res.detail_code or "unreadable"}
        ref = res.value
        c = self._client
        editors = await r.read_editors(c, site, ref.courseid)
        graders = await r.read_graders(c, site, ref.courseid)
        out: dict[str, Any] = {
            "resolved": True,
            "courseid": ref.courseid,
            "activity_type": kind,
            "editors": sorted({u.userid for u in editors.value.graders})
            if isinstance(editors, Readable)
            else None,
            "graders": sorted({u.userid for u in graders.value.graders})
            if isinstance(graders, Readable)
            else None,
            "close_at": None,
            "team_submission": None,
            "grading_method": None,
            "moodle_rubric": None,
        }
        if kind == "quiz":
            close = await r.read_quiz_close_inputs(
                c, site, ref.courseid, ref.instanceid
            )
            if isinstance(close, Readable):
                out["close_at"] = int(close.value.timeclose or 0)
            out["team_submission"] = False
            return out
        settings = await r.read_assign_settings(c, site, ref.courseid, ref.instanceid)
        if isinstance(settings, Readable):
            out["close_at"] = int(settings.value.cutoffdate or 0)
            out["team_submission"] = bool(settings.value.teamsubmission)
        grading = await r.read_grading_view(c, site, cmid)
        if isinstance(grading, Readable):
            out["grading_method"] = grading.value.method
            out["moodle_rubric"] = (
                grading.value.method == "rubric" and grading.value.rubric is not None
            )
        return out

    async def close_facts(
        self,
        *,
        session_id: UUID,
        site_id: UUID,
        cmid: int,
        student_userid: int,
        courseid_hint: int | None,
        activity_type: str,
        sink: dict[str, Any] | None = None,
    ) -> CloseFacts:
        """What the transcript header needs from Moodle at close (plan 10): the course short
        name and the grade as the student sees it NOW (release-gated: an unreleased grade never
        leaves the reader). Each part is None when unreadable. The recipients come from the
        sealed teacher config (REQ-082 as amended), not from Moodle."""
        target = StartTarget(
            session_id=session_id,
            site_id=site_id,
            cmid=cmid,
            student_userid=student_userid,
            courseid_hint=courseid_hint,
            activity_type=activity_type,
            session_no=1,
            flags={},
        )
        site = await self._site(target)
        if site is None:
            return CloseFacts()
        ref = await self._course_ref(site, target)
        self._resolved.pop(target.session_id)
        if ref is None:
            return CloseFacts()
        # each part goes to ``sink`` as soon as it is read (a caller that times out keeps it)
        part: dict[str, Any] = sink if sink is not None else {}
        # read with the resolution: no second course list
        shortname = part["course_shortname"] = ref.course_shortname
        grade = part["grade"] = await self._grade_now(site, ref, target)
        return CloseFacts(course_shortname=shortname, grade=grade)

    async def _grade_now(
        self, site: SiteRef, ref: ActivityRef, target: StartTarget
    ) -> Readable[GradeReading] | Unreadable:
        """BR-010 at close: an assignment's grade counts as released only when the grade item is
        visible AND the marking workflow (if any) says released; a quiz's when its item is."""
        c, uid = self._client, target.student_userid
        item = await r.read_grade_release(c, site, ref.courseid, uid, target.cmid)
        if isinstance(item, Unreadable):
            return item
        released = not item.value.hidden
        if released and target.activity_type != "quiz":
            rel = await r.read_assign_release(
                c, site, ref.courseid, ref.instanceid, uid
            )
            if isinstance(rel, Unreadable):
                return rel
            released = rel.value.released
        v = item.value
        return Readable(
            GradeReading(
                released=released,
                grade=mark_str(v.grade) if released and v.grade is not None else None,
                scale_label=v.grade_formatted if released else None,
            ),
            item.evidence,
        )

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
    ) -> tuple[SiteRef, Capabilities, str | None, str | None, bool] | None:
        """(site, capabilities, solution, pasted rubric, whether a NEEDED sealed field failed to
        open); None when the site is unknown."""
        async with self._acquire() as conn:
            site = (await conn.execute(_SITE, {"s": target.site_id})).first()
            if site is None or not site.release:
                return None
            caps = {
                r.flag: r.state
                for r in (await conn.execute(_CAPS, {"s": target.site_id})).all()
            }
            # only the fields this capture may see are fetched at all: the solution with the
            # toggle ON, the pasted rubric with the source "pasted"
            kind = "quiz" if target.activity_type == "quiz" else "assign"
            wanted = []
            if target.flags.get("show_solution") is True:
                wanted.append(SOLUTION)
            if rubric_source_of(target.flags, kind) == "pasted":
                wanted.append(PASTED_RUBRIC)
            solution = pasted = None
            unreadable = False
            if wanted and self._teacher_private is not None:
                opened = await read_teacher_config(
                    conn, self._teacher_private, target.site_id, target.cmid, wanted
                )
                solution, pasted = opened.solution, opened.pasted_rubric
                # a field the activity needs that does not open fails closed (never a session
                # silently started without its solution or pasted rubric)
                unreadable = bool(opened.unreadable & set(wanted))
        ref = SiteRef(str(target.site_id), str(site.base_url), str(site.release))
        capabilities = Capabilities(
            review_html_filterable=caps.get("review_html_filterable", "unreadable"),
            rubric_display_settings=caps.get("rubric_display_settings", "unreadable"),
        )
        return ref, capabilities, solution, pasted, unreadable

    async def capture(self, target: StartTarget) -> CaptureResult:
        ctx = await self._context(target)
        if ctx is None:
            return Failed(FailReason.POLICY_INPUT_UNREADABLE, "site_release_unknown")
        site, caps, solution, pasted, unreadable = ctx
        if unreadable:
            return Failed(
                FailReason.POLICY_INPUT_UNREADABLE, "teacher_config_unreadable"
            )
        kind = "quiz" if target.activity_type == "quiz" else "assign"
        ref, _not_found = await self._resolve(site, target)
        self._resolved.pop(target.session_id)
        if ref is None:
            return Failed(FailReason.POLICY_INPUT_UNREADABLE, "activity_unresolved")
        readers = MoodleCaptureReaders(self._client, site)
        teacher = teacher_view(target.flags, kind, solution, pasted)
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
