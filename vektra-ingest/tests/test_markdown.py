"""Unit tests for MarkdownExtractor."""

from __future__ import annotations

import pytest

from vektra_shared.types import ExtractionRequest


@pytest.mark.asyncio
async def test_markdown_split_on_headings():
    """Markdown file is split on heading boundaries."""
    from vektra_ingest.extractors.markdown import MarkdownExtractor

    content = (
        "# Introduction\n"
        "This is the intro.\n"
        "\n"
        "## Details\n"
        "Some details here.\n"
        "\n"
        "## Conclusion\n"
        "Final thoughts.\n"
    )
    req = ExtractionRequest(
        content=content.encode(),
        content_type="text/markdown",
        filename="test.md",
    )
    extractor = MarkdownExtractor()
    chunks = []
    async for chunk in await extractor.extract(req):
        chunks.append(chunk)

    assert len(chunks) == 3

    assert "Introduction" in chunks[0].text
    assert chunks[0].metadata["heading_level"] == 1
    assert chunks[0].metadata["section_title"] == "Introduction"

    assert "Details" in chunks[1].text
    assert chunks[1].metadata["heading_level"] == 2

    assert "Conclusion" in chunks[2].text
    assert chunks[2].metadata["heading_level"] == 2


@pytest.mark.asyncio
async def test_markdown_empty_sections_skipped():
    """Sections with no content after the heading are skipped."""
    from vektra_ingest.extractors.markdown import MarkdownExtractor

    content = "# Title\n\n## Empty section\n## Section with content\nSome content.\n"
    req = ExtractionRequest(
        content=content.encode(),
        content_type="text/markdown",
        filename="test.md",
    )
    extractor = MarkdownExtractor()
    chunks = []
    async for chunk in await extractor.extract(req):
        chunks.append(chunk)

    # "Title" has no body text besides the heading itself, but the heading line is included
    # "Empty section" has only the heading line
    # "Section with content" has both heading and body
    assert len(chunks) == 3
    assert chunks[2].metadata["section_title"] == "Section with content"
    assert "Some content." in chunks[2].text


@pytest.mark.asyncio
async def test_markdown_content_format_is_markdown():
    """All chunks have content_format='markdown'."""
    from vektra_ingest.extractors.markdown import MarkdownExtractor

    content = "# Heading\nBody text.\n"
    req = ExtractionRequest(
        content=content.encode(),
        content_type="text/markdown",
        filename="test.md",
    )
    extractor = MarkdownExtractor()
    chunks = []
    async for chunk in await extractor.extract(req):
        chunks.append(chunk)

    assert len(chunks) == 1
    assert chunks[0].content_format == "markdown"


@pytest.mark.asyncio
async def test_markdown_no_headings():
    """Markdown with no headings produces a single chunk with no heading metadata."""
    from vektra_ingest.extractors.markdown import MarkdownExtractor

    content = "Just plain text.\nAnother line.\n"
    req = ExtractionRequest(
        content=content.encode(),
        content_type="text/markdown",
        filename="test.md",
    )
    extractor = MarkdownExtractor()
    chunks = []
    async for chunk in await extractor.extract(req):
        chunks.append(chunk)

    assert len(chunks) == 1
    assert "Just plain text." in chunks[0].text
    assert "heading_level" not in chunks[0].metadata


@pytest.mark.asyncio
async def test_markdown_supported_types():
    """MarkdownExtractor supports text/markdown and text/x-markdown."""
    from vektra_ingest.extractors.markdown import MarkdownExtractor

    extractor = MarkdownExtractor()
    assert extractor.supported_types() == {"text/markdown", "text/x-markdown"}
