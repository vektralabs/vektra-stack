"""Verdict / merit / estimate / grade-change / teacher-intent / judgement lexicon (stage 6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vektra_assess.llm.postcheck import lexicon
from vektra_assess.llm.prompts import PromptRenderer
from vektra_assess.llm.runner import DiscussionTurnRunner

DATA = json.loads(
    (Path(__file__).parent / "fixtures" / "llm" / "lexicon.json").read_text(
        encoding="utf-8"
    )
)
HITS = [(cat, s) for cat, ss in DATA["hits"].items() for s in ss]


@pytest.mark.parametrize(("category", "sentence"), HITS)
def test_each_forbidden_sentence_is_caught_in_its_category(
    category: str, sentence: str
) -> None:
    assert lexicon.scan(sentence) == category


@pytest.mark.parametrize("sentence", DATA["clean"])
def test_contest_and_explanation_wording_is_not_caught(sentence: str) -> None:
    assert lexicon.scan(sentence) is None


def test_every_category_has_it_and_en_examples() -> None:
    assert set(DATA["hits"]) == set(lexicon.PATTERNS)
    assert all(len(v) >= 3 for v in DATA["hits"].values())


def test_the_lexicon_version_enters_the_prompt_version() -> None:
    assert len(lexicon.LEXICON_VERSION) == 8
    assert (
        PromptRenderer(lexicon.LEXICON_VERSION).prompt_version
        != PromptRenderer("other").prompt_version
    )
    assert "LEXICON_VERSION" in DiscussionTurnRunner.__init__.__code__.co_names
