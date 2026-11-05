"""Part B checks: no values, <= 120 words, <= 3 labels (REQ-094)."""

from __future__ import annotations

import pytest

from vektra_assess.llm.postcheck import part_b

GOOD = (
    "Ciao! In questa discussione possiamo guardare insieme la tua prova. "
    "Potresti partire dalla [Domanda 2], dove il commento del docente parla della conclusione, "
    "oppure dal [Criterio 1]. Quale elemento vuoi discutere per primo?"
)


def test_a_neutral_orientation_passes() -> None:
    out = part_b.check(GOOD)
    assert out.ok and out.violations == () and out.labels == 2


@pytest.mark.parametrize(
    "text",
    [
        "Nella [Domanda 2] hai ottenuto 2,5 punti.",
        "Il tuo voto è 7.",
        "Hai raggiunto il livello 3 nel [Criterio 1].",
        "La [Domanda 1] è 1/1.",
        "Hai risposto bene a 3/5 domande.",
        "You scored 4 points on [Domanda 1].",
    ],
)
def test_any_value_fails(text: str) -> None:
    assert part_b.VALUE in part_b.check(text).violations


def test_more_than_120_words_fails() -> None:
    text = " ".join(["parola"] * 121)
    out = part_b.check(text)
    assert part_b.WORDS in out.violations and out.words == 121
    assert part_b.check(" ".join(["parola"] * 120)).ok


def test_more_than_three_labels_fails() -> None:
    text = (
        "Guarda [Domanda 1], [Domanda 2], [Criterio 1] e la [Valutazione complessiva]."
    )
    out = part_b.check(text)
    assert out.violations == (part_b.LABELS,) and out.labels == 4


def test_the_same_label_twice_counts_once() -> None:
    assert part_b.check("[Domanda 1] e ancora Domanda 1.").labels == 1


def test_empty_fails() -> None:
    assert part_b.check("  ").violations == (part_b.EMPTY,)


def test_numbers_that_are_not_values_pass() -> None:
    assert part_b.check("Partiamo dalla [Domanda 2] e dal passaggio a pagina 3.").ok
