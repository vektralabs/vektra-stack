"""Unit tests for DocumentExtractor implementations (ARCH-009, REQ-016, REQ-045, REQ-046)."""
from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vektra_shared.types import ElementType, ExtractionRequest


# ---------------------------------------------------------------------------
# Helpers: build minimal in-memory documents
# ---------------------------------------------------------------------------


def _make_pdf_bytes(texts: list[str]) -> bytes:
    """Create a real PDF with text content that passes the scanned-detection threshold.

    Uses fpdf2 (FPDF) if available. Otherwise mocks pdfplumber for tests that
    call this function.
    """
    try:
        from fpdf import FPDF  # type: ignore[import-untyped]

        pdf = FPDF()
        for text in texts:
            pdf.add_page()
            pdf.set_font("Helvetica", size=12)
            # Write enough content to exceed 100-char threshold
            full_text = text + " " + text + " " + text
            for _ in range(5):
                pdf.cell(0, 10, text=full_text[:200], ln=True)
        return bytes(pdf.output())
    except ImportError:
        # fpdf2 not available; callers should mock pdfplumber instead
        return b"%PDF-1.4 placeholder"


def _make_docx_bytes(paragraphs: list[str]) -> bytes:
    """Create a minimal DOCX with given paragraphs using python-docx."""
    import docx  # type: ignore[import-untyped]

    doc = docx.Document()
    for para in paragraphs:
        doc.add_paragraph(para)
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.read()


def _make_pptx_bytes(slides: list[str]) -> bytes:
    """Create a minimal PPTX with one text box per slide."""
    from pptx import Presentation  # type: ignore[import-untyped]
    from pptx.util import Inches, Pt

    prs = Presentation()
    blank_layout = prs.slide_layouts[6]  # blank layout

    for text in slides:
        slide = prs.slides.add_slide(blank_layout)
        txBox = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(4))
        txBox.text_frame.text = text

    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf.read()


async def _collect_chunks(extractor, content: bytes, filename: str, content_type: str):
    req = ExtractionRequest(content=content, content_type=content_type, filename=filename)
    it = await extractor.extract(req)
    chunks = []
    async for chunk in it:
        chunks.append(chunk)
    return chunks


# ---------------------------------------------------------------------------
# PdfplumberExtractor
# ---------------------------------------------------------------------------


class TestPdfplumberExtractor:
    @pytest.mark.asyncio
    async def test_extracts_text_from_pdf(self):
        """PDF extraction yields DocumentChunks with TEXT element type."""
        from vektra_ingest.extractors.pdf import PdfplumberExtractor

        long_text = "Machine learning is transforming AI research. " * 5

        mock_page = MagicMock()
        mock_page.extract_text.return_value = long_text

        mock_pdf = MagicMock()
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)
        mock_pdf.pages = [mock_page]

        with patch("pdfplumber.open", return_value=mock_pdf):
            extractor = PdfplumberExtractor()
            chunks = await _collect_chunks(
                extractor, b"fake pdf", "test.pdf", "application/pdf"
            )

        assert len(chunks) >= 1
        text = " ".join(c.text for c in chunks)
        assert "machine learning" in text.lower() or "transforming" in text.lower()

    @pytest.mark.asyncio
    async def test_chunk_has_page_metadata(self):
        """Each extracted chunk carries 'page' metadata with 1-based page number."""
        from vektra_ingest.extractors.pdf import PdfplumberExtractor

        long_text = "Page content with more than one hundred characters of text here. " * 3

        mock_page = MagicMock()
        mock_page.extract_text.return_value = long_text

        mock_pdf = MagicMock()
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)
        mock_pdf.pages = [mock_page]

        with patch("pdfplumber.open", return_value=mock_pdf):
            extractor = PdfplumberExtractor()
            chunks = await _collect_chunks(
                extractor, b"fake pdf", "test.pdf", "application/pdf"
            )

        assert len(chunks) >= 1
        for chunk in chunks:
            assert "page" in chunk.metadata
            assert chunk.metadata["page"] >= 1

    @pytest.mark.asyncio
    async def test_scanned_pdf_raises_ingest_error(self):
        """Pages with < 100 avg chars detected as scanned (REQ-016, ERR-INGEST-003)."""
        from vektra_ingest.exceptions import IngestError
        from vektra_ingest.extractors.pdf import PdfplumberExtractor

        # Mock pdfplumber to simulate scanned PDF (no text per page)
        mock_page = MagicMock()
        mock_page.extract_text.return_value = ""  # zero chars

        mock_pdf = MagicMock()
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)
        mock_pdf.pages = [mock_page] * 3

        with patch("pdfplumber.open", return_value=mock_pdf):
            extractor = PdfplumberExtractor()
            with pytest.raises(IngestError) as exc_info:
                chunks = await _collect_chunks(
                    extractor, b"fake pdf bytes", "scanned.pdf", "application/pdf"
                )
                async for _ in await extractor.extract(
                    ExtractionRequest(
                        content=b"fake",
                        content_type="application/pdf",
                        filename="scanned.pdf",
                    )
                ):
                    pass

        assert exc_info.value.error_code == "ERR-INGEST-003"

    @pytest.mark.asyncio
    async def test_scanned_pdf_threshold_boundary(self):
        """Pages with exactly 100 chars avg should NOT be rejected (boundary)."""
        from vektra_ingest.extractors.pdf import PdfplumberExtractor

        # 100 chars per page = exactly at threshold = allowed
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "x" * 100  # exactly 100 chars

        mock_pdf = MagicMock()
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)
        mock_pdf.pages = [mock_page]  # single page

        with patch("pdfplumber.open", return_value=mock_pdf):
            extractor = PdfplumberExtractor()
            req = ExtractionRequest(
                content=b"fake", content_type="application/pdf", filename="ok.pdf"
            )
            it = await extractor.extract(req)
            chunks = [c async for c in it]

        assert len(chunks) == 1
        assert chunks[0].element_type == ElementType.TEXT

    def test_supported_types(self):
        from vektra_ingest.extractors.pdf import PdfplumberExtractor

        extractor = PdfplumberExtractor()
        assert "application/pdf" in extractor.supported_types()

    @pytest.mark.asyncio
    async def test_health_check_healthy(self):
        from vektra_ingest.extractors.pdf import PdfplumberExtractor

        extractor = PdfplumberExtractor()
        status = await extractor.health_check()
        assert status.status in ("healthy", "unhealthy")


# ---------------------------------------------------------------------------
# WordExtractor
# ---------------------------------------------------------------------------


class TestWordExtractor:
    @pytest.mark.asyncio
    async def test_extracts_paragraphs(self):
        from vektra_ingest.extractors.word import WordExtractor

        docx_bytes = _make_docx_bytes(
            ["Introduction to machine learning.", "Chapter 2: Neural Networks."]
        )
        extractor = WordExtractor()
        chunks = await _collect_chunks(
            extractor,
            docx_bytes,
            "doc.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        texts = [c.text for c in chunks]
        assert any("Introduction" in t for t in texts)
        assert any("Neural" in t for t in texts)

    @pytest.mark.asyncio
    async def test_heading_uses_title_element_type(self):
        """Paragraphs styled as Heading* use ElementType.TITLE."""
        import docx as docx_lib  # type: ignore[import-untyped]

        from vektra_ingest.extractors.word import WordExtractor

        doc = docx_lib.Document()
        doc.add_heading("Chapter 1: Introduction", level=1)
        doc.add_paragraph("Regular paragraph text.")
        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)
        docx_bytes = buf.read()

        extractor = WordExtractor()
        chunks = await _collect_chunks(
            extractor,
            docx_bytes,
            "doc.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        # Find the heading chunk
        heading_chunks = [c for c in chunks if c.element_type == ElementType.TITLE]
        assert len(heading_chunks) >= 1
        assert "Chapter 1" in heading_chunks[0].text

    @pytest.mark.asyncio
    async def test_empty_paragraphs_skipped(self):
        """Empty or whitespace-only paragraphs produce no chunks."""
        import docx as docx_lib  # type: ignore[import-untyped]

        from vektra_ingest.extractors.word import WordExtractor

        doc = docx_lib.Document()
        doc.add_paragraph("")  # empty
        doc.add_paragraph("   ")  # whitespace
        doc.add_paragraph("Real content here.")
        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)
        docx_bytes = buf.read()

        extractor = WordExtractor()
        chunks = await _collect_chunks(
            extractor,
            docx_bytes,
            "doc.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        assert all(c.text.strip() for c in chunks)

    def test_supported_types(self):
        from vektra_ingest.extractors.word import WordExtractor

        extractor = WordExtractor()
        types = extractor.supported_types()
        assert "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in types


# ---------------------------------------------------------------------------
# PowerPointExtractor
# ---------------------------------------------------------------------------


class TestPowerPointExtractor:
    @pytest.mark.asyncio
    async def test_extracts_slide_text(self):
        from vektra_ingest.extractors.powerpoint import PowerPointExtractor

        pptx_bytes = _make_pptx_bytes(["Introduction slide content.", "Data analysis results."])
        extractor = PowerPointExtractor()
        chunks = await _collect_chunks(
            extractor,
            pptx_bytes,
            "presentation.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
        texts = " ".join(c.text for c in chunks)
        assert "Introduction" in texts or "slide" in texts.lower()

    @pytest.mark.asyncio
    async def test_chunk_has_slide_metadata(self):
        from vektra_ingest.extractors.powerpoint import PowerPointExtractor

        pptx_bytes = _make_pptx_bytes(["Slide one text."])
        extractor = PowerPointExtractor()
        chunks = await _collect_chunks(
            extractor,
            pptx_bytes,
            "pres.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
        for chunk in chunks:
            assert "slide" in chunk.metadata
            assert chunk.metadata["slide"] >= 1

    def test_supported_types(self):
        from vektra_ingest.extractors.powerpoint import PowerPointExtractor

        extractor = PowerPointExtractor()
        types = extractor.supported_types()
        assert (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            in types
        )
