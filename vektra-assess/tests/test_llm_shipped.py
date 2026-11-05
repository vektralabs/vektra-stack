"""Vektra-shipped strings: complete per class x language, read-only, single source (REQ-098)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from vektra_assess.context import strings as context_strings
from vektra_assess.llm import shipped
from vektra_assess.llm.postcheck import feedback_quote, values
from vektra_assess.llm.postcheck.markers import CLASS_CODES


@pytest.mark.parametrize("language", shipped.LANGUAGES)
@pytest.mark.parametrize("klass", sorted(CLASS_CODES))
def test_every_class_has_a_core_and_an_alternative(language: str, klass: str) -> None:
    s = shipped.get(language)
    text = s.refusal(klass)
    assert text.startswith(s.classes[klass].core) and len(text) > len(
        s.classes[klass].core
    )
    assert "{" not in text  # every placeholder filled


def test_distress_carries_the_configured_support_text() -> None:
    it = shipped.get("it")
    assert it.refusal("W-DISTRESS").endswith(it.default_support_text)
    assert it.refusal("W-DISTRESS", "Sportello di ascolto: 0123").endswith(
        "Sportello di ascolto: 0123"
    )


def test_unsupported_language_falls_back_to_italian() -> None:
    assert shipped.get("fr") is shipped.get("it")


def test_strings_are_read_only() -> None:
    s = shipped.get("it")
    with pytest.raises(TypeError):
        s.classes["R-SOL"] = None  # type: ignore[index]
    with pytest.raises(AttributeError):
        s.br009 = "x"  # type: ignore[misc]


def test_single_source_with_part_a_and_the_postcheck() -> None:
    it = shipped.get("it")
    assert it.br009 == context_strings.BR009_TEMPLATE
    assert it.missing["value_unverified"] == values.REPLACEMENT
    assert it.missing["quote_unverified"] == feedback_quote.MISSING


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda t: t.replace("  R-GHOST:", "  R-GHOSTX:"), "classes must be exactly"),
        (lambda t: t.replace('    alternative: "{support_text}"\n', ""), "alternative"),
        (lambda t: t.replace("unsupported_language:", "x_unsupported:"), "unsupported"),
    ],
)
def test_an_incomplete_file_fails_loudly(tmp_path: Path, edit, message: str) -> None:  # type: ignore[no-untyped-def]
    shutil.copy(shipped.DIR / "it.yaml", tmp_path / "it.yaml")
    p = tmp_path / "it.yaml"
    p.write_text(edit(p.read_text(encoding="utf-8")), encoding="utf-8")
    with pytest.raises(shipped.ShippedStringsError, match=message):
        shipped._load("it", tmp_path)


def test_the_d3_disclosure_is_shipped_verbatim() -> None:
    """REQ-099 (b): the requirements' D3 sentence, byte for byte, for the REQ-077 help text."""
    req = (Path(__file__).resolve().parents[2] / ".s2s" / "requirements.md").read_text(
        encoding="utf-8"
    )
    flat = " ".join(req.split())
    d3 = shipped.get("it").oracle_disclosure
    assert d3 in flat
    assert shipped.get("en").oracle_disclosure
