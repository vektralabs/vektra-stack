"""Unit tests for safeguard factory and PresidioPIISafeguard."""

from unittest.mock import patch
from uuid import uuid4

import pytest

from vektra_core.safeguards import create_safeguard
from vektra_core.safeguards.presidio import PresidioPIISafeguard
from vektra_shared.safeguards import PassthroughSafeguard
from vektra_shared.types import SafeguardContext, SearchResult

_has_spacy_model = False
try:
    import spacy.util

    _has_spacy_model = spacy.util.is_package("en_core_web_lg") or spacy.util.is_package(
        "en_core_web_sm"
    )
except ImportError:
    pass

requires_presidio = pytest.mark.skipif(
    not _has_spacy_model, reason="No spaCy model available for Presidio"
)

# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


def test_create_passthrough():
    sg = create_safeguard("passthrough")
    assert isinstance(sg, PassthroughSafeguard)


def test_create_presidio():
    sg = create_safeguard("presidio")
    assert isinstance(sg, PresidioPIISafeguard)


def test_create_unknown_falls_back():
    sg = create_safeguard("nonexistent")
    assert isinstance(sg, PassthroughSafeguard)


def test_create_presidio_falls_back_on_import_error():
    with patch(
        "vektra_core.safeguards.presidio.PresidioPIISafeguard",
        side_effect=ImportError("no presidio"),
    ):
        # Force reimport by clearing the cached import
        sg = create_safeguard("presidio")
        assert isinstance(sg, PassthroughSafeguard)


# ---------------------------------------------------------------------------
# PresidioPIISafeguard unit tests
# ---------------------------------------------------------------------------


def _make_result(text: str, chunk_id: str | None = None) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id or str(uuid4()),
        score=0.8,
        text_snippet=text,
        document_id=uuid4(),
    )


@pytest.fixture
def safeguard():
    return PresidioPIISafeguard(pii_chunk_threshold=2)


@pytest.fixture
def ctx():
    return SafeguardContext()


async def test_pre_query_passthrough(safeguard, ctx):
    result = await safeguard.pre_query("test query", ctx)
    assert result.allowed is True


async def test_post_retrieval_no_pii(safeguard, ctx):
    """Chunks without PII are not filtered."""
    results = [
        _make_result("The quick brown fox jumps over the lazy dog"),
        _make_result("Python is a programming language"),
    ]
    result = await safeguard.post_retrieval("test", results, ctx)
    assert result.allowed is True
    assert result.filtered_ids is None


@requires_presidio
async def test_post_retrieval_filters_high_pii_chunks(safeguard, ctx):
    """Chunks with PII count >= threshold are filtered."""
    clean = _make_result("The weather is sunny today")
    pii_heavy = _make_result(
        "John Smith's email is john@example.com and phone is 555-123-4567"
    )
    results = [clean, pii_heavy]
    result = await safeguard.post_retrieval("test", results, ctx)

    assert result.allowed is True
    if result.filtered_ids:
        assert pii_heavy.chunk_id in result.filtered_ids
        assert clean.chunk_id not in result.filtered_ids


@requires_presidio
async def test_pre_response_anonymizes_pii(safeguard, ctx):
    """Pre-response anonymizes PII entities in the answer."""
    text = "John Smith lives at 123 Main St and his email is john@example.com"
    result = await safeguard.pre_response(text, ctx)

    assert result.allowed is True
    assert result.modified_content is not None
    # Original name should be replaced
    assert "John Smith" not in result.modified_content
    # Annotations should record entity types
    assert "pii_entity_types" in result.annotations
    assert result.annotations["pii_entity_count"] > 0


@requires_presidio
async def test_pre_response_no_pii(safeguard, ctx):
    """Pre-response returns unmodified when no PII found."""
    text = "The quick brown fox jumps over the lazy dog"
    result = await safeguard.pre_response(text, ctx)

    assert result.allowed is True
    assert result.modified_content is None


async def test_pre_response_empty_text(safeguard, ctx):
    """Pre-response handles empty text gracefully."""
    result = await safeguard.pre_response("", ctx)
    assert result.allowed is True


async def test_engines_lazy_loaded():
    """Engines are not loaded until first method call."""
    sg = PresidioPIISafeguard()
    assert sg._initialized is False
    assert sg._analyzer is None

    await sg.pre_query("test", SafeguardContext())
    # pre_query doesn't trigger engine loading
    assert sg._initialized is False

    await sg.post_retrieval("test", [], SafeguardContext())
    # post_retrieval does trigger engine loading
    assert sg._initialized is True


async def test_graceful_fallback_on_engine_failure():
    """When engine fails to load, methods return pass-through results."""
    sg = PresidioPIISafeguard(spacy_model="nonexistent_model_xyz")
    ctx = SafeguardContext()

    result = await sg.post_retrieval("test", [_make_result("text")], ctx)
    assert result.allowed is True

    result = await sg.pre_response("John Smith is here", ctx)
    assert result.allowed is True
