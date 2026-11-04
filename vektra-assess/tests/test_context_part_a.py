"""Part A golden renderings (REQ-094 AC1/AC4, BR-009, BR-014 AC3).

``VEKTRA_UPDATE_GOLDEN=1 pytest vektra-assess/tests/test_context_part_a.py`` rewrites the files;
the diff is then REVIEWED before it is committed. Without the variable a difference fails.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import pytest
from golden_part_a_cases import Case, cases

from vektra_assess.context import strings as S
from vektra_assess.context.assemble import assemble_context
from vektra_assess.context.part_a import render_part_a
from vektra_assess.context.snapshot import GradeKind, MarksHidden

#: one file, one block per case ("=== <name> ===" then the rendering): 60 separate files would
#: push the plan's PR past the review tool's 100-file cap
GOLDEN_FILE = Path(__file__).parent / "golden" / "part_a.txt"
UPDATE = os.environ.get("VEKTRA_UPDATE_GOLDEN") == "1"
CASES: list[Case] = asyncio.run(cases())
_HEAD = re.compile(r"^=== (?P<name>[\w.-]+) ===$", re.M)


def _render(case: Case) -> str:
    return render_part_a(assemble_context(case.snapshot, case.frozen), case.facts).text


def _load() -> dict[str, str]:
    if UPDATE:
        GOLDEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_FILE.write_text(
            "".join(f"=== {c.name} ===\n{_render(c)}\n" for c in CASES),
            encoding="utf-8",
        )
    text = GOLDEN_FILE.read_text(encoding="utf-8")
    heads = list(_HEAD.finditer(text))
    out: dict[str, str] = {}
    for h, nxt in zip(heads, [*heads[1:], None], strict=True):
        body = text[h.end() + 1 : nxt.start() if nxt else len(text)]
        out[h["name"]] = body[:-1] if body.endswith("\n") else body
    return out


GOLDEN = _load()


def test_there_are_at_least_fifty_cases_and_names_are_unique() -> None:
    names = [c.name for c in CASES]
    assert len(names) >= 50 and len(set(names)) == len(names)
    kinds = {
        (c.snapshot.header.moodle_release[:3], c.snapshot.header.activity_kind.value)
        for c in CASES
    }
    assert len(kinds) == 4, kinds  # 4.3/5.2 x quiz/assignment
    assert any(c.snapshot.overall.grade_kind is GradeKind.SCALE for c in CASES)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_part_a_equals_the_golden_rendering(case: Case) -> None:
    assert case.name in GOLDEN, (
        f"no golden block for {case.name}: run with VEKTRA_UPDATE_GOLDEN=1 and review"
    )
    assert _render(case) == GOLDEN[case.name]


def test_no_stale_golden_blocks() -> None:
    assert sorted(set(GOLDEN) - {c.name for c in CASES}) == []


_BR009 = re.compile(
    "^"
    + re.escape(S.BR009_TEMPLATE)
    .replace(r"\{grade\}", "(?P<grade>.+)")
    .replace(r"\{date\}", r"(?P<date>\d\d/\d\d/\d{4})")
    + "$",
    re.M,
)


def test_br009_wording_is_the_required_literal() -> None:
    """Independent of the golden file: regenerating it cannot hide a change of the wording."""
    assert S.BR009_TEMPLATE == (
        "Voto registrato: {grade} (letto il {date}). Il voto NON è stato modificato da questa "
        "discussione; decide l'orale"
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_br009_statement_is_the_shipped_constant_byte_for_byte(case: Case) -> None:
    text = GOLDEN[case.name]
    matches = list(_BR009.finditer(text))
    assert len(matches) == 1
    line = matches[0].group(0)
    assert line == S.BR009_TEMPLATE.format(
        grade=matches[0]["grade"], date=matches[0]["date"]
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_hidden_marks_never_appear_and_scale_has_no_points(case: Case) -> None:
    text = GOLDEN[case.name]
    for item in case.snapshot.quiz_items:
        line = next(
            ln for ln in text.splitlines() if ln.startswith(f"- Domanda {item.label}:")
        )
        if item.marks_hidden is MarksHidden.REVIEW_OPTIONS:
            assert not re.search(r"\d+(,\d+)?/\d", line), line  # no "x/max" at all
            if item.max_mark is None:
                assert not re.search(r"\d", line.split(":", 1)[1]), line
    if case.snapshot.overall.grade_kind is GradeKind.SCALE:
        overall = next(
            ln for ln in text.splitlines() if ln.startswith(f"- {S.OVERALL_LABEL}")
        )
        assert "/" not in overall and "punti" not in text
        assert case.snapshot.overall.scale_label in overall


def test_solution_label_follows_the_toggle() -> None:
    for case in CASES:
        text = GOLDEN[case.name]
        shown = case.snapshot.solution_text is not None
        assert (S.SOLUTION[True] in text) is shown, case.name
        assert (S.SOLUTION[False] in text) is (not shown), case.name


def test_file_statuses_use_the_req_101_strings() -> None:
    case = next(c for c in CASES if c.name.endswith("assign_files_all"))
    text = GOLDEN[case.name]
    for s in (
        "letto", "letto parzialmente (fino a pagina 50)", "non letto: formato non supportato",
        "non letto: troppo grande", "non letto: testo non estraibile", "non letto: protetto",
        "non letto: file danneggiato o non elaborabile",
    ):  # fmt: skip
        assert f": {s}\n" in text, s


def test_html_variant_escapes_student_controlled_names() -> None:
    case = next(c for c in CASES if c.name.endswith("assign_files_all"))
    part = render_part_a(assemble_context(case.snapshot, case.frozen), case.facts)
    assert "<script>" in part.text  # plain text: consumers escape
    assert "<script>" not in part.html and "&lt;script&gt;" in part.html


def test_rendering_is_deterministic() -> None:
    case = CASES[0]
    a = render_part_a(assemble_context(case.snapshot, case.frozen), case.facts)
    b = render_part_a(assemble_context(case.snapshot, case.frozen), case.facts)
    assert a == b


def test_technical_flags_stay_out_of_the_student_text() -> None:
    for case in CASES:
        text = GOLDEN[case.name]
        assert (
            "structured" not in text and "policy" not in text and "fallback" not in text
        )
