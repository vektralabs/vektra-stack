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


# --- pasted-rubric criteria (end-to-end test 2026-11-15) ----------------------------------------------

_RUBRIC = (
    "Criterio 1 - Modello modale (8 punti): tre stadi.\n"
    "Criterio 2 - Memoria di lavoro (10 punti): componenti.\n"
)


def _pasted_index():  # type: ignore[no-untyped-def]
    from test_context_rendering_variants import _assign

    s = _assign(pasted_rubric=_RUBRIC, criteria=())
    flags = s.header.flags.model_copy(update={"rubric_source": "pasted"})
    s = s.model_copy(update={"header": s.header.model_copy(update={"flags": flags})})
    return value_index(assemble_context(s, _frozen(s)))


def test_what_a_pasted_criterion_is_worth_is_confirmed() -> None:
    idx = _pasted_index()
    for sentence in (
        "Il commento punta sul Criterio 2 (Memoria di lavoro, 10 punti).",
        "Il Criterio 1 vale 8 punti.",
    ):
        assert check(sentence, idx).replaced == 0, sentence


def test_a_score_obtained_on_a_pasted_criterion_is_never_confirmed() -> None:
    idx = _pasted_index()
    for sentence in (
        "Nel Criterio 2 hai ottenuto 10 punti.",
        "Nel Criterio 2 hai 10 punti.",
        "Il Criterio 2 vale 9 punti.",
        "Il Criterio 3 vale 6 punti.",
    ):
        assert check(sentence, idx).replaced == 1, sentence


def test_a_pasted_criterion_is_a_citable_label_through_the_gate() -> None:
    from test_context_rendering_variants import _assign

    from vektra_assess.llm import shipped as shipped_strings
    from vektra_assess.llm.postcheck.gate import SentenceGate

    s = _assign(pasted_rubric=_RUBRIC, criteria=())
    flags = s.header.flags.model_copy(update={"rubric_source": "pasted"})
    s = s.model_copy(update={"header": s.header.model_copy(update={"flags": flags})})
    gate = SentenceGate(
        assemble_context(s, _frozen(s)), shipped_strings.get("it"), "BR009"
    )
    sentence = "Il commento riguarda il [Criterio 2] (Memoria di lavoro, 10 punti)."
    (unit,) = gate.feed(sentence)
    assert unit.text == sentence and not unit.marked and gate.marks == []


@pytest.mark.parametrize(
    "sentence",
    [
        "Nel [Criterio 2] hai ottenuto il massimo: 10 punti.",
        "Hai ottenuto nel [Criterio 2] 10 punti.",
        "Hai ottenuto nel [Criterio 2] (10 punti).",
    ],
)
def test_earned_wording_anywhere_in_the_clause_is_never_confirmed(
    sentence: str,
) -> None:
    assert check(sentence, _pasted_index()).replaced == 1


def test_a_zero_padded_pasted_criterion_is_indexed_by_its_number() -> None:
    from vektra_assess.llm.postcheck.values import _pasted_maxima

    assert "criterio 2" in _pasted_maxima("Criterio 02 - Memoria (10 punti)")


# --- rubric sub-scores taken from the context (F1 2026-11-19) ---------------------------------------

_SUB_RUBRIC = (
    "Criterio 1 - Condizionamento classico (10 punti): definizioni corrette (4), ruolo di stimolo "
    "e conseguenza (3), due esempi pertinenti (3). Criterio 2 - Rinforzo e punizione (10 punti): "
    "effetto aumenta/diminuisce (4), significato di positivo/negativo (3), esempi (3). Criterio 3 "
    "- Tolman (10 punti): esperimento descritto (4), apprendimento latente (3), S-O-R contro S-R (3)."
)


def _sub_index(solution: str | None = None):  # type: ignore[no-untyped-def]
    """The F1 shape: a quiz (Domanda 1 1/1, Domanda 2 2,5/5) with a pasted rubric."""
    s = make(pasted_rubric=_SUB_RUBRIC, solution_text=solution)
    if (
        solution is not None
    ):  # the solution is in context only with the teacher's toggle
        flags = s.header.flags.model_copy(update={"show_solution": True})
        s = s.model_copy(
            update={"header": s.header.model_copy(update={"flags": flags})}
        )
    return value_index(assemble_context(s, _with_review(s, _ALL, "unreadable")))


@pytest.mark.parametrize(
    "sentence",
    [
        "[Criterio 2]: la rubrica valuta soprattutto «effetto aumenta/diminuisce» (4 punti) e "
        "«significato di positivo/negativo» (3 punti).",
        "Secondo la rubrica avresti dovuto descrivere l'esperimento (4 punti): Tolman fece "
        "esplorare i ratti anche senza ricompensa.",
        "Avresti dovuto spiegare l'apprendimento latente (3 punti): cioè l'apprendimento senza "
        "rinforzo.",
        "Il [Criterio 3] vale 10 punti.",
    ],
)
def test_a_sub_score_written_in_the_pasted_rubric_is_accepted(sentence: str) -> None:
    out = check(sentence, _sub_index())
    assert out.replaced == 0 and out.text == sentence


@pytest.mark.parametrize(
    "sentence",
    [
        "Nel [Criterio 2] hai ottenuto 4 punti.",  # earned: no per-criterion mark exists
        "Hai preso 4 punti.",  # unattributed, but presented as obtained
        "Ti hanno tolto 3 punti sull'esperimento.",
        "Nel [Criterio 2] vale 7 punti.",  # not in the rubric
        "Il [Criterio 2] vale 4 punti.",  # a sub-score is not what the criterion is worth
        "L'esperimento vale 6 punti.",  # unattributed, not in the context
        "Nel [Criterio 2] hai ottenuto quattro punti.",  # spelled out: always replaced
        "Il tuo punteggio è 4.",  # a score word, not a rubric point
        "Il punteggio del [Criterio 2] è di 4 punti.",
        "Il voto è 3.",
    ],
)
def test_a_rubric_number_never_becomes_an_obtained_or_invented_score(
    sentence: str,
) -> None:
    assert check(sentence, _sub_index()).replaced == 1, sentence


def test_a_quiz_mark_is_still_checked_against_the_snapshot() -> None:
    """A number the rubric happens to contain is no licence on an item with a snapshot mark."""
    idx = _sub_index()
    assert check("Nella [Domanda 2] hai 4 punti.", idx).replaced == 1
    assert check("Nella [Domanda 2] il punteggio è 3.", idx).replaced == 1
    assert check("Nella [Domanda 2] hai ottenuto 2,5/5 punti.", idx).replaced == 0


def test_points_in_the_solution_in_context_are_accepted() -> None:
    sol = "Risposta attesa: definizione (2 punti) e un esempio (5 punti)."
    sentence = "La soluzione chiede un esempio (5 punti)."
    assert check(sentence, _sub_index(sol)).replaced == 0
    assert check(sentence, _sub_index()).replaced == 1  # not in context: not accepted
