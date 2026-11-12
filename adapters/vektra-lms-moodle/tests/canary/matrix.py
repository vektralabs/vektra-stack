"""The NFR-017 canary matrix (plan assess-context F): cases, injection, expected sets.

Reusable: plan 7 reruns it through the real runner, plan 10 through transcripts, the NFR-023 gate
through ``tests/eval/assess_adapter.py``. Import ``build_matrix`` and ``run_case``.

Every case is built from the plan 4 recordings, varied synthetically:
- QUIZ cases use the recorded "types" attempt (one question of each of the 9 types). Into every
  question's review HTML a unique canary is injected per forbidden field and type
  (``CNRY-<field>-<qtype>-<uuid8>``): in the right-answer, specific, general and outcome blocks, in
  the response history, and, adversarially, INSIDE the kept blocks under a feedback class. Marks
  get a numeric canary (``7.3141...``), the status label a correctness canary. The student's
  combined review options are rewritten per case.
- ASSIGNMENT cases use the recorded assignments (rubric visible / hidden / display settings
  unreadable, simple direct, scale, files, unreleased) with the canaries the seeder planted.
- The solution text comes from a fake teacher-config store that counts fetches: with the toggle
  OFF it must never be fetched.
"""

from __future__ import annotations

import copy
import itertools
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

QTYPES = (
    "truefalse",
    "multichoice",
    "shortanswer",
    "numerical",
    "essay",
    "match",
    "multianswer",
    "gapselect",
    "ddwtos",
)
#: forbidden fields per quiz question, and the review option that would permit each
QUIZ_FIELDS = {
    "right_answer": "rightanswer",
    "specific_feedback": "feedback",
    "general_feedback": "generalfeedback",
    "hidden_mark": "marks",
    "correctness": "marks",
    "review_html": None,  # never permitted: teacher-visibility markup outside the kept blocks
    "kept_block_feedback": None,  # feedback planted INSIDE qtext / ablock: always pruned
}
ASSIGN_SHAPES = (
    "rubric_visible",
    "rubric_hidden",
    "rubric_settings_unreadable",
    "simple",
    "scale",
    "files",
    "unreleased",
)
ASSIGN_FIELDS = {
    "rubric_visible": ("rubric_descriptors",),
    "rubric_hidden": ("rubric_descriptors",),
    "rubric_settings_unreadable": ("rubric_descriptors",),
    "simple": ("solution_text",),
    "scale": ("solution_text",),
    "files": ("unread_file_text",),
    "unreleased": ("unreleased_feedback",),
}
#: review-option variants for quiz cases: (feedback, generalfeedback, rightanswer, marks)
REVIEW_VARIANTS = {
    "all_hidden": (0, 0, 0, 0),
    "max_only": (0, 0, 0, 1),
    "marks_only": (0, 0, 0, 2),
    "feedback_visible": (1, 1, 0, 2),
    "all_visible": (1, 1, 1, 2),
}
VERSIONS = ("4.3", "5.2")


def canary(fld: str, kind: str) -> str:
    return f"CNRY-{fld}-{kind}-{uuid.uuid4().hex[:8]}"


@dataclass
class Case:
    name: str
    version: str
    kind: str  # quiz | assign
    toggle: bool
    html_path: str  # filterable | structured_only
    review: str | None = None
    shape: str | None = None
    #: canary -> (field, qtype-or-shape): must appear 0 times in every sink
    forbidden: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: canaries that MUST appear (positive controls), with the sinks they must reach
    required: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: canaries that are allowed (permitted by policy) and therefore not asserted either way
    allowed: set[str] = field(default_factory=set)
    mutate: Callable[[str, Any], Any] | None = None


# --- quiz injection ------------------------------------------------------------------------------


def _quiz_mutator(case: Case, marks: dict[str, str]) -> Callable[[str, Any], Any]:
    fb, gfb, ra, mk = REVIEW_VARIANTS[case.review or "all_hidden"]
    per_q: dict[int, dict[str, str]] = {}

    def mutate(wsfunction: str, body: Any) -> Any:
        if wsfunction == "mod_quiz_get_combined_review_options":
            body = copy.deepcopy(body)
            values = {
                "feedback": fb,
                "generalfeedback": gfb,
                "rightanswer": ra,
                "overallfeedback": 0,
                "marks": mk,
            }
            for opt in body["alloptions"] + body["someoptions"]:
                opt["value"] = values[opt["name"]]
            return body
        if wsfunction != "mod_quiz_get_attempt_review":
            return body
        body = copy.deepcopy(body)
        for q in body["questions"]:
            t = q["type"]
            if t not in QTYPES:
                continue
            c = per_q.setdefault(q["slot"], {f: canary(f, t) for f in QUIZ_FIELDS})
            # the hidden-mark canary IS the number put in the mark: unique per type, with no
            # trailing zero so it survives the canonical decimal form ("7.31411" stays "7.31411")
            c["hidden_mark"] = f"7.3141{QTYPES.index(t) + 1}"
            html = q["html"]
            outside = (
                f'<div class="outcome clearfix"><div class="feedback">'
                f'<div class="specificfeedback">{c["specific_feedback"]}</div>'
                f'<div class="generalfeedback">{c["general_feedback"]}</div>'
                f'<div class="rightanswer">The correct answer is: {c["right_answer"]}</div></div></div>'
                f'<div class="history">{c["review_html"]}</div>'
            )
            inside = f'<span class="feedbackspan">{c["kept_block_feedback"]}</span>'
            html = html.replace(
                '<div class="qtext">', '<div class="qtext">' + inside, 1
            )
            html = html.replace('class="ablock', 'data-x="1" class="ablock', 1)
            html = html.replace(
                '<div class="answer">',
                '<div class="answer"><div class="specificfeedback">'
                + c["kept_block_feedback"]
                + "</div>",
                1,
            )
            q["html"] = html + outside
            # numeric canary for the mark, correctness canary for the status label
            # adversarial: even an unanswered question gets a mark, which must not leak when hidden
            q["mark"] = c["hidden_mark"]
            q["status"] = c["correctness"]
        case.forbidden.update(
            {
                v: (f, t)
                for slot, cs in per_q.items()
                for f, v in cs.items()
                for t in [_qtype_of(body, slot)]
            }
        )
        return body

    return mutate


def _qtype_of(body: Any, slot: int) -> str:
    return next(q["type"] for q in body["questions"] if q["slot"] == slot)


def _quiz_case(version: str, toggle: bool, html_path: str, review: str) -> Case:
    case = Case(
        name=f"quiz-{version}-{review}-{'on' if toggle else 'off'}-{html_path}",
        version=version,
        kind="quiz",
        toggle=toggle,
        html_path=html_path,
        review=review,
    )
    marks: dict[str, str] = {}
    case.mutate = _quiz_mutator(case, marks)
    return case


def finalize_quiz(case: Case) -> None:
    """After injection: move canaries the policy PERMITS from forbidden to allowed."""
    fb, gfb, ra, mk = REVIEW_VARIANTS[case.review or "all_hidden"]
    permitted = {
        "specific_feedback": bool(fb),
        "general_feedback": bool(gfb),
        "right_answer": bool(ra),
        "hidden_mark": mk == 2,
        "correctness": mk == 2,
        "review_html": False,
        "kept_block_feedback": False,
    }
    for c, (f, _t) in list(case.forbidden.items()):
        if permitted.get(
            f, False
        ):  # solution_text and other non-quiz fields: never here
            del case.forbidden[c]
            case.allowed.add(c)


# --- matrix --------------------------------------------------------------------------------------


def build_matrix() -> list[Case]:
    cases: list[Case] = []
    for version, toggle, html_path, review in itertools.product(
        VERSIONS, (False, True), ("structured_only", "filterable"), REVIEW_VARIANTS
    ):
        cases.append(_quiz_case(version, toggle, html_path, review))
    for version, toggle, shape in itertools.product(
        VERSIONS, (False, True), ASSIGN_SHAPES
    ):
        cases.append(
            Case(
                name=f"assign-{version}-{shape}-{'on' if toggle else 'off'}",
                version=version,
                kind="assign",
                toggle=toggle,
                html_path="structured_only",
                shape=shape,
            )
        )
    return cases


def required_cells() -> set[tuple[str, str]]:
    """Every forbidden field x type / shape cell the matrix must cover with >= 1 case."""
    cells = {(f, t) for f in QUIZ_FIELDS for t in QTYPES}
    cells |= {(f, s) for s, fs in ASSIGN_FIELDS.items() for f in fs}
    cells |= {("solution_text", "quiz")}
    return cells
