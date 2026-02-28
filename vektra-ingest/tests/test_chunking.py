"""Unit tests for FixedSizeChunking (ARCH-037, REQ-002)."""

from __future__ import annotations

import pytest

from vektra_shared.types import DocumentChunk, ElementType


def _make_chunks(*texts: str) -> list[DocumentChunk]:
    return [DocumentChunk(text=t, element_type=ElementType.TEXT) for t in texts]


async def _collect(chunker, elements):
    it = await chunker.chunk(_aiter(elements))
    chunks = []
    async for c in it:
        chunks.append(c)
    return chunks


async def _aiter(items):
    for item in items:
        yield item


# ---------------------------------------------------------------------------
# FixedSizeChunking init
# ---------------------------------------------------------------------------


def test_overlap_must_be_less_than_size():
    from vektra_ingest.chunking import FixedSizeChunking

    with pytest.raises(ValueError, match="chunk_overlap"):
        FixedSizeChunking(chunk_size=100, chunk_overlap=100)


# ---------------------------------------------------------------------------
# Chunking behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_input_yields_no_chunks():
    from vektra_ingest.chunking import FixedSizeChunking

    chunker = FixedSizeChunking(chunk_size=100, chunk_overlap=20)
    chunks = await _collect(chunker, [])
    assert chunks == []


@pytest.mark.asyncio
async def test_short_text_yields_single_chunk():
    """Text well below chunk_size → one chunk containing all tokens."""
    from vektra_ingest.chunking import FixedSizeChunking

    chunker = FixedSizeChunking(chunk_size=500, chunk_overlap=50)
    text = "Hello world. " * 10  # ~30 tokens
    chunks = await _collect(chunker, _make_chunks(text))

    assert len(chunks) == 1
    assert chunks[0].metadata["chunk_index"] == 0
    assert chunks[0].metadata["token_count"] <= 500


@pytest.mark.asyncio
async def test_long_text_yields_multiple_chunks():
    """Text exceeding chunk_size yields multiple overlapping chunks."""
    from vektra_ingest.chunking import FixedSizeChunking

    # Build ~300 tokens of text; with chunk_size=100, overlap=20 → 3-4 chunks
    words = ["word"] * 300
    text = " ".join(words)

    chunker = FixedSizeChunking(chunk_size=100, chunk_overlap=20)
    chunks = await _collect(chunker, _make_chunks(text))

    assert len(chunks) >= 2
    for i, c in enumerate(chunks):
        assert c.metadata["chunk_index"] == i
        assert c.metadata["token_count"] <= 100


@pytest.mark.asyncio
async def test_chunk_overlap_shared_tokens():
    """Adjacent chunks share chunk_overlap tokens (overlap > 0)."""
    import tiktoken

    from vektra_ingest.chunking import FixedSizeChunking

    # 150 tokens, chunk_size=100, overlap=50 → step=50 → chunks at [0:100], [50:150]
    words = ["tok"] * 150
    text = " ".join(words)

    chunker = FixedSizeChunking(chunk_size=100, chunk_overlap=50)
    chunks = await _collect(chunker, _make_chunks(text))

    assert len(chunks) == 2

    enc = tiktoken.get_encoding("cl100k_base")
    tokens_0 = enc.encode(chunks[0].text)
    tokens_1 = enc.encode(chunks[1].text)

    # The second chunk should start with tokens that appear in the first chunk
    # (overlap = last 50 tokens of chunk 0 = first 50 tokens of chunk 1)
    assert tokens_0[-50:] == tokens_1[:50]


@pytest.mark.asyncio
async def test_multiple_elements_concatenated():
    """Multiple DocumentChunk elements are concatenated before splitting."""
    from vektra_ingest.chunking import FixedSizeChunking

    # Two short elements → total ~20 tokens; should produce a single chunk
    elements = _make_chunks("First paragraph text.", "Second paragraph text.")
    chunker = FixedSizeChunking(chunk_size=500, chunk_overlap=50)
    chunks = await _collect(chunker, elements)

    assert len(chunks) == 1
    # Both texts should appear in the single chunk
    assert "First" in chunks[0].text
    assert "Second" in chunks[0].text


@pytest.mark.asyncio
async def test_chunk_metadata_contains_required_fields():
    """Each chunk has chunk_index and token_count in metadata."""
    from vektra_ingest.chunking import FixedSizeChunking

    text = "word " * 200
    chunker = FixedSizeChunking(chunk_size=100, chunk_overlap=10)
    chunks = await _collect(chunker, _make_chunks(text))

    for chunk in chunks:
        assert "chunk_index" in chunk.metadata
        assert "token_count" in chunk.metadata
        assert isinstance(chunk.metadata["token_count"], int)
        assert chunk.metadata["token_count"] > 0
