"""Machine markers: parsed, validated against the bundle, always stripped (post-check stage 1)."""

from __future__ import annotations

import pytest

from vektra_assess.llm.postcheck.markers import (
    CLASS_CODES,
    CLASS_UNKNOWN,
    CONTEST_LABEL_UNKNOWN,
    CONTEST_MALFORMED,
    extract,
)
from vektra_assess.llm.types import ContestEntry

LABELS = ("Domanda 1", "Domanda 2", "Valutazione complessiva")


@pytest.mark.parametrize("code", sorted(CLASS_CODES))
def test_a_known_class_is_read_and_stripped(code: str) -> None:
    m = extract(f"[[CLASS {code}]] Capisco la richiesta.", LABELS)
    assert m.behaviour_class == code and m.text == "Capisco la richiesta."
    assert m.dropped == ()


def test_an_unknown_class_is_dropped_and_reported() -> None:
    m = extract("[[CLASS R-WHATEVER]] Testo.", LABELS)
    assert m.behaviour_class is None and m.text == "Testo."
    assert m.dropped == (CLASS_UNKNOWN,)


def test_a_contest_marker_with_a_bundle_label_is_kept() -> None:
    m = extract(
        'Lo registro. [[PUNTO_CONTESTATO label="Domanda 2" status="confirmed"]]', LABELS
    )
    assert m.contest == (ContestEntry("Domanda 2", "confirmed"),)
    assert m.text == "Lo registro."


@pytest.mark.parametrize(
    ("marker", "code"),
    [
        (
            '[[PUNTO_CONTESTATO label="Domanda 9" status="proposed"]]',
            CONTEST_LABEL_UNKNOWN,
        ),
        (
            '[[PUNTO_CONTESTATO label="attemptid 77" status="proposed"]]',
            CONTEST_LABEL_UNKNOWN,
        ),
        ('[[PUNTO_CONTESTATO label="Domanda 1" status="accepted"]]', CONTEST_MALFORMED),
        ("[[PUNTO_CONTESTATO Domanda 1]]", CONTEST_MALFORMED),
    ],
)
def test_an_invalid_contest_marker_is_dropped_with_a_content_free_code(
    marker: str, code: str
) -> None:
    m = extract(f"Ok. {marker}", LABELS)
    assert m.contest == () and m.dropped == (code,) and m.text == "Ok."
    assert "Domanda 9" not in m.dropped and "attemptid" not in str(m)


def test_a_matrix_is_not_a_marker() -> None:
    m = extract("La matrice [[1, 2], [3, 4]] è invertibile.", LABELS)
    assert m.text == "La matrice [[1, 2], [3, 4]] è invertibile." and m.dropped == ()


def test_markers_never_survive_in_the_text() -> None:
    m = extract(
        '[[CLASS R-SOL]] a [[CLASS R-GRADE]] b [[PUNTO_CONTESTATO label="x" status="y"]]',
        LABELS,
    )
    assert "[[" not in m.text and m.behaviour_class == "R-SOL"
