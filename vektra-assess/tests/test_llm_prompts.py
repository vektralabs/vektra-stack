"""PromptRenderer and prompt_version (plan assess-llm-runner, task A)."""

from __future__ import annotations

import shutil
from pathlib import Path

import jinja2
import pytest
from test_context_bundle import _frozen
from test_context_snapshot import make

from vektra_assess.context.assemble import assemble_context
from vektra_assess.context.payload import bundle_to_model_payload
from vektra_assess.llm.prompts import (
    STRINGS_DIR,
    TEMPLATE_NAMES,
    TEMPLATES_DIR,
    PromptRenderer,
    compute_prompt_version,
)
from vektra_assess.llm.types import PriorTurn


def _bundle():  # type: ignore[no-untyped-def]
    s = make()
    return assemble_context(s, _frozen(s))


def _copy(tmp_path: Path) -> tuple[Path, Path]:
    t, s = tmp_path / "templates", tmp_path / "strings"
    shutil.copytree(TEMPLATES_DIR, t)
    s.mkdir()
    (s / "it.yaml").write_text("a: b\n", encoding="utf-8")
    return t, s


def test_version_is_eight_hex_and_stable() -> None:
    v = PromptRenderer("lex1").prompt_version
    assert len(v) == 8 and int(v, 16) >= 0
    assert PromptRenderer("lex1").prompt_version == v


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_changing_any_template_changes_the_version(tmp_path: Path, name: str) -> None:
    t, s = _copy(tmp_path)
    before = compute_prompt_version(t, s, "lex1")
    path = t / f"{name}.j2"
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    assert compute_prompt_version(t, s, "lex1") != before


def test_changing_a_string_file_or_the_lexicon_changes_the_version(
    tmp_path: Path,
) -> None:
    t, s = _copy(tmp_path)
    before = compute_prompt_version(t, s, "lex1")
    assert compute_prompt_version(t, s, "lex2") != before
    (s / "it.yaml").write_text("a: c\n", encoding="utf-8")
    edited = compute_prompt_version(t, s, "lex1")
    assert edited != before
    (s / "en.yaml").write_text("a: c\n", encoding="utf-8")  # a new file counts too
    assert compute_prompt_version(t, s, "lex1") != edited


def test_the_payload_version_changes_the_prompt_version(tmp_path: Path) -> None:
    """The model reads the payload's vocabulary too (ADR-0033 D2 changed an item type's
    spelling): a bump of ``PAYLOAD_VERSION`` needs a new NFR-023 activation."""
    from vektra_assess.context.payload import PAYLOAD_VERSION

    t, s = _copy(tmp_path)
    assert PAYLOAD_VERSION == 2
    current = compute_prompt_version(t, s, "lex1")
    assert compute_prompt_version(t, s, "lex1", PAYLOAD_VERSION) == current
    assert compute_prompt_version(t, s, "lex1", PAYLOAD_VERSION - 1) != current


def test_the_builtin_version_covers_the_shipped_strings_dir() -> None:
    assert PromptRenderer("x").prompt_version == compute_prompt_version(
        TEMPLATES_DIR, STRINGS_DIR, "x"
    )


def test_a_missing_template_is_an_error(tmp_path: Path) -> None:
    t, s = _copy(tmp_path)
    (t / "part_b.j2").unlink()
    with pytest.raises(FileNotFoundError):
        compute_prompt_version(t, s, "lex1")


def test_turn_messages_wrap_the_single_serializer() -> None:
    b = _bundle()
    msgs = PromptRenderer("x").turn_messages(
        b,
        (PriorTurn("student", "prima"), PriorTurn("assistant", "risposta")),
        "e la domanda 2?",
        "it",
    )
    assert [m.role for m in msgs] == ["system", "user", "user", "assistant", "user"]
    assert bundle_to_model_payload(b)[0].content in msgs[1].content
    assert "[Domanda 1]" in msgs[0].content and "italiano" in msgs[0].content
    assert msgs[2].content.endswith("prima") and msgs[3].content == "risposta"
    assert msgs[4].content.endswith("e la domanda 2?")


def test_english_reply_language_is_named() -> None:
    msgs = PromptRenderer("x").part_b_messages(_bundle(), "en")
    assert "inglese" in msgs[0].content and "inglese" in msgs[2].content
    assert "120 parole" in msgs[2].content and "3 elementi" in msgs[2].content


def test_student_text_is_not_template_code() -> None:
    """The sandbox renders values; a student's Jinja syntax stays literal text."""
    msgs = PromptRenderer("x").turn_messages(
        _bundle(), (), "{{ ''.__class__ }} {% raw %}", "it"
    )
    assert "{{ ''.__class__ }} {% raw %}" in msgs[-1].content


def test_undefined_variables_fail_loudly(tmp_path: Path) -> None:
    t, s = _copy(tmp_path)
    (t / "turn.j2").write_text("{{ nope }}", encoding="utf-8")
    with pytest.raises(jinja2.UndefinedError):
        PromptRenderer("x", t, s).turn_messages(_bundle(), (), "x", "it")
