"""Part A of the opening message: system-templated facts, never the LLM (REQ-094, BR-009).

Deterministic: the same bundle and session facts give the same bytes. Rendered with Jinja2 and
``StrictUndefined`` (a missing value is an error, never an empty string). ``PartA.text`` is plain
text and NOT escaped (student-controlled file names flow through it); ``PartA.html`` is the same
text autoescaped, for any consumer that puts it into HTML.

Technical flags (structured-only fallback, policy version) stay out of the student-facing text:
they belong to the transcript header's config snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from jinja2 import Environment, StrictUndefined

from vektra_assess.context import strings as S
from vektra_assess.context.bundle import ContextBundle
from vektra_assess.context.snapshot import (
    ActivityKind,
    GradeKind,
    MarksHidden,
    QuizItem,
)
from vektra_assess.parser_protocol import label as file_label

TZ = ZoneInfo("Europe/Rome")

_TEMPLATE = """\
{{ activity }}
{% if attempt %}{{ attempt }}
{% endif %}{{ br009 }}
{{ session }}
{% if previous %}{{ previous }}
{% endif %}{{ duration }}
{{ material }}
{{ solution }}

{{ overview_title }}:
{% for line in overview %}- {{ line }}
{% endfor %}{% for note in notes %}{{ note }}
{% endfor %}
{{ files_title }}:
{% for line in files %}- {{ line }}
{% else %}{{ no_files }}
{% endfor %}
{{ how_to }}"""

_TEXT = Environment(
    autoescape=False, undefined=StrictUndefined, keep_trailing_newline=False
)
_HTML = Environment(
    autoescape=True, undefined=StrictUndefined, keep_trailing_newline=False
)
_T_TEXT = _TEXT.from_string(_TEMPLATE)
_T_HTML = _HTML.from_string(_TEMPLATE)


@dataclass(frozen=True)
class SessionFacts:
    session_number: int  # "Sessione #N"
    duration_minutes: int
    course_material_used: bool = False  # RAG locked OFF in v1
    #: the previous-context line of REQ-094; its wording belongs to plan 8 (session lifecycle)
    previous_context: str | None = None


@dataclass(frozen=True)
class PartA:
    text: str
    html: str
    overview_items: tuple[str, ...]
    at_summary: str


def fmt_number(value: str) -> str:
    """A canonical decimal string in Italian notation: ``7.5`` -> ``7,5``."""
    return value.replace(".", ",")


def fmt_date(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).astimezone(TZ).strftime("%d/%m/%Y")


def grade_text(bundle: ContextBundle) -> str:
    ov = bundle.overall
    if ov.grade_kind is GradeKind.SCALE:
        return ov.scale_label or ""
    if ov.grade is None:
        return ""
    shown = fmt_number(ov.grade_display or ov.grade)  # as the student sees it in Moodle
    return f"{shown}/{fmt_number(ov.grade_max)}" if ov.grade_max else shown


def br009_statement(bundle: ContextBundle) -> str:
    return S.BR009_TEMPLATE.format(
        grade=grade_text(bundle), date=fmt_date(bundle.overall.grade_read_at)
    )


def _quiz_line(item: QuizItem) -> str:
    if item.marks_hidden is MarksHidden.NOT_GRADED:
        line = S.QUIZ_ITEM_UNGRADED.format(label=item.label)
    elif item.mark is not None and item.max_mark is not None:
        line = S.QUIZ_ITEM.format(
            label=item.label,
            mark=fmt_number(item.mark),
            max=fmt_number(item.max_mark),
            state=item.status or S.QUIZ_STATE_UNKNOWN,
        )
    elif item.max_mark is not None and item.marks_hidden is None:
        # marks are visible but this question has none: it was not answered ("gaveup")
        line = S.QUIZ_ITEM_NO_MARK.format(
            label=item.label,
            max=fmt_number(item.max_mark),
            state=item.status or S.QUIZ_STATE_UNKNOWN,
        )
    elif item.max_mark is not None:
        line = S.QUIZ_ITEM_MAX_ONLY.format(
            label=item.label, max=fmt_number(item.max_mark)
        )
    else:
        line = S.QUIZ_ITEM_HIDDEN.format(label=item.label)
    return line + (
        S.DEGRADED if item.degraded else S.TRUNCATED if item.truncated else ""
    )


def overview(bundle: ContextBundle) -> tuple[str, ...]:
    lines: list[str] = [_quiz_line(i) for i in bundle.quiz_items]
    for n, c in enumerate(bundle.criteria, start=1):
        lines.append(
            S.CRITERION_ITEM.format(
                n=n, level=c.level_index, levels=c.level_count,
                points=fmt_number(c.points), max=fmt_number(c.max_points),
            )
        )  # fmt: skip
    lines.append(f"{S.OVERALL_LABEL}: {grade_text(bundle)}")
    if bundle.activity_kind is ActivityKind.ASSIGN:
        lines.append(S.SUBMISSION_LABEL)
    return tuple(lines)


def _notes(bundle: ContextBundle) -> list[str]:
    notes = []
    if any(i.marks_hidden is MarksHidden.REVIEW_OPTIONS for i in bundle.quiz_items):
        notes.append(S.HIDDEN_MARKS_NOTE)
    if any(i.degraded for i in bundle.quiz_items):
        notes.append(S.DEGRADED_NOTE)
    return notes


def render_part_a(bundle: ContextBundle, facts: SessionFacts) -> PartA:
    attempt = (
        S.ATTEMPT.format(
            k=bundle.attempt_number,
            m=bundle.attempt_count,
            method=S.GRADE_METHOD[bundle.grade_method.value],
        )
        if bundle.grade_method is not None
        else ""
    )
    items = overview(bundle)
    data = {
        "activity": S.ACTIVITY[bundle.activity_kind.value],
        "attempt": attempt,
        "br009": br009_statement(bundle),
        "session": S.SESSION.format(n=facts.session_number),
        "previous": facts.previous_context or "",
        "duration": S.DURATION.format(minutes=facts.duration_minutes),
        "material": S.COURSE_MATERIAL[facts.course_material_used],
        "solution": S.SOLUTION[bundle.solution_shown],
        "overview_title": S.OVERVIEW_TITLE,
        "overview": items,
        "notes": _notes(bundle),
        "files_title": S.FILES_TITLE,
        "files": [
            S.FILE_ITEM.format(
                label=f.label,
                filename=f.filename,
                status=file_label(f.status, f.upto_page),
            )
            for f in bundle.files
        ],
        "no_files": S.NO_FILES,
        "how_to": S.HOW_TO_PROCEED,
    }
    discussion_items = len(bundle.quiz_items) + len(bundle.criteria)
    return PartA(
        text=_T_TEXT.render(**data),
        html=_T_HTML.render(**data),
        overview_items=items,
        at_summary=S.AT_SUMMARY.format(
            k=bundle.attempt_number, n=discussion_items, minutes=facts.duration_minutes
        ),
    )
