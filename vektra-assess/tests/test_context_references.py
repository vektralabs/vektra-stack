"""Server-side item references (REQ-095 AC3/AC5)."""

from __future__ import annotations

import pytest
from test_context_bundle import _frozen
from test_context_snapshot import make

from vektra_assess.context.assemble import assemble_context
from vektra_assess.context.bundle import ContextBundle
from vektra_assess.context.references import (
    MAX_CANDIDATES,
    Ambiguous,
    NotFound,
    Rejected,
    Resolved,
    resolve_reference,
)
from vektra_assess.context.snapshot import QuizItem


def _quiz() -> ContextBundle:
    items = tuple(
        QuizItem(
            label=str(n),
            qtype="multichoice",
            discussable=True,
            state="gradedright",
            status="Corretta",
            mark="1",
            max_mark="1",
            question_text=t,
        )
        for n, t in enumerate(
            [
                "Calcola la derivata della funzione",
                "Inverti la matrice A",
                "Determinante della matrice B",
                "Integrale definito",
            ],
            start=1,
        )
    )
    s = make().model_copy(update={"quiz_items": items})
    return assemble_context(s, _frozen(s))


@pytest.mark.parametrize(
    ("ref", "label"),
    [
        ("Domanda 2", "Domanda 2"), ("domanda 2", "Domanda 2"), ("D3", "Domanda 3"), ("q1", "Domanda 1"),
        ("4", "Domanda 4"), ("la terza", "Domanda 3"), ("la terza domanda", "Domanda 3"),
        ("l'ultima", "Domanda 4"), ("quella sulla derivata", "Domanda 1"),
        ("Valutazione complessiva", "Valutazione complessiva"), ("il voto", "Valutazione complessiva"),
    ],
)  # fmt: skip
def test_resolved(ref: str, label: str) -> None:
    assert resolve_reference(_quiz(), ref) == Resolved(label)


def test_ambiguous_offers_at_most_three_and_never_guesses() -> None:
    res = resolve_reference(_quiz(), "quella sulle matrici")
    assert isinstance(res, Ambiguous) and set(res.candidates) == {
        "Domanda 2",
        "Domanda 3",
    }
    assert len(res.candidates) <= MAX_CANDIDATES and "Intendi" in res.text


def test_not_found_gives_the_fixed_text_with_the_valid_list() -> None:
    res = resolve_reference(_quiz(), "Domanda 9")
    assert isinstance(res, NotFound)
    assert res.text == (
        "Non trovo Domanda 9 in questo tentativo; ecco l'elenco: Domanda 1, Domanda 2, Domanda 3, "
        "Domanda 4, Valutazione complessiva"
    )


@pytest.mark.parametrize(
    "ref",
    [
        "slot=3",
        "cmid: 44",
        "attemptid 39",
        "id=17",
        "questionid=5",
        "1234",
        "#57",
        "userid",
        '{"slot": 2}',
    ],
)
def test_moodle_id_shapes_are_rejected(ref: str) -> None:
    res = resolve_reference(_quiz(), ref)
    assert res == Rejected("moodle_id") and res.audit_data() == {
        "reason_code": "moodle_id"
    }


def test_picker_with_an_out_of_snapshot_label_is_rejected() -> None:
    assert resolve_reference(_quiz(), "Domanda 9", source="picker") == Rejected(
        "out_of_snapshot"
    )
    assert resolve_reference(_quiz(), "Domanda 2", source="picker") == Resolved(
        "Domanda 2"
    )


def test_assignment_labels() -> None:
    from test_context_capture import (
        _env,  # noqa: F401  (recorded fixtures are not needed here)
    )

    from vektra_assess.context.snapshot import ActivityKind, AssignCriterion

    s = make()
    s = s.model_copy(update={
        "quiz_items": (),
        "criteria": (AssignCriterion(label="C1", level_index=2, level_count=3, points="5", max_points="10", remark=""),
                     AssignCriterion(label="C2", level_index=3, level_count=3, points="10", max_points="10", remark="")),
        "header": s.header.model_copy(update={"activity_kind": ActivityKind.ASSIGN, "grade_method": None}),
    })  # fmt: skip
    b = assemble_context(s, _frozen(s))
    assert resolve_reference(b, "Criterio 2") == Resolved("Criterio 2")
    assert resolve_reference(b, "c1") == Resolved("Criterio 1")
    assert resolve_reference(b, "il secondo criterio") == Resolved("Criterio 2")
    assert resolve_reference(b, "consegna") == Resolved("Consegna")
    assert resolve_reference(b, "Domanda 1") == NotFound(
        "Non trovo Domanda 1 in questo tentativo; ecco l'elenco: Criterio 1, Criterio 2, Valutazione complessiva, Consegna",
        ("Criterio 1", "Criterio 2", "Valutazione complessiva", "Consegna"),
    )


def test_empty_is_rejected() -> None:
    assert resolve_reference(_quiz(), "  ") == Rejected("empty")
