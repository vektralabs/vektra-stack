"""PowerPointExtractor: PPTX text extraction (REQ-046, ARCH-009).

Phase 1 implementation of the DocumentExtractor Protocol using python-pptx.

Extracts slide titles, text boxes, and speaker notes in slide order.
Charts, SmartArt, tables, and embedded media are not extracted but generate
warnings.
"""
from __future__ import annotations

from typing import AsyncGenerator, AsyncIterator

import structlog

from vektra_shared.types import DocumentChunk, ElementType, ExtractionRequest, HealthStatus

log = structlog.get_logger(__name__)

_SUPPORTED = {
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-powerpoint",
}

# python-pptx MSO_SHAPE_TYPE constants
_MSO_CHART = 3
_MSO_TABLE = 19


class PowerPointExtractor:
    """DocumentExtractor for PPTX files using python-pptx."""

    def supported_types(self) -> set[str]:
        return _SUPPORTED

    async def extract(self, request: ExtractionRequest) -> AsyncIterator[DocumentChunk]:
        return self._extract_impl(request)

    async def _extract_impl(
        self, request: ExtractionRequest
    ) -> AsyncGenerator[DocumentChunk, None]:
        import io

        from pptx import Presentation  # type: ignore[import-untyped]
        from pptx.util import Emu  # noqa: F401

        prs = Presentation(io.BytesIO(request.content))

        for slide_num, slide in enumerate(prs.slides, 1):
            for shape in slide.shapes:
                # Warn about non-text shapes
                if hasattr(shape, "shape_type"):
                    if shape.shape_type == _MSO_CHART:
                        log.warning(
                            "pptx_chart_skipped",
                            filename=request.filename,
                            slide=slide_num,
                        )
                        continue
                    if shape.shape_type == _MSO_TABLE:
                        log.warning(
                            "pptx_table_skipped",
                            filename=request.filename,
                            slide=slide_num,
                        )
                        continue

                if not shape.has_text_frame:
                    continue

                text_parts = [
                    para.text for para in shape.text_frame.paragraphs if para.text.strip()
                ]
                text = "\n".join(text_parts).strip()
                if not text:
                    continue

                # Identify title placeholders (placeholder type 1=CENTER_TITLE, 13=TITLE)
                is_title = False
                try:
                    from pptx.enum.text import PP_ALIGN  # noqa: F401
                    ph = shape.placeholder_format
                    if ph is not None and ph.idx in (0, 1):
                        is_title = True
                except Exception:
                    pass

                yield DocumentChunk(
                    text=text,
                    element_type=ElementType.TITLE if is_title else ElementType.TEXT,
                    metadata={
                        "slide": slide_num,
                        "source_file": request.filename,
                    },
                )

            # Speaker notes
            if slide.has_notes_slide:
                try:
                    notes_frame = slide.notes_slide.notes_text_frame
                    notes_parts = [
                        para.text for para in notes_frame.paragraphs if para.text.strip()
                    ]
                    notes_text = "\n".join(notes_parts).strip()
                    if notes_text:
                        yield DocumentChunk(
                            text=notes_text,
                            element_type=ElementType.TEXT,
                            metadata={
                                "slide": slide_num,
                                "notes": True,
                                "source_file": request.filename,
                            },
                        )
                except Exception as exc:
                    log.warning(
                        "pptx_notes_failed",
                        filename=request.filename,
                        slide=slide_num,
                        error=str(exc),
                    )

    async def health_check(self) -> HealthStatus:
        try:
            from pptx import Presentation  # noqa: F401  type: ignore[import-untyped]
            return HealthStatus(status="healthy")
        except ImportError as exc:
            return HealthStatus(status="unhealthy", message=str(exc))
