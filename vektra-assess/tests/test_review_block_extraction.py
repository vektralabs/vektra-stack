"""Allowlist extraction of a question's text and response (ADR-0032 amendment 2026-11-04).

The oracle is the recorded review HTML itself: for every question of every recorded 4.3 / 5.2
attempt, the text of every block the extraction must drop (right answer, specific / general /
per-choice feedback, outcome, grader comment, response history) is collected, and none of it may
appear in what the extraction returns. The seed canaries (``CANARY-*``) may not appear either,
and the visible question body (``VISIBLE-QTEXT-*``) must.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import html5lib
import pytest

from vektra_assess.context.qtypes import EXTRACTION_QTYPES
from vektra_assess.parser_protocol import FileStatus
from vektra_assess.parser_sidecar.html_review import extract_review_block

FIXTURES = Path(__file__).parent / "fixtures" / "moodle_ws"
EXCLUDED = {
    "outcome",
    "rightanswer",
    "specificfeedback",
    "generalfeedback",
    "history",
    "feedbackspan",
    "comment",
    "feedback",
}


def _questions() -> list[tuple[str, dict[str, Any]]]:
    out = []
    for f in sorted(FIXTURES.glob("*/mod_quiz_get_attempt_review__*.json")):
        doc = json.loads(f.read_text())
        for q in doc.get("response", doc).get("questions", []):
            out.append((f"{f.parent.name}/{f.stem.split('__')[1]}/q{q['slot']}", q))
    return out


QUESTIONS = _questions()


def _excluded_texts(html: str) -> set[str]:
    doc = html5lib.parse(html, treebuilder="etree", namespaceHTMLElements=False)
    out: set[str] = set()
    for el in doc.iter():
        if isinstance(el.tag, str) and set((el.get("class") or "").split()) & EXCLUDED:
            for t in el.itertext():
                t = re.sub(r"\s+", " ", t).strip()
                if (
                    len(t) >= 6
                ):  # shorter strings are option labels shared with the answer
                    out.add(t)
    return out


def test_the_oracle_has_material() -> None:
    types = {q["type"] for _, q in QUESTIONS}
    assert EXTRACTION_QTYPES <= types, "every enabled type has recordings"
    assert len(QUESTIONS) > 100
    with_excluded = [
        n
        for n, q in QUESTIONS
        if q["type"] in EXTRACTION_QTYPES and _excluded_texts(q["html"])
    ]
    assert len(with_excluded) > 50, "the excluded blocks are really there to be dropped"


@pytest.mark.parametrize(
    ("name", "q"),
    [p for p in QUESTIONS if p[1]["type"] in EXTRACTION_QTYPES],
    ids=lambda x: x if isinstance(x, str) else "",
)
def test_enabled_types_keep_question_and_answer_and_drop_the_rest(
    name: str, q: dict[str, Any]
) -> None:
    html = q["html"].encode()
    qtext = extract_review_block(html, "qtext", 100_000)
    resp = extract_review_block(html, "response", 100_000)
    assert qtext.status is FileStatus.READ, (name, qtext.code)
    assert re.search(r"VISIBLE-QTEXT-\w+", qtext.text), name
    out = qtext.text + "\n" + resp.text
    leaked = sorted(t for t in _excluded_texts(q["html"]) if t in out)
    assert leaked == [], (name, leaked)
    assert "CANARY-" not in out, name
    assert "The correct answer" not in out and "La risposta corretta" not in out, name


def test_cloze_and_drag_types_fail_closed() -> None:
    for name, q in QUESTIONS:
        if q["type"] == "multianswer":
            e = extract_review_block(q["html"].encode(), "qtext", 100_000)
            assert (e.status, e.code) == (FileStatus.NO_TEXT, "block_missing"), name
        if q["type"] == "ddwtos":
            e = extract_review_block(q["html"].encode(), "response", 100_000)
            assert e.status is FileStatus.NO_TEXT, name


def test_the_essay_grader_comment_never_comes_out() -> None:
    hits = [n for n, q in QUESTIONS if "CANARY-ESSAYCOMMENT" in q["html"]]
    assert hits, "the recordings carry the grader-comment canary"
    for name, q in QUESTIONS:
        if "CANARY-ESSAYCOMMENT" in q["html"]:
            for block in ("qtext", "response"):
                assert (
                    "CANARY-ESSAYCOMMENT"
                    not in extract_review_block(q["html"].encode(), block, 100_000).text
                ), name


def test_answers_given_are_kept() -> None:
    by_type: dict[str, list[str]] = {}
    for _, q in QUESTIONS:
        if q["type"] in EXTRACTION_QTYPES and q.get("state", "").startswith("graded"):
            by_type.setdefault(q["type"], []).append(
                extract_review_block(q["html"].encode(), "response", 100_000).text
            )
    assert any("[x]" in t for t in by_type["multichoice"])
    assert any("[x]" in t for t in by_type["truefalse"])
    assert any("[risposta: " in t for t in by_type["shortanswer"])
    assert any("[risposta: " in t for t in by_type["numerical"])


@pytest.mark.parametrize(
    ("html", "code"),
    [
        (b"<div class='que'><p>no blocks</p></div>", "block_missing"),
        (b"<div class='qtext'>a</div><div class='qtext'>b</div>", "block_ambiguous"),
    ],
)
def test_unexpected_shapes_fail_closed(html: bytes, code: str) -> None:
    e = extract_review_block(html, "qtext", 1000)
    assert (e.status, e.code, e.text) == (FileStatus.NO_TEXT, code, "")


def test_feedback_nested_in_a_kept_block_is_pruned_and_tails_survive() -> None:
    html = (
        b"<div class='qtext'>Quanto fa 2+2? <span class='feedbackspan'>Giusto: 4</span>"
        b" fine domanda<div class='specificfeedback'>Bravo</div> coda</div>"
    )
    e = extract_review_block(html, "qtext", 1000)
    assert "Giusto" not in e.text and "Bravo" not in e.text
    assert "Quanto fa 2+2?" in e.text and "fine domanda" in e.text and "coda" in e.text


def test_unknown_block_name_is_refused() -> None:
    assert (
        extract_review_block(b"<div class='qtext'>a</div>", "rightanswer", 100).code
        == "unknown_block"
    )


def test_the_child_returns_both_blocks_from_one_parse() -> None:
    import json

    from vektra_assess.parser_sidecar.child import run

    html = b"<div class='qtext'>Domanda</div><fieldset class='ablock'>[x] A</fieldset>"
    doc = json.loads(run("review_item", html, "html", max_pages=1, max_chars=100).text)
    assert doc["qtext"] == "Domanda" and "A" in doc["response"]
    doc = json.loads(
        run("review_item", b"<p>nothing</p>", "html", max_pages=1, max_chars=100).text
    )
    assert doc == {"qtext": None, "response": None, "truncated": False}
    big = b"<div class='qtext'>" + b"x" * 500 + b"</div>"
    out = run("review_item", big, "html", max_pages=1, max_chars=200).text
    assert len(out) <= 200 and json.loads(out)["qtext"]  # the JSON itself fits the cap
    assert json.loads(out)["truncated"] is True  # and the cut is flagged


@pytest.mark.parametrize(
    "icon",
    [
        '<img class="icon questioncorrectnessicon" alt="Correct" src="x.svg">',
        '<i class="icon fa fa-check text-success" title="Correct"></i>',
    ],
)
def test_correctness_icons_inside_the_response_are_dropped(icon: str) -> None:
    html = f"<fieldset class='ablock'><div class='answer'><div class='r0 correct'>[x] Vero {icon}</div></div></fieldset>"
    text = extract_review_block(html.encode(), "response", 1000).text
    assert "Vero" in text and "Correct" not in text


def test_review_item_json_fits_the_cap_even_when_escaping_grows_it() -> None:
    from vektra_assess.parser_sidecar.child import run

    # every quote and backslash doubles under JSON escaping: half-capped values alone overflow
    body = '"\\' * 200
    html = (
        f"<div class='qtext'>{body}</div>"
        f"<fieldset class='ablock'><div class='answer'>{body}</div></fieldset>"
    ).encode()
    out = run("review_item", html, "html", max_pages=1, max_chars=300).text
    assert len(out) <= 300
    doc = json.loads(out)  # still valid JSON, not cut
    assert doc["qtext"] and doc["response"]


def test_review_item_flags_a_block_cut_at_its_cap() -> None:
    from vektra_assess.parser_sidecar.child import run

    html = (
        "<div class='qtext'>" + "parola " * 400 + "</div>"
        "<fieldset class='ablock'><div class='answer'>breve</div></fieldset>"
    ).encode()
    doc = json.loads(run("review_item", html, "html", max_pages=1, max_chars=400).text)
    assert doc["truncated"] is True and doc["response"] == "breve"
    short = b"<div class='qtext'>Domanda breve</div>"
    doc = json.loads(run("review_item", short, "html", max_pages=1, max_chars=400).text)
    assert doc["truncated"] is False


def test_review_item_flags_values_shortened_by_the_json_cap() -> None:
    from vektra_assess.parser_sidecar.child import run

    body = '"\\' * 90  # each block fits its half cap, but escaping overflows the JSON
    html = (
        f"<div class='qtext'>{body}</div>"
        f"<fieldset class='ablock'><div class='answer'>{body}</div></fieldset>"
    ).encode()
    out = run("review_item", html, "html", max_pages=1, max_chars=300).text
    doc = json.loads(out)
    assert len(out) <= 300 and doc["truncated"] is True
