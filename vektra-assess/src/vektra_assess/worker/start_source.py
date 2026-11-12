"""The production ``StartSource``: the LMS provider + parser sidecar -> snapshot (plan 8
pre-flight), over the ``LmsProvider`` Protocol only (ADR-0033 D1, D2).

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

The authority stays here (ADR-0033 D1): the decision to re-resolve every id, the resolution
cache, and every gate-fact derivation (the grace rule, ``own_close_at``, ``fully_graded``, the
release rule of BR-010). The provider only looks up and reads, in neutral values.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vektra_assess.context.capture.bound import SiteReaders
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
from vektra_assess.context.regrade import GradeReading
from vektra_assess.context.snapshot import mark_str
from vektra_assess.crypto.teacher_config import (
    PASTED_RUBRIC,
    SOLUTION,
    read_teacher_config,
)
from vektra_assess.keys import AssessKeyring
from vektra_assess.teacher.validation import rubric_source_of
from vektra_assess.transcript.facts import CloseFacts
from vektra_assess.worker.preflight import LmsGateFacts, StartTarget
from vektra_shared.lms import (
    NOT_IN_USER_COURSES,
    ActivityKind,
    ActivityRef,
    AttemptState,
    LmsProvider,
    LmsSite,
    OverdueMode,
    QuizSettings,
    Readable,
    StaffMember,
    StaffRole,
    Unreadable,
)

_SITE = text("SELECT base_url, release FROM assess_sites WHERE id = :s")
_SET_FIELDS = text(
    "SELECT count(*) FROM assess_activity_config_sealed WHERE site_id = :s AND cmid = :c "
    "AND field = ANY(:f) AND sealed_blob IS NOT NULL"
)
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


def _kind(activity_type: str) -> ActivityKind:
    return "quiz" if activity_type == "quiz" else "assign"


def _ids(members: tuple[StaffMember, ...]) -> list[int]:
    return sorted({m.user_id for m in members})


class LmsStartSource:
    def __init__(
        self,
        provider: LmsProvider,
        parser: CaptureParser,
        acquire: Callable[[], AbstractAsyncContextManager[AsyncConnection]],
        *,
        teacher_private: AssessKeyring | None = None,
        resolved_max: int = 256,
        resolved_ttl_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
        self._parser = parser
        self._acquire = acquire
        self._teacher_private = teacher_private
        # the course and instance resolved by ``gate_facts``, reused by ``capture`` (same Start).
        # Bounded: a render check, or a Start a gate refused, never reaches ``capture``.
        self._resolved = _BoundedCache(resolved_max, resolved_ttl_s, clock)

    async def _site(self, site_id: UUID) -> SiteReaders | None:
        async with self._acquire() as conn:
            site = (await conn.execute(_SITE, {"s": site_id})).first()
        if site is None or not site.release:
            return None
        return SiteReaders(
            self._provider, LmsSite(str(site_id), str(site.base_url), str(site.release))
        )

    async def _resolve(
        self, lms: SiteReaders, target: StartTarget
    ) -> tuple[ActivityRef | None, bool]:
        """(the activity, or None; and whether None means "not in the student's courses").

        Per call: the source is shared by every concurrent pre-flight of the worker."""
        ref = self._resolved.get(target.session_id)
        if ref is not None:
            return ref, False
        res = await lms.resolve_activity(
            user_id=target.student_userid,
            activity_id=target.cmid,
            kind=_kind(target.activity_type),
            course_hint=target.courseid_hint,
        )
        if isinstance(res, Unreadable):
            return None, res.detail_code == NOT_IN_USER_COURSES
        self._resolved.put(target.session_id, res.value)
        return res.value, False

    async def enrolled(self, target: StartTarget) -> bool | None:
        """The enrolment re-check (M3.7): the activity inside the student's ACTIVE courses and the
        student role there. Not found in those courses is False; unreadable is None."""
        lms = await self._site(target.site_id)
        if lms is None:
            return None
        ref, not_found = await self._resolve(lms, target)
        self._resolved.pop(target.session_id)
        if ref is None:
            return False if not_found else None
        enrol = await lms.enrolment(ref.course_id, target.student_userid)
        return enrol.value.is_student if isinstance(enrol, Readable) else None

    def forget(self, session_id: UUID) -> None:
        """Drop a resolution cached under ``session_id`` (a render check's scratch key)."""
        self._resolved.pop(session_id)

    @property
    def cached(self) -> int:
        return len(self._resolved)

    async def _course_ref(
        self, lms: SiteReaders, target: StartTarget
    ) -> ActivityRef | None:
        """The activity's course for the GRADER side (graders, close facts): resolved inside the
        student's courses, or, once the student has left the course, inside the hinted course
        when the cmid is found there. Gates never use this fallback."""
        ref, not_found = await self._resolve(lms, target)
        if ref is not None or not not_found or not target.courseid_hint:
            return ref
        res = await lms.resolve_in_course(
            activity_id=target.cmid,
            kind=_kind(target.activity_type),
            course_id=int(target.courseid_hint),
        )
        return res.value if isinstance(res, Readable) else None

    async def graders(self, target: StartTarget) -> tuple[StaffMember, ...] | None:
        """The course's current graders (REQ-082); None when unreadable (never a guess)."""
        lms = await self._site(target.site_id)
        if lms is None:
            return None
        ref = await self._course_ref(lms, target)
        if ref is None:
            return None
        res = await lms.course_staff(ref.course_id, StaffRole.GRADER)
        return res.value if isinstance(res, Readable) else None

    async def teacher_facts(
        self, site_id: UUID, cmid: int, courseid: int, kind: str
    ) -> dict[str, Any]:
        """The activity-level facts of ``assess_teacher_gate`` (plan assess-teacher-ops): the
        cmid resolved INSIDE the hinted course only, then, concurrently, its editors (REQ-077),
        its graders (REQ-076, plan 11b), the REQ-077 enable facts and, for a quiz, the override
        facts of the REQ-076 gate panel: ``override_holder`` (the participant whose user override
        sets the latest close, when the overrides are readable) and ``override_viewers`` (the
        staff who may manage overrides). Anything unreadable is None (the API fails closed on
        it). Ids and flags only."""
        lms = await self._site(site_id)
        if lms is None:
            return {"resolved": False, "reason": "site_release_unknown"}
        res = await lms.resolve_in_course(
            activity_id=cmid, kind=_kind(kind), course_id=courseid
        )
        if isinstance(res, Unreadable):
            return {"resolved": False, "reason": res.detail_code or "unreadable"}
        ref = res.value
        out: dict[str, Any] = {
            "resolved": True,
            "courseid": ref.course_id,
            "activity_type": kind,
            "editors": None,
            "graders": None,
            "close_at": None,
            "team_submission": None,
            "grading_method": None,
            "lms_rubric": None,
            "override_holder": None,
            "override_viewers": None,
            "automatic_close_at": None,
            "grace_s": None,
        }
        if kind == "quiz":
            editors, graders, close, viewers = await asyncio.gather(
                lms.course_staff(ref.course_id, StaffRole.EDITOR),
                lms.course_staff(ref.course_id, StaffRole.GRADER),
                lms.quiz_settings(ref.course_id, ref.instance_id),
                lms.course_staff(ref.course_id, StaffRole.OVERRIDE_MANAGER),
            )
            if isinstance(close, Readable):
                out["close_at"] = int(close.value.time_close or 0)
                out["override_holder"] = _override_holder(close.value)
                out["automatic_close_at"] = _automatic_close(close.value)
                if isinstance(close.value.grace, Readable):
                    out["grace_s"] = _grace_s(close.value)
            out["team_submission"] = False
            if isinstance(viewers, Readable):
                out["override_viewers"] = _ids(viewers.value)
        else:
            editors, graders, settings, grading = await asyncio.gather(
                lms.course_staff(ref.course_id, StaffRole.EDITOR),
                lms.course_staff(ref.course_id, StaffRole.GRADER),
                lms.assignment_settings(ref.course_id, ref.instance_id),
                lms.grading_definition(cmid),
            )
            out["grace_s"] = 0  # an assignment has no grace (the gate's grace_s=0)
            if isinstance(settings, Readable):
                out["close_at"] = int(settings.value.cutoff_date or 0)
                out["team_submission"] = bool(settings.value.team_submission)
            if isinstance(grading, Readable):
                out["grading_method"] = grading.value.method
                out["lms_rubric"] = (
                    grading.value.method == "rubric"
                    and grading.value.rubric is not None
                )
        if isinstance(editors, Readable):
            out["editors"] = _ids(editors.value)
        if isinstance(graders, Readable):
            out["graders"] = _ids(graders.value)
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
        """What the transcript header needs from the LMS at close (plan 10): the course short
        name and the grade as the student sees it NOW (release-gated: an unreleased grade never
        leaves the provider). Each part is None when unreadable. The recipients come from the
        sealed teacher config (REQ-082 as amended), not from the LMS."""
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
        lms = await self._site(target.site_id)
        if lms is None:
            return CloseFacts()
        ref = await self._course_ref(lms, target)
        self._resolved.pop(target.session_id)
        if ref is None:
            return CloseFacts()
        # each part goes to ``sink`` as soon as it is read (a caller that times out keeps it)
        part: dict[str, Any] = sink if sink is not None else {}
        # read with the resolution: no second course list
        shortname = part["course_shortname"] = ref.course_shortname
        grade = part["grade"] = await self._grade_now(lms, ref, target)
        return CloseFacts(course_shortname=shortname, grade=grade)

    async def _grade_now(
        self, lms: SiteReaders, ref: ActivityRef, target: StartTarget
    ) -> Readable[GradeReading] | Unreadable:
        """BR-010 at close: an assignment's grade counts as released only when the grade item is
        visible AND the marking workflow (if any) says released; a quiz's when its item is."""
        uid = target.student_userid
        item = await lms.grade_item(ref.course_id, uid, target.cmid)
        if isinstance(item, Unreadable):
            return item
        released = not item.value.hidden
        if released and target.activity_type != "quiz":
            rel = await lms.assignment_release(ref.course_id, ref.instance_id, uid)
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

    async def gate_facts(self, target: StartTarget) -> LmsGateFacts:
        """The LMS half of the START gates, read fresh; anything unreadable stays None."""
        lms = await self._site(target.site_id)
        if lms is None:
            return LmsGateFacts()
        ref, not_found = await self._resolve(lms, target)
        if ref is None:
            # NOT FOUND in the student's active courses: not enrolled (state 7); the course list
            # unreadable: every fact unknown. Both fail closed.
            return LmsGateFacts(enrolled=False if not_found else None)
        enrol = await lms.enrolment(ref.course_id, target.student_userid)
        enrolled = enrol.value.is_student if isinstance(enrol, Readable) else None
        if target.activity_type == "quiz":
            return await self._quiz_facts(lms, ref, target, enrolled)
        return await self._assign_facts(lms, ref, target, enrolled)

    async def _quiz_facts(
        self, lms: SiteReaders, ref: ActivityRef, t: StartTarget, enrolled: bool | None
    ) -> LmsGateFacts:
        uid = t.student_userid
        close = await lms.quiz_settings(ref.course_id, ref.instance_id)
        attempts = await lms.attempts(ref.instance_id, uid)
        release = await lms.grade_item(ref.course_id, uid, t.cmid)
        activity_close = grace = participants = own = None
        if isinstance(close, Readable):
            q = close.value
            activity_close = q.time_close
            if isinstance(q.grace, Readable):
                grace = _grace_s(q)
            if isinstance(q.overrides, Readable):
                participants = tuple(
                    o.time_close for o in q.overrides.value if o.time_close
                )
                mine = [
                    o.time_close
                    for o in q.overrides.value
                    if o.user_id == uid and o.time_close
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
            graded = bool(finished) and all(a.sum_grades is not None for a in finished)
        released = (
            not release.value.hidden and not release.value.hidden_by_date
            if isinstance(release, Readable)
            else None
        )
        return LmsGateFacts(
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
        self, lms: SiteReaders, ref: ActivityRef, t: StartTarget, enrolled: bool | None
    ) -> LmsGateFacts:
        uid = t.student_userid
        settings = await lms.assignment_settings(ref.course_id, ref.instance_id)
        grading = await lms.grading_definition(t.cmid)
        dates = await lms.assignment_dates(ref.instance_id, uid)
        status = await lms.submission(ref.instance_id, uid)
        release = await lms.assignment_release(ref.course_id, ref.instance_id, uid)
        team = cutoff = None
        if isinstance(settings, Readable):
            team = settings.value.team_submission
            cutoff = settings.value.cutoff_date
        own = None
        if isinstance(dates, Readable) and cutoff is not None:
            mine = max(dates.value.cutoff_date, dates.value.extension_due_date or 0)
            own = mine if mine > cutoff else None
        in_progress = submitted = graded = None
        if isinstance(status, Readable):
            st = status.value
            in_progress = st.state in ("draft", "reopened")
            submitted = (
                st.state == "submitted" or bool(st.files) or bool(st.online_text)
            )
            graded = st.graded  # capture re-checks the rubric levels (BR-014)
        return LmsGateFacts(
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
            # the user overrides and extensions of ALL participants are not listable with the
            # minimal service-account role (OQ-025): the REQ-091 fallback always applies here
            participant_closes=None,
            own_close_at=own,
            owner_verified=True,  # the submission is read for the token's own user id
            enrolled=enrolled,
        )

    async def _context(
        self, target: StartTarget
    ) -> tuple[SiteReaders, Capabilities, str | None, str | None, bool] | None:
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
            kind = _kind(target.activity_type)
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
            elif wanted:
                # no key to open them: a STORED needed field fails closed like one that does not
                # open; an unset one keeps the safe default (nothing to show)
                stored = (
                    await conn.execute(
                        _SET_FIELDS,
                        {"s": target.site_id, "c": target.cmid, "f": wanted},
                    )
                ).scalar()
                unreadable = bool(stored)
        lms = SiteReaders(
            self._provider,
            LmsSite(str(target.site_id), str(site.base_url), str(site.release)),
        )
        capabilities = Capabilities(
            review_html_filterable=caps.get("review_html_filterable", "unreadable"),
            rubric_display_settings=caps.get("rubric_display_settings", "unreadable"),
        )
        return lms, capabilities, solution, pasted, unreadable

    async def capture(self, target: StartTarget) -> CaptureResult:
        ctx = await self._context(target)
        if ctx is None:
            return Failed(FailReason.POLICY_INPUT_UNREADABLE, "site_release_unknown")
        lms, caps, solution, pasted, unreadable = ctx
        if unreadable:
            return Failed(
                FailReason.POLICY_INPUT_UNREADABLE, "teacher_config_unreadable"
            )
        kind = _kind(target.activity_type)
        ref, _not_found = await self._resolve(lms, target)
        self._resolved.pop(target.session_id)
        if ref is None:
            return Failed(FailReason.POLICY_INPUT_UNREADABLE, "activity_unresolved")
        teacher = teacher_view(target.flags, kind, solution, pasted)
        release = str(lms.site.release)
        if kind == "quiz":
            return await capture_quiz(
                QuizTarget(
                    ref.course_id, ref.instance_id, target.cmid, target.student_userid
                ),
                lms,
                self._parser,
                teacher,
                caps,
                lms_release=release,
            )
        return await capture_assign(
            AssignTarget(
                ref.course_id, ref.instance_id, target.cmid, target.student_userid
            ),
            lms,
            self._parser,
            teacher,
            caps,
            lms_release=release,
        )


def _grace_s(q: QuizSettings) -> int:
    """The grace period that delays the close: only with the GRACE overdue mode."""
    assert isinstance(q.grace, Readable)
    g = q.grace.value
    return g.grace_period_s if g.overdue is OverdueMode.GRACE else 0


def _override_holder(q: QuizSettings) -> int | None:
    """The participant whose USER override sets the quiz's latest close (REQ-076 gate panel),
    or None: overrides unreadable, none later than the quiz's own close, or the latest one is a
    GROUP override (a group is not a person to name)."""
    if not isinstance(q.overrides, Readable):
        return None
    later = [
        o for o in q.overrides.value if o.time_close and o.time_close > q.time_close
    ]
    if not later:
        return None
    top = max(later, key=lambda o: o.time_close or 0)
    return int(top.user_id) if top.user_id else None


def _automatic_close(q: QuizSettings) -> int | None:
    """BR-006(d) automatic close of a quiz for the teacher pages: the
    latest of the quiz close and every participant's override close, plus the grace period,
    the same formula the Start gate uses (``gates._effective_gate``); None unless the close,
    every override and the grace are readable (the gate then takes the REQ-091 fallback path,
    whose inputs the API already has)."""
    if not q.time_close or not isinstance(q.overrides, Readable):
        return None
    if not isinstance(q.grace, Readable):
        return None
    closes = [o.time_close for o in q.overrides.value if o.time_close]
    return int(max([q.time_close, *closes]) + _grace_s(q))
