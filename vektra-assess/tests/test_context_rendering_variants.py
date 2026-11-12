"""Every rendering branch of the model payload and of Part A's overview, on hand-built snapshots.

The renderers are the vertical's (``context.payload``, ``context.part_a``) and decide what the
model and the student are told for each shape of a question, criterion, grade and submission:
a mark the student may not see is never shown as a number, an item that could not be read is
said to be so, a truncated text is never presented as complete. Until 2a-4 these branches were
reached only by the Moodle provider's recorded suites (now in ``adapters/``); here they are
pinned on neutral values.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_context_snapshot import make

from vektra_assess.context.assemble import assemble_context
from vektra_assess.context.part_a import overview
from vektra_assess.context.payload import bundle_to_model_payload
from vektra_assess.context.policy import (
    FrozenPolicy,
    PolicyInputs,
    ReviewOptions,
    freeze,
)
from vektra_assess.context.snapshot import (
    ActivityKind,
    AssignCriterion,
    FileEntry,
    GradeKind,
    MarksHidden,
    QuizItem,
    Snapshot,
    SubmissionItem,
)
from vektra_assess.parser_protocol import FileStatus

#: the review options of a released attempt that show marks and maximums (BR-011): what the
#: student sees is what the renderers may say, so the mark branches need marks to be visible
MARKS_SHOWN = ReviewOptions(
    feedback=True,
    generalfeedback=True,
    rightanswer=False,
    overallfeedback=True,
    marks=2,
)


def _frozen(s: Snapshot, **inputs: Any) -> FrozenPolicy:
    base: dict[str, Any] = {
        "flags": s.header.flags,
        "review": MARKS_SHOWN,
        "review_html_filterable": "unreadable",
        "rubric_display_settings": "unreadable",
        "rubric_descriptors_visible": None,
        "released": True,
    }
    base.update(inputs)
    return freeze(PolicyInputs(**base))


def _quiz(**item: Any) -> Snapshot:
    base = dict(
        label="1",
        qtype="multiple_choice",
        discussable=True,
        state="final_marked",
        status="Corretta",
        mark="1",
        max_mark="1",
    )
    base.update(item)
    return make().model_copy(update={"quiz_items": (QuizItem(**base),)})  # type: ignore[arg-type]


def _assign(**update: Any) -> Snapshot:
    s = make()
    header = s.header.model_copy(
        update={"activity_kind": ActivityKind.ASSIGN, "grade_method": None}
    )
    return s.model_copy(update={"quiz_items": (), "header": header, **update})


def _text(messages: Any) -> str:
    return "\n".join(m.content for m in messages)


def _payload(s: Snapshot, **inputs: Any) -> str:
    return _text(bundle_to_model_payload(assemble_context(s, _frozen(s, **inputs))))


def _overview(s: Snapshot) -> tuple[str, ...]:
    return overview(assemble_context(s, _frozen(s)))


# --- quiz items ---------------------------------------------------------------------------------


def test_a_question_with_no_mark_but_a_visible_maximum_was_not_answered() -> None:
    s = _quiz(mark=None, max_mark="2", state="final_no_mark", status="Non risposta")
    assert "Punteggio massimo: 2 (nessun punteggio registrato)" in _payload(s)


def test_a_hidden_mark_is_never_shown_as_a_number() -> None:
    s = _quiz(
        mark=None, max_mark="2", marks_hidden=MarksHidden.REVIEW_OPTIONS, state=None
    )
    text = _payload(s)
    assert "Punteggio massimo: 2 (punteggio non visibile)" in text
    assert "Punteggio: 2" not in text
    s = _quiz(
        mark=None, max_mark=None, marks_hidden=MarksHidden.REVIEW_OPTIONS, state=None
    )
    assert "Punteggio: non visibile" in _payload(s)


def test_the_texts_of_a_question_are_labelled() -> None:
    s = _quiz(question_text="Quanto fa 2+2?", response_text="4", review_text="Visibile")
    # the review text reaches the model only on the filterable path (ADR-0032)
    assert "Revisione visibile" not in _payload(s)
    text = _payload(s, review_html_filterable="readable")
    assert "Testo della domanda:\nQuanto fa 2+2?" in text
    assert "Risposta dello studente:\n4" in text
    assert "Revisione visibile allo studente:\nVisibile" in text


@pytest.mark.parametrize(
    ("item", "note"),
    [
        (
            {"degraded": True, "response_text": "4"},
            "Nota: testo della domanda non disponibile; risposta dello studente disponibile.",
        ),
        (
            {"degraded": True, "response_text": "4", "truncated": True},
            "risposta dello studente disponibile ma troncata; non trattarla come completa.",
        ),
        ({"degraded": True}, "Nota: testo della domanda e risposta non disponibili."),
        (
            {"truncated": True, "response_text": "4"},
            "Nota: testo della domanda o della risposta troncato; non trattarlo come completo.",
        ),
    ],
)
def test_an_item_read_in_part_says_so(item: dict[str, Any], note: str) -> None:
    assert note in _payload(_quiz(**item))


def test_the_overview_line_follows_the_same_mark_rules() -> None:
    from vektra_assess.context import strings as S

    hidden = _overview(
        _quiz(
            mark=None,
            max_mark=None,
            marks_hidden=MarksHidden.REVIEW_OPTIONS,
            state=None,
        )
    )
    assert hidden[0] == S.QUIZ_ITEM_HIDDEN.format(label="1")
    max_only = _overview(
        _quiz(
            mark=None, max_mark="2", marks_hidden=MarksHidden.REVIEW_OPTIONS, state=None
        )
    )
    assert max_only[0] == S.QUIZ_ITEM_MAX_ONLY.format(label="1", max="2")
    no_mark = _overview(_quiz(mark=None, max_mark="2", status=None))
    assert no_mark[0] == S.QUIZ_ITEM_NO_MARK.format(
        label="1", max="2", state=S.QUIZ_STATE_UNKNOWN
    )
    degraded = _overview(_quiz(degraded=True, response_text="4", truncated=True))
    assert degraded[0].endswith(S.DEGRADED_RESPONSE_ONLY + S.RESPONSE_TRUNCATED)
    assert _overview(_quiz(degraded=True))[0].endswith(S.DEGRADED)
    assert _overview(_quiz(truncated=True))[0].endswith(S.TRUNCATED)
    ungraded = _overview(
        _quiz(mark=None, max_mark="2", marks_hidden=MarksHidden.NOT_GRADED, state=None)
    )
    assert ungraded[0] == S.QUIZ_ITEM_UNGRADED.format(label="1")


# --- assignments --------------------------------------------------------------------------------


def _criterion(**kw: Any) -> AssignCriterion:
    base = dict(
        label="C1", level_index=2, level_count=3, points="5", max_points="10", remark=""
    )
    base.update(kw)
    return AssignCriterion(**base)  # type: ignore[arg-type]


def test_a_criterion_carries_only_the_texts_it_has() -> None:
    text = _payload(
        _assign(
            criteria=(
                _criterion(
                    remark="Buona analisi",
                    description="Analisi del problema",
                    chosen_level_text="Completa",
                ),
            )
        )
    )
    assert "Livello 2 di 3; punti 5/10" in text
    assert "Commento del docente:\nBuona analisi" in text
    # descriptors only when the rubric's display settings are known to show them (OQ-035)
    assert (
        "Descrizione del criterio" not in text and "Descrittore del livello" not in text
    )
    s = _assign(
        criteria=(
            _criterion(
                remark="Buona analisi",
                description="Analisi del problema",
                chosen_level_text="Completa",
            ),
        )
    )
    shown = _payload(
        s, rubric_display_settings="readable", rubric_descriptors_visible=True
    )
    assert "Descrizione del criterio:\nAnalisi del problema" in shown
    assert "Descrittore del livello:\nCompleta" in shown
    bare = _payload(_assign(criteria=(_criterion(),)))
    assert "Commento del docente" not in bare and "Descrittore del livello" not in bare


def test_a_scale_grade_is_its_label() -> None:
    s = _assign()
    overall = s.overall.model_copy(
        update={
            "grade_kind": GradeKind.SCALE,
            "grade": None,
            "grade_max": None,
            "scale_label": "Ottimo",
        }
    )
    assert "Voto: Ottimo (scala)" in _payload(s.model_copy(update={"overall": overall}))


@pytest.mark.parametrize(
    ("submission", "line"),
    [
        (
            SubmissionItem(onlinetext="Il mio testo", truncated=True),
            "Nota: il testo consegnato è troncato.",
        ),
        (
            SubmissionItem(onlinetext=None, unreadable=True),
            "Testo consegnato: non leggibile",
        ),
        (
            SubmissionItem(onlinetext="", truncated=True),
            "Testo consegnato: omesso (troncato)",
        ),
    ],
)
def test_the_submitted_text_is_never_presented_as_complete_when_it_is_not(
    submission: SubmissionItem, line: str
) -> None:
    assert line in _payload(_assign(submission=submission))


def test_a_read_file_is_labelled_and_its_text_follows() -> None:
    f = FileEntry(
        label="F1", filename="relazione.pdf", status=FileStatus.READ, text="Contenuto"
    )
    text = _payload(_assign(files=(f,)))
    assert "File F1 (relazione.pdf):" in text
    assert "Contenuto di F1:\nContenuto" in text
    assert _overview(_assign(files=(f,)))[-1]  # the submission line closes the overview


def test_the_pasted_rubric_and_the_solution_are_labelled_sections() -> None:
    s = _assign(pasted_rubric="Rubrica: chiarezza", solution_text="La soluzione")
    # the solution only with the teacher's toggle sealed into the snapshot
    assert "Soluzione fornita" not in _payload(s)
    flags = s.header.flags.model_copy(update={"show_solution": True})
    s = s.model_copy(update={"header": s.header.model_copy(update={"flags": flags})})
    text = _payload(s)
    assert "## Rubrica fornita dal docente\nRubrica: chiarezza" in text
    assert "## Soluzione fornita dal docente\nLa soluzione" in text
