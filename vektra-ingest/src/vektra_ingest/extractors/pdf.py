"""PdfplumberExtractor: PDF text extraction (REQ-002, REQ-016, ARCH-009).

Phase 1 implementation of the DocumentExtractor Protocol using pdfplumber.

Scanned PDF detection: if the average extracted text across the first 5 pages
is < 100 characters, the file is treated as scanned (no text layer) and
IngestError(ERR-INGEST-003) is raised before any chunking (fail fast).
"""
from __future__ import annotations

import io
from typing import AsyncGenerator, AsyncIterator

import structlog

from vektra_shared.errors import ERR_INGEST_003
from vektra_shared.types import DocumentChunk, ElementType, ExtractionRequest, HealthStatus

from vektra_ingest.exceptions import IngestError

log = structlog.get_logger(__name__)

_SCAN_THRESHOLD = 100   # avg chars/page below which we call it scanned
_SCAN_SAMPLE_PAGES = 5  # pages to sample for scanned detection


class PdfplumberExtractor:
    """DocumentExtractor for PDF files using pdfplumber.

    Extracts text page by page. Raises IngestError(ERR-INGEST-003) for
    scanned PDFs (avg < 100 chars over first 5 pages) before returning any chunks.
    """

    def supported_types(self) -> set[str]:
        return {"application/pdf"}

    async def extract(self, request: ExtractionRequest) -> AsyncIterator[DocumentChunk]:
        return self._extract_impl(request)

    async def _extract_impl(
        self, request: ExtractionRequest
    ) -> AsyncGenerator[DocumentChunk, None]:
        import pdfplumber  # type: ignore[import-untyped]

        with pdfplumber.open(io.BytesIO(request.content)) as pdf:
            pages = pdf.pages

            # Scanned PDF detection: sample first _SCAN_SAMPLE_PAGES pages
            sample = pages[:_SCAN_SAMPLE_PAGES]
            if sample:
                total_chars = sum(len(p.extract_text() or "") for p in sample)
                avg_chars = total_chars / len(sample)
                if avg_chars < _SCAN_THRESHOLD:
                    log.info(
                        "scanned_pdf_detected",
                        filename=request.filename,
                        avg_chars_per_page=avg_chars,
                    )
                    raise IngestError(
                        error_code=ERR_INGEST_003,
                        message=(
                            f"PDF appears to be scanned (avg {avg_chars:.1f} chars/page "
                            f"across first {len(sample)} pages). "
                            "A text layer is required for ingestion."
                        ),
                    )

            # Extract text page by page
            for page_num, page in enumerate(pages, 1):
                text = page.extract_text() or ""
                text = text.strip()
                if text:
                    yield DocumentChunk(
                        text=text,
                        element_type=ElementType.TEXT,
                        metadata={
                            "page": page_num,
                            "source_file": request.filename,
                        },
                    )

    async def health_check(self) -> HealthStatus:
        try:
            import pdfplumber  # noqa: F401  type: ignore[import-untyped]
            return HealthStatus(status="healthy")
        except ImportError as exc:
            return HealthStatus(status="unhealthy", message=str(exc))
