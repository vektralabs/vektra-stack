"""Citation tags and verbatim teacher feedback (post-check stages 4 and 5)."""

from __future__ import annotations

import pytest
from test_context_bundle import _ALL, _with_review
from test_context_snapshot import make

from vektra_assess.context.assemble import assemble_context
from vektra_assess.llm.postcheck import citations, feedback_quote
from vektra_assess.llm.postcheck.feedback_quote import MISSING

LABELS = ("Domanda 1", "Domanda 2", "Criterio 1", "Valutazione complessiva", "Consegna")


@pytest.mark.parametrize(
    ("sentence", "resolved"),
    [
        ("Vedi [Domanda 2].", ("Domanda 2",)),
        ("Vedi [domanda  1] e [Criterio 1].", ("Domanda 1", "Criterio 1")),
        ("Lo dici in [Consegna p.4].", ("Consegna",)),
        ("Nel complesso [Valutazione complessiva].", ("Valutazione complessiva",)),
    ],
)
def test_resolved_tags_are_kept(sentence: str, resolved: tuple[str, ...]) -> None:
    out = citations.check(sentence, LABELS)
    assert out.text == sentence and out.removed == 0 and out.resolved == resolved


@pytest.mark.parametrize(
    ("sentence", "expected"),
    [
        ("Vedi [Domanda 7].", "Vedi."),
        ("Come in [Criterio 4], manca il punto.", "Come in, manca il punto."),
        ("Lo spiega il libro [Materiale: Cap. 3].", "Lo spiega il libro."),
        ("Vedi [Consegna p.0].", "Vedi."),
        ("Vedi [Question 1].", "Vedi."),
    ],
)
def test_unresolved_and_material_tags_are_removed(sentence: str, expected: str) -> None:
    out = citations.check(sentence, LABELS)
    assert out.text == expected and out.removed == 1


def test_material_is_removed_even_if_a_label_had_that_name() -> None:
    assert citations.check("[Materiale: x]", (*LABELS, "Materiale: x")).removed == 1


def test_brackets_that_are_not_tags_are_text() -> None:
    s = "L'intervallo [0.5, 1.5] e la matrice [[1, 2], [3, 4]] restano."
    assert citations.check(s, LABELS).text == s


INDEX = {
    "domanda 2": "Buona impostazione, ma manca la conclusione.",
    "criterio 1": "Analisi corretta  ma superficiale.",
}


@pytest.mark.parametrize(
    "sentence",
    [
        'Per la [Domanda 2], Commento del docente: "manca la conclusione".',
        "Commento del docente: «Analisi corretta ma superficiale.» [Criterio 1]",
        "Commento del docente: “Buona impostazione”.",  # no item cited: any feedback
    ],
)
def test_an_exact_quote_passes(sentence: str) -> None:
    out = feedback_quote.check(sentence, INDEX)
    assert out.text == sentence and out.replaced == 0


@pytest.mark.parametrize(
    "sentence",
    [
        'Per la [Domanda 2], Commento del docente: "ottimo lavoro".',
        'Per il [Criterio 1], Commento del docente: "manca la conclusione".',  # wrong item
        'Commento del docente: "".',
    ],
)
def test_an_inexact_quote_is_replaced(sentence: str) -> None:
    out = feedback_quote.check(sentence, INDEX)
    assert out.replaced == 1 and MISSING in out.text and '"' not in out.text


def test_feedback_index_holds_only_what_the_bundle_shows() -> None:
    s = make()
    s = s.model_copy(
        update={"overall": s.overall.model_copy(update={"overall_feedback": "Bene."})}
    )
    b = assemble_context(s, _with_review(s, _ALL, "unreadable"))
    idx = feedback_quote.feedback_index(b)
    assert idx["valutazione complessiva"] == "Bene."
    assert "domanda 2" not in idx  # structured-only path: no review text in the bundle


def test_teacher_comment_says_lead_is_a_quote_lead() -> None:
    ok = 'Il Commento del docente dice: "manca la conclusione".'
    assert feedback_quote.check(ok, INDEX).text == ok
    out = feedback_quote.check('Il Commento del docente dice: "ottimo lavoro".', INDEX)
    assert out.replaced == 1 and "ottimo" not in out.text


@pytest.mark.parametrize(
    "sentence",
    [
        # a cited item with no feedback confirms nothing: no fallback to the other items
        'Per la [Domanda 3], Commento del docente: "manca la conclusione".',
        'Nella Domanda 9 il Commento del docente: "Analisi corretta".',
    ],
)
def test_a_quote_citing_an_item_without_feedback_is_replaced(sentence: str) -> None:
    out = feedback_quote.check(sentence, INDEX)
    assert out.replaced == 1 and MISSING in out.text


def test_running_text_question_word_is_not_a_citation() -> None:
    s = 'La domanda chiedeva altro. Commento del docente: "manca la conclusione".'
    assert feedback_quote.check(s, INDEX).text == s


@pytest.mark.parametrize(
    "sentence",
    [
        'Commento del docente: "manca la conclusione e poi il resto',
        "Teacher\u2019s comment: “missing the conclusion",
        "Per la [Domanda 2], Commento del docente: «Buona impostazione.",
    ],
)
def test_an_unclosed_teacher_quote_is_replaced_to_the_end(sentence: str) -> None:
    out = feedback_quote.check(sentence, INDEX)
    assert out.replaced == 1 and out.text.endswith(MISSING)
