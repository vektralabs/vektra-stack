"""``capture_snapshot``: read, minimise and freeze one attempt (BR-008, BR-011, BR-015, REQ-101).

Runs ONLY inside the worker's pre-flight handler (plan 8). The order is the control:

1. compute the disclosure policy from flags, review options, capabilities and release state;
2. select the attempt (BR-012) from the readers' typed values;
3. read the selected attempt;
4. project through the policy: excluded fields are dropped right here, inside this frame, and
   never assigned to a snapshot field, logged or persisted;
5. send the question HTML (allowlist blocks only) and the submission files to the sidecar;
6. build the snapshot; ``persist_capture`` then hashes, encrypts, inserts and writes the exclusion
   audit event in ONE transaction (a failed audit write aborts the pre-flight).

Review HTML and file bytes live only in local variables of this module; nothing here writes them
anywhere. Logging is limited to content-free codes (the ``vektra_assess`` allowlist applies too).
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

import structlog

from vektra_assess.context.capture.selection import (
    NotEligible,
    NotEligibleReason,
    Selected,
    assign_fully_graded,
    is_fully_graded,
    select_attempt,
)
from vektra_assess.context.capture.store import Conn, insert_snapshot, write_audit
from vektra_assess.context.policy import (
    CapabilityState,
    DisclosurePolicy,
    FrozenPolicy,
    HtmlPath,
    PolicyInputs,
    ReviewOptions,
    freeze,
)
from vektra_assess.context.qtypes import (
    EXTRACTION_QTYPES,
    INFORMATION_QTYPES,
    is_discussable,
)
from vektra_assess.context.snapshot import (
    ActivityKind,
    AssignCriterion,
    ExclusionRecord,
    FileEntry,
    FrozenFlags,
    GradeKind,
    GradeMethod,
    MarksHidden,
    OverallItem,
    QuizItem,
    Snapshot,
    SnapshotHeader,
    SubmissionItem,
    mark_str,
)
from vektra_assess.moodle.domain import (
    AssignReleaseState,
    AssignSettings,
    AttemptReview,
    AttemptState,
    AttemptSummary,
    GradeReleaseState,
    GradingView,
    QuizCloseInputs,
    RubricFilling,
    SubmissionFileRef,
    SubmissionStatus,
    UserReviewOptions,
)
from vektra_assess.moodle.result import Readable, Reading, Unreadable
from vektra_assess.parser_protocol import FileStatus

log = structlog.get_logger(__name__)

# --- ports ---------------------------------------------------------------------------------------


class CaptureReaders(Protocol):
    """The plan 4 readers, bound to one client and site (``MoodleCaptureReaders``)."""

    async def quiz_close_inputs(
        self, courseid: int, quizid: int
    ) -> Reading[QuizCloseInputs]: ...
    async def user_review_options(
        self, quizid: int, userid: int
    ) -> Reading[UserReviewOptions]: ...
    async def user_attempts(
        self, quizid: int, userid: int
    ) -> Reading[tuple[AttemptSummary, ...]]: ...
    async def attempt_review(self, attemptid: int) -> Reading[AttemptReview]: ...
    async def grade_release(
        self, courseid: int, userid: int, cmid: int
    ) -> Reading[GradeReleaseState]: ...
    async def assign_settings(
        self, courseid: int, assignid: int
    ) -> Reading[AssignSettings]: ...
    async def assign_release(
        self, courseid: int, assignid: int, userid: int
    ) -> Reading[AssignReleaseState]: ...
    async def submission_status(
        self, assignid: int, userid: int
    ) -> Reading[SubmissionStatus]: ...
    async def grading_view(self, cmid: int) -> Reading[GradingView]: ...
    async def rubric_fillings(
        self, definitionid: int
    ) -> Reading[tuple[RubricFilling, ...]]: ...
    async def submission_file(
        self,
        assignid: int,
        ref: SubmissionFileRef,
        owner_userid: int,
        submission_id: int,
    ) -> Reading[bytes]: ...


class ParserResult(Protocol):
    status: FileStatus
    text: str
    upto_page: int | None
    pages_total: int | None
    code: str


class CaptureParser(Protocol):
    """The sidecar client (``ParserClient``)."""

    async def parse_file(self, data: bytes, ext_hint: str = "") -> Any: ...
    async def sanitize_html(self, html: bytes) -> Any: ...
    async def extract_review_item(
        self, html: bytes
    ) -> tuple[str | None, str | None, bool]: ...


# --- inputs and results --------------------------------------------------------------------------


@dataclass(frozen=True)
class TeacherConfigView:
    """What capture may know of the teacher config (plan 11 store, OQ-034 write-only).

    ``solution_text`` is decrypted by the worker ONLY when ``show_solution`` is ON: with the toggle
    OFF the field is not merely ignored, it is never fetched. The constructor enforces it.
    """

    show_solution: bool
    rubric_source: str  # none | pasted | moodle
    solution_text: str | None = None
    pasted_rubric: str | None = None

    def __post_init__(self) -> None:
        if self.solution_text is not None and not self.show_solution:
            raise ValueError("solution_text is populated only when show_solution is ON")
        if self.rubric_source not in ("none", "pasted", "moodle"):
            raise ValueError("unknown rubric source")


@dataclass(frozen=True)
class Capabilities:
    """The two ARCH-070 flags the policy reads (``assess_site_capabilities``)."""

    review_html_filterable: CapabilityState = "unreadable"
    rubric_display_settings: CapabilityState = "unreadable"


@dataclass(frozen=True)
class CaptureLimits:
    """REQ-101 (c), operator-configurable. Total text and online text caps are design defaults."""

    max_files: int = 10
    max_file_bytes: int = 10 * 1024 * 1024
    total_text_chars: int = 120_000
    onlinetext_chars: int = 50_000


@dataclass(frozen=True)
class QuizTarget:
    courseid: int
    quizid: int
    cmid: int
    userid: int


@dataclass(frozen=True)
class AssignTarget:
    courseid: int
    assignid: int
    cmid: int
    userid: int


@dataclass(frozen=True)
class AuditEvent:
    """A content-free audit row to write with the snapshot (paths, codes, counts: never values)."""

    event: str
    reason_code: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)


class FailReason(StrEnum):
    POLICY_INPUT_UNREADABLE = "policy_input_unreadable"
    ATTEMPT_UNREADABLE = "attempt_unreadable"
    SUBMISSION_UNREADABLE = "submission_unreadable"


@dataclass(frozen=True)
class Built:
    snapshot: Snapshot
    frozen_policy: FrozenPolicy
    audit: tuple[AuditEvent, ...]


@dataclass(frozen=True)
class CaptureNotEligible:
    reason: NotEligibleReason
    audit: tuple[AuditEvent, ...] = ()


@dataclass(frozen=True)
class Failed:
    reason: FailReason
    detail_code: str = ""


type CaptureResult = Built | CaptureNotEligible | Failed


# --- helpers -------------------------------------------------------------------------------------


def _released(gr: GradeReleaseState) -> bool:
    return not gr.hidden and not gr.hidden_by_date


def _exclusion(
    policy: DisclosurePolicy, paths: Sequence[str], *, structured_only: bool
) -> ExclusionRecord:
    return ExclusionRecord(
        policy_version=policy.version,
        excluded_paths=tuple(sorted(set(paths))),
        html_structured_only=structured_only,
        rubric_descriptors_excluded=not policy.rubric_descriptors,
        solution_shown=policy.solution,
    )


def _common_paths(policy: DisclosurePolicy) -> list[str]:
    paths = []
    if not policy.solution:
        paths.append("solution_text")
    if not policy.overall_feedback:
        paths.append("overall_feedback")
    return paths


def _solution(policy: DisclosurePolicy, teacher: TeacherConfigView) -> str | None:
    return teacher.solution_text if policy.solution else None


def _pasted(policy: DisclosurePolicy, teacher: TeacherConfigView) -> str | None:
    return teacher.pasted_rubric if policy.pasted_rubric else None


def _flags(teacher: TeacherConfigView) -> FrozenFlags:
    return FrozenFlags(
        show_solution=teacher.show_solution,
        rubric_source=teacher.rubric_source,
    )


async def _item_text(
    parser: CaptureParser, html: str | None
) -> tuple[str | None, str | None, bool]:
    """Question text, response and the truncation flag from ONE sidecar call (allowlist)."""
    if not html:
        return None, None, False
    q, r, truncated = await parser.extract_review_item(html.encode("utf-8"))
    return (
        q if q and q.strip() else None,
        r if r and r.strip() else None,
        truncated,
    )


# --- quiz ----------------------------------------------------------------------------------------


async def capture_quiz(
    target: QuizTarget,
    readers: CaptureReaders,
    parser: CaptureParser,
    teacher: TeacherConfigView,
    caps: Capabilities,
    *,
    moodle_release: str,
    now: int | None = None,
) -> CaptureResult:
    t_now = int(time.time()) if now is None else now
    # 1. policy, before any content is read
    close = await readers.quiz_close_inputs(target.courseid, target.quizid)
    review_opts = await readers.user_review_options(target.quizid, target.userid)
    release = await readers.grade_release(target.courseid, target.userid, target.cmid)
    if isinstance(close, Unreadable) or isinstance(release, Unreadable):
        return Failed(FailReason.POLICY_INPUT_UNREADABLE)
    review = (
        ReviewOptions(
            feedback=review_opts.value.all_attempts.feedback,
            generalfeedback=review_opts.value.all_attempts.generalfeedback,
            rightanswer=review_opts.value.all_attempts.rightanswer,
            overallfeedback=review_opts.value.all_attempts.overallfeedback,
            marks=review_opts.value.all_attempts.marks,
        )
        if isinstance(review_opts, Readable)
        else None
    )
    frozen = freeze(
        PolicyInputs(
            flags=_flags(teacher),
            review=review,
            review_html_filterable=caps.review_html_filterable,
            rubric_display_settings=caps.rubric_display_settings,
            rubric_descriptors_visible=None,
            released=_released(release.value),
        )
    )
    policy = frozen.policy
    # 2. selection over every attempt. A review read for "fully graded" keeps only the boolean:
    #    its html (teacher visibility) is dropped at once. The first unreadable or ungraded
    #    attempt already makes the result NotEligible, so the reads stop there.
    attempts = await readers.user_attempts(target.quizid, target.userid)
    graded: dict[int, bool | None] = {}
    if isinstance(attempts, Readable):
        for a in attempts.value:
            if a.preview or a.state is not AttemptState.FINISHED:
                continue
            r = await readers.attempt_review(a.attemptid)
            graded[a.attemptid] = (
                is_fully_graded(r.value) if isinstance(r, Readable) else None
            )
            del r
            if graded[a.attemptid] is not True:
                break
    selected = select_attempt(
        attempts, close.value.grademethod, graded, released=policy.eligible
    )
    if isinstance(selected, NotEligible):
        return CaptureNotEligible(selected.reason)
    assert isinstance(selected, Selected)
    # 3. read the selected attempt again: the only review whose html is kept, for this frame
    reread = await readers.attempt_review(selected.attempt.attemptid)
    if not isinstance(reread, Readable) or not is_fully_graded(reread.value):
        return CaptureNotEligible(NotEligibleReason.UNREADABLE)
    chosen = reread.value
    # 3-5. project each item; html goes to the sidecar as allowlisted blocks only
    items: list[QuizItem] = []
    paths = _common_paths(policy)
    degraded = 0
    for slot in chosen.slots:
        if slot.qtype in INFORMATION_QTYPES:
            continue
        label = slot.display_number
        p = f"q{label}"
        if not policy.right_answer:
            paths.append(f"{p}.right_answer")
        if not policy.specific_feedback:
            paths.append(f"{p}.specific_feedback")
        if not policy.general_feedback:
            paths.append(f"{p}.general_feedback")
        if not policy.marks:
            paths += [f"{p}.mark", f"{p}.correctness"]
        if not policy.max_marks:
            paths.append(f"{p}.max_mark")
        paths.append(f"{p}.review_html")  # never kept whole, whatever the path
        question_text = response_text = None
        item_degraded = item_truncated = False
        if slot.qtype in EXTRACTION_QTYPES:
            question_text, response_text, item_truncated = await _item_text(
                parser, slot.html
            )
            item_degraded = question_text is None
        else:
            item_degraded = True  # mark-and-feedback level only (REQ-101), flagged
        degraded += item_degraded
        zero = slot.maxmark <= 0
        items.append(
            QuizItem(
                label=label,
                qtype=slot.qtype,
                discussable=is_discussable(slot.qtype),
                # state ("gradedright") and status ("Correct") reveal correctness; Moodle's
                # "correctness" review option is not exposed by the WS, so both follow the marks
                # visibility (fail closed: hidden marks hide correctness too)
                state=slot.state if policy.marks else None,
                status=slot.status if policy.marks else None,
                mark=mark_str(slot.mark)
                if policy.marks and not zero and slot.mark is not None
                else None,
                max_mark=mark_str(slot.maxmark)
                if policy.max_marks and not zero
                else None,
                marks_hidden=(
                    MarksHidden.NOT_GRADED
                    if zero
                    else (None if policy.marks else MarksHidden.REVIEW_OPTIONS)
                ),
                question_text=question_text,
                response_text=response_text,
                degraded=item_degraded,
                truncated=item_truncated and not item_degraded,
            )
        )
    del chosen  # the html is not needed past this point
    gr = release.value
    method = close.value.grademethod
    snapshot = Snapshot(
        header=SnapshotHeader(
            activity_kind=ActivityKind.QUIZ,
            moodle_release=moodle_release,
            attempt_number=selected.attempt_number,
            attempt_count=selected.attempt_count,
            grade_method=GradeMethod(method),
            # the after-close review options apply only once the quiz is closed, and only if
            # they could be read at all (otherwise the fail-closed set was used)
            review_options_closed=(
                review is not None
                and close.value.timeclose != 0
                and t_now >= close.value.timeclose
            ),
            grade_released=True,
            grade_modified_at=None,
            captured_at=t_now,
            flags=_flags(teacher),
        ),
        quiz_items=tuple(items),
        overall=OverallItem(
            grade_kind=GradeKind.POINTS,
            grade=mark_str(gr.grade),
            grade_max=mark_str(gr.grade_max),
            grade_display=gr.grade_formatted,
            overall_feedback=None,
            grade_read_at=t_now,
        ),
        exclusion=_exclusion(
            policy, paths, structured_only=policy.html_path is HtmlPath.STRUCTURED_ONLY
        ),
        solution_text=_solution(policy, teacher),
        pasted_rubric=_pasted(policy, teacher),
        degraded_count=degraded,
    )
    return Built(snapshot, frozen, (_exclusion_event(snapshot),))


# --- assignment ----------------------------------------------------------------------------------

#: an assignment has no review options: once released (BR-010), grade and feedback are visible
_ASSIGN_VISIBLE = ReviewOptions(
    feedback=True,
    generalfeedback=True,
    rightanswer=False,
    overallfeedback=True,
    marks=2,
)


async def capture_assign(
    target: AssignTarget,
    readers: CaptureReaders,
    parser: CaptureParser,
    teacher: TeacherConfigView,
    caps: Capabilities,
    *,
    moodle_release: str,
    limits: CaptureLimits = CaptureLimits(),
    now: int | None = None,
) -> CaptureResult:
    t_now = int(time.time()) if now is None else now
    settings = await readers.assign_settings(target.courseid, target.assignid)
    release = await readers.assign_release(
        target.courseid, target.assignid, target.userid
    )
    grading = await readers.grading_view(target.cmid)
    if any(isinstance(x, Unreadable) for x in (settings, release, grading)):
        return Failed(FailReason.POLICY_INPUT_UNREADABLE)
    assert isinstance(settings, Readable) and isinstance(release, Readable)
    assert isinstance(grading, Readable)
    frozen = freeze(
        PolicyInputs(
            flags=_flags(teacher),
            review=_ASSIGN_VISIBLE,
            review_html_filterable=caps.review_html_filterable,
            rubric_display_settings=caps.rubric_display_settings,
            rubric_descriptors_visible=None,
            released=release.value.released,
        )
    )
    policy = frozen.policy
    # an unsupported grading method is refused before anything else (EX-023: marking guide)
    if grading.value.method not in ("none", "simple", "rubric", ""):
        return CaptureNotEligible(NotEligibleReason.UNSUPPORTED)
    if not policy.eligible:
        return CaptureNotEligible(NotEligibleReason.NOT_RELEASED)
    status = await readers.submission_status(target.assignid, target.userid)
    if isinstance(status, Unreadable):
        return Failed(FailReason.SUBMISSION_UNREADABLE, status.reason.value)
    st = status.value
    if st.state not in ("submitted",) and not st.files and not st.onlinetext:
        return CaptureNotEligible(NotEligibleReason.NO_SUBMISSION)
    fb = st.feedback
    view = grading.value
    rubric = view.rubric if view.method == "rubric" else None
    filling: RubricFilling | None = None
    if rubric is not None:
        fillings = await readers.rubric_fillings(rubric.definitionid)
        if isinstance(fillings, Unreadable):
            return CaptureNotEligible(NotEligibleReason.UNREADABLE)
        mine = [
            f for f in fillings.value if fb is not None and f.item_id == fb.grade_id
        ]
        filling = mine[0] if len(mine) == 1 else None
    levels = {c.criterionid: c.levelid for c in filling.criteria} if filling else None
    graded = assign_fully_graded(
        view.method,
        None if fb is None else fb.grade,
        [c.criterionid for c in rubric.criteria] if rubric else None,
        levels,
    )
    if isinstance(graded, NotEligible):
        return CaptureNotEligible(graded.reason)
    if not graded or fb is None:
        return CaptureNotEligible(NotEligibleReason.NOT_GRADED)
    paths = _common_paths(policy)
    audit: list[AuditEvent] = []
    # rubric: levels, points and remarks always; descriptors only if permitted (never today)
    criteria: list[AssignCriterion] = []
    if rubric is not None and filling is not None:
        remarks = {c.criterionid: c.remark for c in filling.criteria}
        for i, crit in enumerate(
            sorted(rubric.criteria, key=lambda c: c.sortorder), start=1
        ):
            ordered = sorted(crit.levels, key=lambda lv: lv.score)
            chosen_id = levels[crit.criterionid] if levels else None
            idx = next(
                (n for n, lv in enumerate(ordered, start=1) if lv.levelid == chosen_id),
                None,
            )
            if idx is None:
                # the filling points at a level the definition no longer has (rubric edited
                # after grading) or the criterion has no levels: fail closed, never guess
                return CaptureNotEligible(NotEligibleReason.UNREADABLE)
            label = f"C{i}"
            if not policy.rubric_descriptors:
                paths += [
                    f"rubric.{label.lower()}.description",
                    f"rubric.{label.lower()}.descriptors",
                ]
            criteria.append(
                AssignCriterion(
                    label=label,
                    level_index=idx,
                    level_count=len(ordered),
                    points=mark_str(ordered[idx - 1].score),
                    max_points=mark_str(ordered[-1].score),
                    remark=remarks.get(crit.criterionid, ""),
                )
            )
    # grade: points, or the scale label (BR-014: never "punti/max" for a scale)
    gi = release.value.grade_item
    if settings.value.grade_type == "scale":
        overall = OverallItem(
            grade_kind=GradeKind.SCALE,
            grade=None,
            grade_max=None,
            scale_label=gi.grade_formatted,
            overall_feedback=_feedback(policy, fb.comments),
            grade_read_at=t_now,
        )
    else:
        overall = OverallItem(
            grade_kind=GradeKind.POINTS,
            grade=mark_str(gi.grade),
            grade_max=mark_str(gi.grade_max),
            grade_display=gi.grade_formatted,
            overall_feedback=_feedback(policy, fb.comments),
            grade_read_at=t_now,
        )
    submission = await _onlinetext(parser, st.onlinetext, limits)
    # the online text counts against the same total extracted-text cap (REQ-101 (c))
    used = len(submission.onlinetext or "") if submission is not None else 0
    files = await _files(
        target,
        st,
        readers,
        parser,
        limits,
        audit,
        budget=limits.total_text_chars - used,
    )
    snapshot = Snapshot(
        header=SnapshotHeader(
            activity_kind=ActivityKind.ASSIGN,
            moodle_release=moodle_release,
            attempt_number=1,
            attempt_count=1,
            grade_method=None,
            review_options_closed=False,
            grade_released=True,
            grade_modified_at=fb.graded_date,
            captured_at=t_now,
            flags=_flags(teacher),
        ),
        criteria=tuple(criteria),
        overall=overall,
        submission=submission,
        files=tuple(files),
        exclusion=_exclusion(
            policy, paths, structured_only=policy.html_path is HtmlPath.STRUCTURED_ONLY
        ),
        solution_text=_solution(policy, teacher),
        pasted_rubric=_pasted(policy, teacher),
    )
    return Built(snapshot, frozen, (_exclusion_event(snapshot), *audit))


def _feedback(policy: DisclosurePolicy, comments: Sequence[str]) -> str | None:
    if not policy.overall_feedback:
        return None
    text = "\n\n".join(c for c in comments if c.strip())
    return text or None


async def _onlinetext(
    parser: CaptureParser, html: str | None, limits: CaptureLimits
) -> SubmissionItem | None:
    if not html or not html.strip():
        return None
    res = await parser.sanitize_html(html.encode("utf-8"))
    if res.status is FileStatus.NO_TEXT:
        return None  # blank once sanitized: nothing was submitted as text
    if res.status not in (FileStatus.READ, FileStatus.READ_PARTIAL):
        return SubmissionItem(onlinetext=None, unreadable=True)
    text = str(res.text)
    # the online text is charged to the total budget too, so it can never exceed it alone
    cap = min(limits.onlinetext_chars, limits.total_text_chars)
    truncated = len(text) > cap or res.status is FileStatus.READ_PARTIAL
    return SubmissionItem(onlinetext=text[:cap], truncated=truncated)


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


async def _files(
    target: AssignTarget,
    st: SubmissionStatus,
    readers: CaptureReaders,
    parser: CaptureParser,
    limits: CaptureLimits,
    audit: list[AuditEvent],
    budget: int | None = None,
) -> list[FileEntry]:
    out: list[FileEntry] = []
    budget = limits.total_text_chars if budget is None else budget
    for n, ref in enumerate(st.files, start=1):
        label = f"F{n}"
        if n > limits.max_files:
            out.append(
                FileEntry(
                    label=label, filename=ref.filename, status=FileStatus.OVER_MAX_FILES
                )
            )
            continue
        if ref.size > limits.max_file_bytes:
            out.append(
                FileEntry(
                    label=label, filename=ref.filename, status=FileStatus.TOO_LARGE
                )
            )
            continue
        if st.submission_id is None:
            out.append(
                FileEntry(label=label, filename=ref.filename, status=FileStatus.CORRUPT)
            )
            continue
        # ownership is re-verified by the reader against the selected submission
        data = await readers.submission_file(
            target.assignid, ref, target.userid, st.submission_id
        )
        if isinstance(data, Unreadable):
            if data.detail_code in ("submission_mismatch", "file_not_in_submission"):
                audit.append(
                    AuditEvent(
                        "assess_file_rejected", data.detail_code, {"file": label}
                    )
                )
            out.append(
                FileEntry(label=label, filename=ref.filename, status=FileStatus.CORRUPT)
            )
            continue
        res = await parser.parse_file(data.value, _ext(ref.filename))
        del data  # bytes go no further than the sidecar call
        status = FileStatus(res.status)
        text = (
            str(res.text)
            if status in (FileStatus.READ, FileStatus.READ_PARTIAL)
            else ""
        )
        cut = False
        if status in (FileStatus.READ, FileStatus.READ_PARTIAL) and not text.strip():
            status, text = FileStatus.NO_TEXT, ""
        elif text and len(text) > budget:
            cut = True
            # the total extracted-text cap (REQ-101 (c)): what fits is kept as partial; a file
            # that gets nothing at all is "troppo grande", not "testo non estraibile"
            if budget > 0:
                text, status = text[:budget], FileStatus.READ_PARTIAL
            else:
                text, status = "", FileStatus.TOO_LARGE
        budget -= len(text)
        out.append(
            FileEntry(
                label=label,
                filename=ref.filename,
                status=status,
                text=text,
                # a cut by the text budget does not end at the parser's page: no page claimed
                upto_page=(
                    res.upto_page
                    if status is FileStatus.READ_PARTIAL and not cut
                    else None
                ),
                pages_total=res.pages_total,
            )
        )
    return out


# --- persistence ---------------------------------------------------------------------------------


def _exclusion_event(snapshot: Snapshot) -> AuditEvent:
    ex = snapshot.exclusion
    return AuditEvent(
        "assess_context_exclusion_applied",
        None,
        {
            "policy_version": ex.policy_version,
            "excluded_field_set_hash": ex.field_set_hash(),
            "html_structured_only": ex.html_structured_only,
            "rubric_descriptors_excluded": ex.rubric_descriptors_excluded,
            "solution_shown": ex.solution_shown,
        },
    )


async def persist_capture(
    conn: Conn,
    session_id: UUID,
    dek: bytes,
    built: Built,
    *,
    site_id: UUID | None = None,
) -> str:
    """Encrypt and insert the snapshot and write its audit events, in the caller's transaction.

    Returns the content hash (for plan 8's content-free commit event). Any failure raises and the
    caller rolls back: there is never a snapshot without its exclusion event.
    """
    digest = await insert_snapshot(conn, session_id, dek, built.snapshot)
    for ev in built.audit:
        await write_audit(
            conn,
            ev.event,
            session_id=session_id,
            site_id=site_id,
            reason_code=ev.reason_code,
            data=ev.data,
        )
    log.info("assess_snapshot_captured", session_id=str(session_id), hash=digest)
    return digest
