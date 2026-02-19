"""Unit tests for TemplateRenderer (ARCH-054, ARCH-048)."""

from pathlib import Path

import pytest

from vektra_core.templates import TemplateRenderer


def test_loads_builtin_templates():
    renderer = TemplateRenderer()
    assert renderer.prompt_version != ""
    assert len(renderer.prompt_version) == 8


def test_prompt_version_is_sha256_prefix():
    renderer = TemplateRenderer()
    # Verify it's a hex string of length 8
    int(renderer.prompt_version, 16)  # raises ValueError if not hex


def test_prompt_version_changes_with_content(tmp_path):
    """Different template content → different prompt_version."""
    (tmp_path / "system.j2").write_text("System A")
    (tmp_path / "context.j2").write_text("Context A")
    (tmp_path / "conversation.j2").write_text("Conversation A")

    r1 = TemplateRenderer(tmp_path)

    (tmp_path / "system.j2").write_text("System B")
    r2 = TemplateRenderer(tmp_path)

    assert r1.prompt_version != r2.prompt_version


def test_render_system_default_namespace():
    renderer = TemplateRenderer()
    result = renderer.render_system()
    assert "assistant" in result.lower()


def test_render_system_custom_namespace():
    renderer = TemplateRenderer()
    result = renderer.render_system(namespace="course-ml-2026")
    assert "course-ml-2026" in result


def test_render_context_with_chunks():
    renderer = TemplateRenderer()
    chunks = [
        {"text": "RAG is retrieval-augmented generation.", "score": 0.9},
        {"text": "It combines search with language models.", "score": 0.8},
    ]
    result = renderer.render_context(chunks)
    assert "RAG is retrieval-augmented generation." in result
    assert "It combines search with language models." in result


def test_render_conversation_empty():
    renderer = TemplateRenderer()
    result = renderer.render_conversation([])
    # Empty history → empty or whitespace output
    assert result.strip() == ""


def test_render_conversation_with_history():
    renderer = TemplateRenderer()
    history = [
        {
            "question": "What is RAG?",
            "answer": "RAG stands for retrieval-augmented generation.",
        },
        {"question": "Who invented it?", "answer": "Various researchers."},
    ]
    result = renderer.render_conversation(history)
    assert "What is RAG?" in result
    assert "RAG stands for retrieval-augmented generation." in result
    assert "Who invented it?" in result


def test_render_conversation_none_answer():
    renderer = TemplateRenderer()
    history = [{"question": "Hello?", "answer": None}]
    result = renderer.render_conversation(history)
    assert "Hello?" in result  # should not crash


def test_missing_template_raises():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        # Create only two of the three required templates
        (p / "system.j2").write_text("system")
        (p / "context.j2").write_text("context")
        # conversation.j2 missing
        with pytest.raises(FileNotFoundError, match="conversation"):
            TemplateRenderer(p)
