"""The parser sidecar's review-HTML extraction with a NEUTRAL profile (ADR-0033 D4).

The sidecar holds no LMS's class names: the provider sends the profile (which classes hold the
question text and the response, which ones carry what the LMS hides). These tests use made-up
class names, so they pin the generic contract: exactly one block or nothing (fail closed), the
dropped classes removed with their whole subtree but the surrounding text kept, the caps, and
the item's JSON never cut into something unreadable. The Moodle profile's own oracle is the
provider's suite (``adapters/vektra-lms-moodle/tests/test_review_block_extraction.py``).
"""

from __future__ import annotations

import json

from vektra_assess.parser_protocol import FileStatus, ReviewProfile
from vektra_assess.parser_sidecar.html_review import (
    extract_review_block,
    extract_review_item,
)

PROFILE = ReviewProfile.build(
    {"question_text": "q-body", "response": "q-answer"},
    {"q-hidden", "q-key"},
    "neutral-1",
)


def _block(html: str, block: str = "question_text", max_chars: int = 1_000):
    return extract_review_block(html.encode(), block, max_chars, PROFILE)


def test_a_block_the_profile_does_not_name_is_refused() -> None:
    only_q = ReviewProfile.build({"question_text": "q-body"}, set(), "neutral-2")
    res = extract_review_block(b"<div class='q-body'>x</div>", "response", 100, only_q)
    assert (res.status, res.code) == (FileStatus.CORRUPT, "unknown_block")


def test_exactly_one_block_or_nothing() -> None:
    assert _block("<div class='other'>x</div>").code == "block_missing"
    two = "<div class='q-body'>a</div><div class='q-body'>b</div>"
    res = _block(two)
    assert (res.status, res.code) == (FileStatus.NO_TEXT, "block_ambiguous")


def test_a_dropped_class_goes_with_its_subtree_and_the_text_around_it_stays() -> None:
    html = (
        "<div class='q-body'><span class='q-key'>SECRET-1</span> prima "
        "<b>grassetto</b><div class='q-hidden'>SECRET-2 <i>x</i></div> dopo</div>"
    )
    res = _block(html)
    assert res.status is FileStatus.READ
    assert "SECRET" not in res.text
    assert "prima" in res.text and "grassetto" in res.text and "dopo" in res.text


def test_an_empty_block_has_no_text_and_a_long_one_is_partial() -> None:
    assert (
        _block("<div class='q-body'> <span class='q-key'>x</span> </div>").code
        == "empty"
    )
    res = _block("<div class='q-body'>" + "a" * 50 + "</div>", max_chars=10)
    assert (res.status, res.text, res.code) == (
        FileStatus.READ_PARTIAL,
        "a" * 10,
        "char_cap",
    )


def test_a_formula_keeps_its_tex_source() -> None:
    html = (
        "<div class='q-body'>Calcola <math><semantics><mi>x</mi>"
        "<annotation encoding='application/x-tex'>x^2</annotation></semantics></math></div>"
    )
    assert "x^2" in _block(html).text


def test_a_text_area_keeps_its_lines_and_blank_runs_are_collapsed() -> None:
    html = "<div class='q-answer'><textarea>riga 1\n\n\n\nriga 2</textarea></div>"
    text = _block(html, "response").text
    assert "riga 1" in text and "riga 2" in text
    assert "\n\n\n" not in text  # at most one blank line in a row


def test_the_item_is_both_blocks_as_json_and_a_missing_one_is_null() -> None:
    res = extract_review_item(
        b"<div class='q-body'>Domanda</div><div class='q-answer'>Risposta</div>",
        1_000,
        PROFILE,
    )
    assert json.loads(res.text) == {
        "question_text": "Domanda",
        "response": "Risposta",
        "truncated": False,
    }
    res = extract_review_item(b"<div class='q-body'>Domanda</div>", 1_000, PROFILE)
    assert json.loads(res.text)["response"] is None


def test_json_escaping_never_pushes_the_item_past_its_cap() -> None:
    """Quotes and backslashes double under JSON escaping: the values are shortened to the longest
    length that fits, and the item is marked truncated rather than cut into broken JSON."""
    quoted = '"\\' * 200
    html = f"<div class='q-body'>{quoted}</div><div class='q-answer'>{quoted}</div>"
    res = extract_review_item(html.encode(), 300, PROFILE)
    assert len(res.text) <= 300
    doc = json.loads(res.text)  # still valid JSON
    assert doc["truncated"] is True
    assert doc["question_text"] and set(doc["question_text"]) <= {'"', "\\"}
