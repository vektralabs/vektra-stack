"""UnstructuredExtractor: OCR-capable PDF extraction (Phase 2, ARCH-009).

Uses the Unstructured library with strategy="auto" to handle both
text-layer PDFs and scanned (OCR-required) PDFs. When Unstructured
is not installed, this module raises ImportError at instantiation.

Element type mapping from Unstructured categories to Vektra ElementType:
- Title -> TITLE
- NarrativeText, UncategorizedText -> TEXT
- Table -> TABLE
- ListItem -> LIST
- Header -> HEADER
- Footer -> FOOTER
- FigureCaption -> FIGURE_CAPTION
- Formula -> FORMULA
- PageBreak -> PAGE_BREAK
- Image -> IMAGE
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

import structlog

from vektra_shared.types import (
    BoundingBox,
    DocumentChunk,
    ElementType,
    ExtractionRequest,
    HealthStatus,
)

log = structlog.get_logger(__name__)

# Unstructured category -> Vektra ElementType
_CATEGORY_MAP: dict[str, ElementType] = {
    "Title": ElementType.TITLE,
    "NarrativeText": ElementType.TEXT,
    "UncategorizedText": ElementType.TEXT,
    "Table": ElementType.TABLE,
    "ListItem": ElementType.LIST,
    "Header": ElementType.HEADER,
    "Footer": ElementType.FOOTER,
    "FigureCaption": ElementType.FIGURE_CAPTION,
    "Formula": ElementType.FORMULA,
    "PageBreak": ElementType.PAGE_BREAK,
    "Image": ElementType.IMAGE,
}


def _map_element_type(category: str) -> ElementType:
    """Map an Unstructured element category to Vektra ElementType."""
    return _CATEGORY_MAP.get(category, ElementType.TEXT)


def _detect_format(element: Any) -> str:
    """Detect content format from an Unstructured element."""
    category = getattr(element, "category", "")
    if category == "Table":
        # Tables from Unstructured often have HTML representation
        if hasattr(element, "metadata") and hasattr(element.metadata, "text_as_html"):
            if element.metadata.text_as_html:
                return "html"
    return "text"


def _extract_coordinates(element: Any) -> BoundingBox | None:
    """Extract bounding box coordinates from an Unstructured element."""
    meta = getattr(element, "metadata", None)
    if meta is None:
        return None

    coords = getattr(meta, "coordinates", None)
    if coords is None:
        return None

    points = getattr(coords, "points", None)
    page_number = getattr(meta, "page_number", None)
    if points is None or page_number is None:
        return None

    try:
        # Unstructured coordinates are a list of (x, y) tuples
        # representing the bounding polygon. Extract the bounding box.
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return BoundingBox(
            page=page_number,
            x0=min(xs),
            y0=min(ys),
            x1=max(xs),
            y1=max(ys),
        )
    except (TypeError, IndexError, ValueError):
        return None


class UnstructuredExtractor:
    """DocumentExtractor for OCR and advanced element classification.

    Uses Unstructured's partition_pdf with strategy="auto":
    - Text-layer PDFs: fast extraction (same quality as pdfplumber)
    - Scanned PDFs: OCR via tesseract (automatic detection)

    Requires: pip install 'vektra-ingest[ocr]'
    """

    def __init__(self) -> None:
        try:
            import unstructured.partition.pdf  # noqa: F401
        except ImportError:
            raise ImportError(
                "Unstructured is not installed. Install it with: "
                "pip install 'vektra-ingest[ocr]' or "
                "pip install 'unstructured[pdf]'"
            )

    def supported_types(self) -> set[str]:
        return {"application/pdf"}

    async def extract(self, request: ExtractionRequest) -> AsyncIterator[DocumentChunk]:
        return self._extract_impl(request)

    async def _extract_impl(
        self, request: ExtractionRequest
    ) -> AsyncGenerator[DocumentChunk, None]:
        from unstructured.partition.pdf import partition_pdf

        elements = await asyncio.to_thread(
            partition_pdf,
            file=io.BytesIO(request.content),
            strategy="auto",
        )

        position = 0
        for el in elements:
            text = str(el).strip()
            if not text:
                continue

            category = getattr(el, "category", "UncategorizedText")
            element_type = _map_element_type(category)

            # Skip PAGE_BREAK and IMAGE (metadata-only markers)
            if element_type in (ElementType.PAGE_BREAK, ElementType.IMAGE):
                continue

            content_format = _detect_format(el)
            coordinates = _extract_coordinates(el)

            meta = getattr(el, "metadata", None)
            page_number = getattr(meta, "page_number", None) if meta else None

            metadata: dict[str, Any] = {
                "position": position,
                "source_file": request.filename,
            }
            if page_number is not None:
                metadata["page"] = page_number

            # Use HTML text for tables when available
            if content_format == "html" and hasattr(meta, "text_as_html"):
                text = meta.text_as_html

            yield DocumentChunk(
                text=text,
                element_type=element_type,
                content_format=content_format,
                metadata=metadata,
                coordinates=coordinates,
            )
            position += 1

    async def health_check(self) -> HealthStatus:
        try:
            import unstructured.partition.pdf  # noqa: F401

            return HealthStatus(status="healthy")
        except ImportError as exc:
            return HealthStatus(status="unhealthy", message=str(exc))
