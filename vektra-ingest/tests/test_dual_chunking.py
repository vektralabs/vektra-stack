"""Unit tests for DualStrategyChunking (ARCH-037, REQ-054, T7)."""

from __future__ import annotations

import pytest

from vektra_shared.types import DocumentChunk, ElementType


async def _aiter(items):
    for item in items:
        yield item


async def _collect(chunker, elements):
    it = await chunker.chunk(_aiter(elements))
    chunks = []
    async for c in it:
        chunks.append(c)
    return chunks


def _text_element(text: str, **kwargs) -> DocumentChunk:
    return DocumentChunk(text=text, element_type=ElementType.TEXT, **kwargs)


def _table_element(text: str, content_format: str = "html", **kwargs) -> DocumentChunk:
    return DocumentChunk(
        text=text, element_type=ElementType.TABLE, content_format=content_format, **kwargs
    )


# ---------------------------------------------------------------------------
# Init validation
# ---------------------------------------------------------------------------


def test_overlap_must_be_less_than_size():
    from vektra_ingest.chunking import DualStrategyChunking

    with pytest.raises(ValueError, match="text_chunk_overlap"):
        DualStrategyChunking(text_chunk_size=100, text_chunk_overlap=100)


def test_parent_size_must_be_gte_child_size():
    from vektra_ingest.chunking import DualStrategyChunking

    with pytest.raises(ValueError, match="parent_chunk_size"):
        DualStrategyChunking(text_chunk_size=100, text_chunk_overlap=20, parent_chunk_size=50)


# ---------------------------------------------------------------------------
# Text elements: split with overlap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_elements_split_with_overlap():
    """Text elements are accumulated and split like FixedSizeChunking."""
    from vektra_ingest.chunking import DualStrategyChunking

    # ~200 tokens, chunk_size=100 -> should get 1 parent + multiple children
    text = "word " * 200
    elements = [_text_element(text)]

    chunker = DualStrategyChunking(
        text_chunk_size=100, text_chunk_overlap=20, parent_chunk_size=300
    )
    chunks = await _collect(chunker, elements)

    # Should have at least one parent and at least one child
    parents = [c for c in chunks if c.parent_id is None and c.element_type == ElementType.TEXT]
    children = [c for c in chunks if c.parent_id is not None]

    assert len(parents) >= 1
    assert len(children) >= 1

    for child in children:
        assert child.parent_id is not None
        assert child.metadata.get("chunk_level") == "child"

    for parent in parents:
        assert parent.metadata.get("chunk_level") == "parent"


@pytest.mark.asyncio
async def test_empty_input_yields_no_chunks():
    from vektra_ingest.chunking import DualStrategyChunking

    chunker = DualStrategyChunking(
        text_chunk_size=100, text_chunk_overlap=20, parent_chunk_size=300
    )
    chunks = await _collect(chunker, [])
    assert chunks == []


@pytest.mark.asyncio
async def test_short_text_yields_parent_and_one_child():
    """Text shorter than chunk_size yields one parent + one child."""
    from vektra_ingest.chunking import DualStrategyChunking

    text = "Hello world."  # ~3 tokens
    elements = [_text_element(text)]

    chunker = DualStrategyChunking(
        text_chunk_size=500, text_chunk_overlap=50, parent_chunk_size=1000
    )
    chunks = await _collect(chunker, elements)

    parents = [c for c in chunks if c.metadata.get("chunk_level") == "parent"]
    children = [c for c in chunks if c.metadata.get("chunk_level") == "child"]

    assert len(parents) == 1
    assert len(children) == 1
    assert "Hello" in parents[0].text
    assert "Hello" in children[0].text
    assert children[0].parent_id is not None


# ---------------------------------------------------------------------------
# Table elements: never split
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_table_elements_never_split():
    """Table elements are emitted as-is, never split."""
    from vektra_ingest.chunking import DualStrategyChunking

    long_table = "<table>" + "<tr><td>cell</td></tr>" * 100 + "</table>"
    elements = [_table_element(long_table)]

    chunker = DualStrategyChunking(
        text_chunk_size=50, text_chunk_overlap=10, parent_chunk_size=100
    )
    chunks = await _collect(chunker, elements)

    assert len(chunks) == 1
    assert chunks[0].element_type == ElementType.TABLE
    assert chunks[0].text == long_table
    assert chunks[0].parent_id is None  # tables are standalone


@pytest.mark.asyncio
async def test_table_gets_html_format():
    """Table elements have content_format='html'."""
    from vektra_ingest.chunking import DualStrategyChunking

    # A table with default text format should be converted to html
    elements = [
        DocumentChunk(
            text="plain table", element_type=ElementType.TABLE, content_format="text"
        )
    ]

    chunker = DualStrategyChunking(
        text_chunk_size=100, text_chunk_overlap=20, parent_chunk_size=300
    )
    chunks = await _collect(chunker, elements)

    assert len(chunks) == 1
    assert chunks[0].content_format == "html"


# ---------------------------------------------------------------------------
# Mixed text + table input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mixed_text_table_input():
    """Mixed text and table elements produce correct output."""
    from vektra_ingest.chunking import DualStrategyChunking

    elements = [
        _text_element("Introduction paragraph. " * 10),
        _table_element("<table><tr><td>data</td></tr></table>"),
        _text_element("Conclusion paragraph. " * 10),
    ]

    chunker = DualStrategyChunking(
        text_chunk_size=500, text_chunk_overlap=50, parent_chunk_size=1000
    )
    chunks = await _collect(chunker, elements)

    # Should have: text parent + text child(ren), table, text parent + text child(ren)
    tables = [c for c in chunks if c.element_type == ElementType.TABLE]
    texts = [c for c in chunks if c.element_type == ElementType.TEXT]

    assert len(tables) == 1
    assert len(texts) >= 2  # at least one parent + one child per text segment


# ---------------------------------------------------------------------------
# Parent-child hierarchy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parent_chunks_emitted_at_correct_intervals():
    """Multiple parent chunks are emitted when text exceeds parent_chunk_size."""
    from vektra_ingest.chunking import DualStrategyChunking

    # ~600 tokens, parent_chunk_size=200 -> ~3 parent chunks
    text = "word " * 600
    elements = [_text_element(text)]

    chunker = DualStrategyChunking(
        text_chunk_size=100, text_chunk_overlap=20, parent_chunk_size=200
    )
    chunks = await _collect(chunker, elements)

    parents = [c for c in chunks if c.metadata.get("chunk_level") == "parent"]
    children = [c for c in chunks if c.metadata.get("chunk_level") == "child"]

    assert len(parents) >= 3
    assert len(children) >= len(parents)  # each parent has at least 1 child


@pytest.mark.asyncio
async def test_child_chunks_have_parent_id():
    """All child chunks reference a valid parent_id."""
    from vektra_ingest.chunking import DualStrategyChunking

    text = "word " * 300
    elements = [_text_element(text)]

    chunker = DualStrategyChunking(
        text_chunk_size=100, text_chunk_overlap=20, parent_chunk_size=200
    )
    chunks = await _collect(chunker, elements)

    children = [c for c in chunks if c.metadata.get("chunk_level") == "child"]

    for child in children:
        assert child.parent_id is not None
        assert len(child.parent_id) == 36  # UUID string length


@pytest.mark.asyncio
async def test_children_share_parent_id_within_group():
    """Children from the same parent section share the same parent_id."""
    from vektra_ingest.chunking import DualStrategyChunking

    # Small enough to fit in one parent, large enough for multiple children
    text = "word " * 300
    elements = [_text_element(text)]

    chunker = DualStrategyChunking(
        text_chunk_size=100, text_chunk_overlap=20, parent_chunk_size=500
    )
    chunks = await _collect(chunker, elements)

    children = [c for c in chunks if c.metadata.get("chunk_level") == "child"]
    parents = [c for c in chunks if c.metadata.get("chunk_level") == "parent"]

    # With parent_chunk_size=500 and ~300 tokens, should be 1 parent
    assert len(parents) == 1
    # All children should share the same parent_id
    parent_ids = {c.parent_id for c in children}
    assert len(parent_ids) == 1


# ---------------------------------------------------------------------------
# PAGE_BREAK and IMAGE are skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_page_break_and_image_skipped():
    """PAGE_BREAK and IMAGE elements are ignored."""
    from vektra_ingest.chunking import DualStrategyChunking

    elements = [
        _text_element("Before"),
        DocumentChunk(text="---", element_type=ElementType.PAGE_BREAK),
        DocumentChunk(text="[img]", element_type=ElementType.IMAGE),
        _text_element("After"),
    ]

    chunker = DualStrategyChunking(
        text_chunk_size=500, text_chunk_overlap=50, parent_chunk_size=1000
    )
    chunks = await _collect(chunker, elements)

    # Only text chunks, no PAGE_BREAK or IMAGE
    for c in chunks:
        assert c.element_type in (ElementType.TEXT, ElementType.TABLE)


@pytest.mark.asyncio
async def test_chunk_metadata_has_required_fields():
    """Each chunk has chunk_index and token_count."""
    from vektra_ingest.chunking import DualStrategyChunking

    text = "word " * 100
    elements = [_text_element(text)]

    chunker = DualStrategyChunking(
        text_chunk_size=50, text_chunk_overlap=10, parent_chunk_size=100
    )
    chunks = await _collect(chunker, elements)

    for chunk in chunks:
        assert "chunk_index" in chunk.metadata
        if chunk.element_type == ElementType.TEXT:
            assert "token_count" in chunk.metadata
            assert chunk.metadata["token_count"] > 0
