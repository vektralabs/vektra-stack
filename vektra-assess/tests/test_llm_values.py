"""Scoped value check (REQ-096a): grade values must match the cited item, other numbers are untouched."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_context_bundle import _ALL, _frozen, _with_review
from test_context_snapshot import make

from vektra_assess.context.assemble import assemble_context
from vektra_assess.llm.postcheck.values import REPLACEMENT, check, value_index

NO_VALUES = json.loads(
    (Path(__file__).parent / "fixtures" / "llm" / "no_values.json").read_text(
        encoding="utf-8"
    )
)


def _index(visible: bool = True):  # type: ignore[no-untyped-def]
    s = make()
    frozen = _with_review(s, _ALL, "unreadable") if visible else _frozen(s)
    return value_index(assemble_context(s, frozen))


@pytest.mark.parametrize(
    "sentence",
    [
        "Per la [Domanda 2] hai ottenuto 2,5/5 punti.",
        "Nella Domanda 2 il punteggio è 2.5 su 5.",
        "La [Domanda 1] vale 1 punto su 1: punteggio 1/1.",
        "La [Valutazione complessiva] è 7,5/10.",
        "Il voto della valutazione complessiva è 7.5.",
        "In [Domanda 2] you scored 2.5 points.",
    ],
)
def test_correct_values_pass_untouched(sentence: str) -> None:
    assert check(sentence, _index()).text == sentence


@pytest.mark.parametrize(
    ("sentence", "replaced"),
    [
        ("Per la [Domanda 2] hai ottenuto 3/5 punti.", "3/5"),
        ("Nella Domanda 2 il punteggio è 4.", "4"),
        ("La [Valutazione complessiva] è 8/10.", "8/10"),
        ("Hai preso 4 punti.", "4"),  # no item to check it against
        ("In [Domanda 1] you scored 3 points.", "3"),
        (
            "Per la [Domanda 2] hai ottenuto 2,5/6.",
            "2,5/6",
        ),  # right value, wrong maximum
    ],
)
def test_a_wrong_or_unanchored_value_is_replaced(sentence: str, replaced: str) -> None:
    out = check(sentence, _index())
    assert out.replaced == 1 and REPLACEMENT in out.text
    assert out.text == sentence.replace(replaced, REPLACEMENT, 1)


def test_a_hidden_mark_is_never_confirmed() -> None:
    """Marks hidden by the review options: even the true value is replaced."""
    out = check("Per la [Domanda 2] hai ottenuto 2.5 punti.", _index(visible=False))
    assert out.replaced == 1 and "2.5" not in out.text


@pytest.mark.parametrize("sentence", NO_VALUES)
def test_numbers_that_are_not_grade_values_are_untouched(sentence: str) -> None:
    assert check(sentence, _index()).text == sentence


def test_false_replacements_stay_under_one_percent() -> None:
    idx = _index()
    false = sum(check(s, idx).replaced > 0 for s in NO_VALUES)
    assert false / len(NO_VALUES) <= 0.01
