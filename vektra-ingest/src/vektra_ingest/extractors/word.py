"""WordExtractor: DOCX text extraction (REQ-045, ARCH-009).

Phase 1 implementation of the DocumentExtractor Protocol using python-docx.

Extracts paragraphs and headings. Tables, images, and embedded objects are
not extracted but generate warnings. Heading paragraphs use ElementType.TITLE.
"""

from __future__ import annotations

import io
from collections.abc import AsyncGenerator, AsyncIterator

import structlog

from vektra_shared.types import (
    DocumentChunk,
    ElementType,
    ExtractionRequest,
    HealthStatus,
)

log = structlog.get_logger(__name__)

_SUPPORTED = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
}


class WordExtractor:
    """DocumentExtractor for DOCX/DOC files using python-docx."""

    def supported_types(self) -> set[str]:
        return _SUPPORTED

    async def extract(self, request: ExtractionRequest) -> AsyncIterator[DocumentChunk]:
        return self._extract_impl(request)

    async def _extract_impl(
        self, request: ExtractionRequest
    ) -> AsyncGenerator[DocumentChunk, None]:
        import docx

        doc = docx.Document(io.BytesIO(request.content))

        # Warn about non-text elements (not extracted in Phase 1)
        if doc.tables:
            log.warning(
                "word_tables_skipped",
                filename=request.filename,
                table_count=len(doc.tables),
            )

        # Warn about inline images via relationships
        for rel in doc.part.rels.values():
            if "image" in rel.reltype:
                log.warning("word_images_skipped", filename=request.filename)
                break

        position = 0
        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue

            # Detect headings by style name
            style_name = para.style.name if para.style else ""
            element_type = (
                ElementType.TITLE
                if style_name.startswith("Heading")
                else ElementType.TEXT
            )

            yield DocumentChunk(
                text=text,
                element_type=element_type,
                metadata={
                    "position": position,
                    "source_file": request.filename,
                },
            )
            position += 1

    async def health_check(self) -> HealthStatus:
        try:
            import docx  # noqa: F401  type: ignore[import-untyped]

            return HealthStatus(status="healthy")
        except ImportError as exc:
            return HealthStatus(status="unhealthy", message=str(exc))
