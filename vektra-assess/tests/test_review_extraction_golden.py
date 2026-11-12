"""The profile-driven review extraction reproduces the old built-in one, byte for byte (BR-011).

``golden/review_extraction_moodle.json`` was generated from ``html_review`` BEFORE ADR-0033 D4
moved the CSS classes out of the sidecar (commit c57b0535, code at cbd22067): one sha256 per
block, per cap and per item reply, for every question of every recorded 4.3 / 5.2 attempt
review. With the Moodle provider's profile the new code must give the same status, code and
text for every one of them: what reaches the model has not changed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from vektra_assess.moodle.provider import MOODLE_REVIEW_PROFILE
from vektra_assess.parser_protocol import ReviewProfile
from vektra_assess.parser_sidecar.html_review import (
    extract_review_block,
    extract_review_item,
)

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures" / "moodle_ws"
GOLDEN = json.loads(
    (HERE / "golden" / "review_extraction_moodle.json").read_text(encoding="utf-8")
)["cases"]
PROFILE = ReviewProfile.build(
    MOODLE_REVIEW_PROFILE.keep,
    MOODLE_REVIEW_PROFILE.drop_classes,
    MOODLE_REVIEW_PROFILE.profile_version,
)


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _questions() -> dict[str, bytes]:
    out = {}
    for f in sorted(FIXTURES.glob("*/mod_quiz_get_attempt_review__*.json")):
        doc = json.loads(f.read_text())
        for q in doc.get("response", doc).get("questions", []):
            out[f"{f.parent.name}/{f.stem.split('__')[1]}/q{q['slot']}"] = q[
                "html"
            ].encode()
    return out


QUESTIONS = _questions()


def _now(html: bytes) -> dict[str, list[Any]]:
    entry: dict[str, list[Any]] = {}
    for block in ("question_text", "response"):
        for cap in (100_000, 50):
            e = extract_review_block(html, block, cap, PROFILE)
            entry[f"{block}@{cap}"] = [e.status.value, e.code, _h(e.text)]
    for cap in (200_000, 300):
        e = extract_review_item(html, cap, PROFILE)
        d = json.loads(e.text)
        entry[f"item@{cap}"] = [
            e.status.value,
            e.code,
            _h(
                json.dumps(
                    [d["question_text"], d["response"], d["truncated"]],
                    ensure_ascii=False,
                )
            ),
        ]
    return entry


def test_the_golden_covers_every_recorded_question() -> None:
    """False-pass guard: an empty or partial golden would prove nothing."""
    assert len(GOLDEN) == 162
    assert set(GOLDEN) == set(QUESTIONS)
    assert all(len(v) == 6 for v in GOLDEN.values())


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_same_html_same_extracted_text(name: str) -> None:
    assert _now(QUESTIONS[name]) == GOLDEN[name], name
