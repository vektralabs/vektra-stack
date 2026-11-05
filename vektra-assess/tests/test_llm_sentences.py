"""Sentence segmentation for check-then-release (plan assess-llm-runner, task B)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vektra_assess.llm.postcheck.sentences import split

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "llm" / "sentences.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("case", CASES, ids=[c["text"][:30] for c in CASES])
def test_fixture_splits(case: dict[str, object]) -> None:
    out, rest = split(str(case["text"]), final=True)
    assert out == case["expect"] and rest == ""


@pytest.mark.parametrize("case", CASES, ids=[c["text"][:30] for c in CASES])
def test_streaming_in_any_chunking_gives_the_same_sentences(
    case: dict[str, object],
) -> None:
    """Fed one character at a time, the incremental split agrees with the one-shot split."""
    text = str(case["text"])
    buf, got = "", []
    for ch in text:
        buf += ch
        done, buf = split(buf)
        got += done
    done, _ = split(buf, final=True)
    assert got + done == case["expect"]


def test_a_trailing_point_is_held_until_the_next_chunk() -> None:
    assert split("Vale 3.") == ([], "Vale 3.")
    assert split("Vale 3.5 punti. Ok") == (["Vale 3.5 punti."], " Ok")


def test_an_open_quote_holds_everything_until_it_closes() -> None:
    out, rest = split('Commento del docente: "Prima frase. Seconda')
    assert out == [] and rest.startswith("Commento")


def test_an_open_tag_holds_the_point_inside_it() -> None:
    assert split("Vedi [Consegna p.3. ")[0] == []


def test_a_half_open_interval_does_not_freeze_the_stream() -> None:
    text = "La funzione è definita su [0, 1). Poi cresce. Hai torto. Fine."
    out, _ = split(text, final=True)
    assert out == [
        "La funzione è definita su [0, 1).",
        "Poi cresce.",
        "Hai torto.",
        "Fine.",
    ]


def test_an_open_bracket_never_spans_a_line() -> None:
    out, rest = split("Intervallo [0, 1)\nSeconda riga. Terza")
    assert out == ["Intervallo [0, 1)", "Seconda riga."] and rest.strip() == "Terza"


def test_a_stray_quote_is_released_after_the_hold_limit() -> None:
    from vektra_assess.llm.postcheck.sentences import MAX_HOLD

    text = (
        'Il simbolo " indica pollici. ' + "Frase lunga. " * (MAX_HOLD // 10) + "Ultima"
    )
    out, _ = split(text)
    assert len(out) > 5 and out[0] == 'Il simbolo " indica pollici.'


def test_an_unclosed_quote_at_the_end_still_splits() -> None:
    out, _ = split('Il docente scrive "manca. Poi niente. Fine.', final=True)
    assert out == ['Il docente scrive "manca.', "Poi niente.", "Fine."]
