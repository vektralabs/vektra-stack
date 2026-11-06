"""Scoped value check (REQ-096a): grade values must match the cited item, other numbers are untouched."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_context_bundle import _ALL, _frozen, _with_review
from test_context_snapshot import make

from vektra_assess.context.assemble import assemble_context
from vektra_assess.llm.postcheck.values import (
    REPLACEMENT,
    asserts_any_value,
    check,
    value_index,
)

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


@pytest.mark.parametrize(
    "sentence",
    [
        "Nella [Domanda 2] hai ottenuto 2,5 punti su 5.",
        "In [Domanda 2] you scored 2.5 out of 5 points.",
        "La [Domanda 2] vale 5 punti.",  # a maximum presented as the maximum
        "Hai ottenuto 2,5/5 punti [Domanda 2].",  # the only label, cited after the value
    ],
)
def test_ratio_separators_and_roles_pass_when_right(sentence: str) -> None:
    assert check(sentence, _index()).text == sentence


@pytest.mark.parametrize(
    ("sentence", "replaced"),
    [
        # "su" / "out of" ratios are checked as a pair, not on the value alone
        ("Nella [Domanda 2] il punteggio è 2.5 su 6.", "2.5 su 6"),
        ("In [Domanda 2] your score is 2.5 out of 6.", "2.5 out of 6"),
        # a visible maximum never validates points obtained
        ("Per la [Domanda 2] hai ottenuto 5 punti.", "5"),
        ("La [Domanda 2] vale 2.5 punti.", "2.5"),  # an earned mark is not the maximum
        # two labels after the value: the attribution cannot be verified
        ("Hai ottenuto 2,5 punti nella Domanda 2 e nella Domanda 1.", "2,5"),
        ("Nella [Domanda 9] hai 2.5 punti.", "2.5"),  # cited item not in the bundle
        # the sign is part of the value
        ("Nella [Domanda 2] hai -2.5 punti.", "-2.5"),
        ("Nella [Domanda 2] hai \u22122,5 punti.", "\u22122,5"),
        ("Nella [Domanda 2] il punteggio è -2.5/5.", "-2.5/5"),
    ],
)
def test_role_attribution_ratio_and_sign_regressions(
    sentence: str, replaced: str
) -> None:
    out = check(sentence, _index())
    assert out.replaced == 1
    assert out.text == sentence.replace(replaced, REPLACEMENT, 1)


def test_each_value_is_checked_against_the_item_it_is_attributed_to() -> None:
    """1 point is right for Domanda 1 and wrong for Domanda 2: only the second is replaced."""
    out = check(
        "Nella [Domanda 1] hai 1 punto, nella [Domanda 2] hai 1 punto.", _index()
    )
    assert out.replaced == 1
    assert (
        out.text
        == f"Nella [Domanda 1] hai 1 punto, nella [Domanda 2] hai {REPLACEMENT} punto."
    )


def test_a_hidden_mark_equal_to_the_visible_maximum_is_not_confirmed() -> None:
    out = check("Per la [Domanda 2] hai ottenuto 5 punti.", _index(visible=False))
    assert out.replaced == 1 and "5 punti" not in out.text


@pytest.mark.parametrize(
    "text",
    [
        "Hai preso quattro punti.",
        "Your grade is seven.",
        "Il voto è sei.",
        "You got three points.",
    ],
)
def test_part_b_catches_spelled_out_values(text: str) -> None:
    assert asserts_any_value(text)


@pytest.mark.parametrize(
    "text",
    [
        "Ti propongo un punto da chiarire.",
        "One point is worth discussing.",
        "The score is one of the topics.",
    ],
)
def test_part_b_leaves_articles_and_plain_words_alone(text: str) -> None:
    assert not asserts_any_value(text)
