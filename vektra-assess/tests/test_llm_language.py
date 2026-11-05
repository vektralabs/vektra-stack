"""Reply-language detection (REQ-100) on a 200-message IT / EN / other fixture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vektra_assess.llm.language import detect

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "llm" / "languages.json").read_text(
        encoding="utf-8"
    )
)


def test_the_fixture_has_200_mixed_messages() -> None:
    assert len(CASES) == 200
    assert {c["expect"] for c in CASES} == {"it", "en", "other"}


@pytest.mark.parametrize("lang", ["it", "en", "other"])
def test_accuracy_per_language_is_at_least_95_percent(lang: str) -> None:
    cases = [c for c in CASES if c["expect"] == lang]
    wrong = [c["text"] for c in cases if detect(c["text"]) != lang]
    assert len(wrong) / len(cases) <= 0.05, wrong


@pytest.mark.parametrize("text", ["", "ok", "Domanda 3?", "3.5/10", "B"])
def test_no_signal_defaults_to_italian(text: str) -> None:
    assert detect(text) == "it"


def test_a_tie_defaults_to_italian() -> None:
    assert detect("il the") == "it"
