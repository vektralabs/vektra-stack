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
