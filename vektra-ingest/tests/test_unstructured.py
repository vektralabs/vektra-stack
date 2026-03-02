"""Unit tests for UnstructuredExtractor (T4).

All tests mock unstructured.partition.pdf.partition_pdf since the
library is large and not installed in CI by default.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from vektra_shared.types import ElementType, ExtractionRequest


# ---------------------------------------------------------------------------
# Module-level mock for unstructured (not installed in test env)
# ---------------------------------------------------------------------------


def _install_unstructured_mock():
    """Install a minimal mock for 'unstructured' in sys.modules."""
    mod_unstructured = ModuleType("unstructured")
    mod_partition = ModuleType("unstructured.partition")
    mod_partition_pdf = ModuleType("unstructured.partition.pdf")
    mod_partition_pdf.partition_pdf = MagicMock(return_value=[])
    mod_unstructured.partition = mod_partition
    mod_partition.pdf = mod_partition_pdf

    sys.modules["unstructured"] = mod_unstructured
    sys.modules["unstructured.partition"] = mod_partition
    sys.modules["unstructured.partition.pdf"] = mod_partition_pdf

    return mod_partition_pdf


_mock_pdf_module = _install_unstructured_mock()


def _make_element(
    text: str,
    category: str = "NarrativeText",
    page_number: int | None = 1,
    coordinates: list[tuple[float, float]] | None = None,
    text_as_html: str | None = None,
):
    """Create a mock Unstructured element."""
    el = MagicMock()
    el.__str__ = lambda self: text
    el.category = category

    meta = MagicMock()
    meta.page_number = page_number

    if text_as_html is not None:
        meta.text_as_html = text_as_html
    else:
        meta.text_as_html = None

    if coordinates is not None:
        coords = MagicMock()
        coords.points = coordinates
        meta.coordinates = coords
    else:
        meta.coordinates = None

    el.metadata = meta
    return el


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_element_type_mapping():
    """Verify Unstructured categories map to correct Vektra ElementType."""
    from vektra_ingest.extractors.unstructured import UnstructuredExtractor

    elements = [
        _make_element("Title text", category="Title"),
        _make_element("Body paragraph", category="NarrativeText"),
        _make_element("Unknown text", category="UncategorizedText"),
        _make_element("<table>...</table>", category="Table"),
        _make_element("- item one", category="ListItem"),
        _make_element("HEADER", category="Header"),
        _make_element("FOOTER", category="Footer"),
        _make_element("Fig 1: caption", category="FigureCaption"),
        _make_element("E = mc^2", category="Formula"),
    ]
    _mock_pdf_module.partition_pdf.return_value = elements

    extractor = UnstructuredExtractor()
    req = ExtractionRequest(
        content=b"%PDF-1.4 fake", content_type="application/pdf", filename="test.pdf"
    )
    chunks = []
    async for chunk in await extractor.extract(req):
        chunks.append(chunk)

    expected_types = [
        ElementType.TITLE,
        ElementType.TEXT,
        ElementType.TEXT,
        ElementType.TABLE,
        ElementType.LIST,
        ElementType.HEADER,
        ElementType.FOOTER,
        ElementType.FIGURE_CAPTION,
        ElementType.FORMULA,
    ]
    assert [c.element_type for c in chunks] == expected_types


@pytest.mark.asyncio
async def test_page_break_and_image_skipped():
    """PAGE_BREAK and IMAGE elements are skipped (metadata-only markers)."""
    from vektra_ingest.extractors.unstructured import UnstructuredExtractor

    elements = [
        _make_element("Real text", category="NarrativeText"),
        _make_element("---", category="PageBreak"),
        _make_element("[image]", category="Image"),
        _make_element("More text", category="NarrativeText"),
    ]
    _mock_pdf_module.partition_pdf.return_value = elements

    extractor = UnstructuredExtractor()
    req = ExtractionRequest(
        content=b"%PDF-1.4", content_type="application/pdf", filename="test.pdf"
    )
    chunks = []
    async for chunk in await extractor.extract(req):
        chunks.append(chunk)

    assert len(chunks) == 2
    assert chunks[0].text == "Real text"
    assert chunks[1].text == "More text"


@pytest.mark.asyncio
async def test_coordinates_extraction():
    """Verify bounding box is extracted from element coordinates."""
    from vektra_ingest.extractors.unstructured import UnstructuredExtractor

    elements = [
        _make_element(
            "Text with coords",
            coordinates=[(10.0, 20.0), (100.0, 20.0), (100.0, 50.0), (10.0, 50.0)],
            page_number=3,
        ),
    ]
    _mock_pdf_module.partition_pdf.return_value = elements

    extractor = UnstructuredExtractor()
    req = ExtractionRequest(
        content=b"%PDF-1.4", content_type="application/pdf", filename="test.pdf"
    )
    chunks = []
    async for chunk in await extractor.extract(req):
        chunks.append(chunk)

    assert len(chunks) == 1
    bbox = chunks[0].coordinates
    assert bbox is not None
    assert bbox.page == 3
    assert bbox.x0 == 10.0
    assert bbox.y0 == 20.0
    assert bbox.x1 == 100.0
    assert bbox.y1 == 50.0


@pytest.mark.asyncio
async def test_scanned_pdf_produces_text():
    """When Unstructured is used, scanned PDFs produce text via OCR
    instead of raising ERR-INGEST-003."""
    from vektra_ingest.extractors.unstructured import UnstructuredExtractor

    # Simulate OCR output from a scanned PDF
    elements = [
        _make_element("OCR extracted text from scanned page", category="NarrativeText"),
    ]
    _mock_pdf_module.partition_pdf.return_value = elements

    extractor = UnstructuredExtractor()
    req = ExtractionRequest(
        content=b"%PDF-1.4 scanned", content_type="application/pdf", filename="scan.pdf"
    )
    chunks = []
    async for chunk in await extractor.extract(req):
        chunks.append(chunk)

    assert len(chunks) == 1
    assert "OCR extracted text" in chunks[0].text


@pytest.mark.asyncio
async def test_table_html_format():
    """Tables with text_as_html use content_format='html' and HTML text."""
    from vektra_ingest.extractors.unstructured import UnstructuredExtractor

    html = "<table><tr><td>cell</td></tr></table>"
    elements = [
        _make_element("plain table text", category="Table", text_as_html=html),
    ]
    _mock_pdf_module.partition_pdf.return_value = elements

    extractor = UnstructuredExtractor()
    req = ExtractionRequest(
        content=b"%PDF-1.4", content_type="application/pdf", filename="test.pdf"
    )
    chunks = []
    async for chunk in await extractor.extract(req):
        chunks.append(chunk)

    assert len(chunks) == 1
    assert chunks[0].content_format == "html"
    assert chunks[0].text == html
    assert chunks[0].element_type == ElementType.TABLE


@pytest.mark.asyncio
async def test_supported_types():
    """Verify supported_types returns application/pdf."""
    from vektra_ingest.extractors.unstructured import UnstructuredExtractor

    extractor = UnstructuredExtractor()
    assert extractor.supported_types() == {"application/pdf"}


@pytest.mark.asyncio
async def test_empty_elements_produce_no_chunks():
    """Empty text elements are skipped."""
    from vektra_ingest.extractors.unstructured import UnstructuredExtractor

    elements = [
        _make_element("", category="NarrativeText"),
        _make_element("   ", category="NarrativeText"),
        _make_element("actual text", category="NarrativeText"),
    ]
    _mock_pdf_module.partition_pdf.return_value = elements

    extractor = UnstructuredExtractor()
    req = ExtractionRequest(
        content=b"%PDF-1.4", content_type="application/pdf", filename="test.pdf"
    )
    chunks = []
    async for chunk in await extractor.extract(req):
        chunks.append(chunk)

    assert len(chunks) == 1
    assert chunks[0].text == "actual text"


@pytest.mark.asyncio
async def test_metadata_includes_page_and_position():
    """Verify metadata has page and position fields."""
    from vektra_ingest.extractors.unstructured import UnstructuredExtractor

    elements = [
        _make_element("First", page_number=1),
        _make_element("Second", page_number=2),
    ]
    _mock_pdf_module.partition_pdf.return_value = elements

    extractor = UnstructuredExtractor()
    req = ExtractionRequest(
        content=b"%PDF-1.4", content_type="application/pdf", filename="doc.pdf"
    )
    chunks = []
    async for chunk in await extractor.extract(req):
        chunks.append(chunk)

    assert chunks[0].metadata["page"] == 1
    assert chunks[0].metadata["position"] == 0
    assert chunks[0].metadata["source_file"] == "doc.pdf"
    assert chunks[1].metadata["page"] == 2
    assert chunks[1].metadata["position"] == 1
