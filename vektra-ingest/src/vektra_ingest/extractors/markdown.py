"""MarkdownExtractor: Markdown file text extraction (REQ-002).

Splits markdown content on heading boundaries (# through ######).
Each section becomes a DocumentChunk with element_type=TEXT and
content_format="markdown". Metadata includes heading_level and
section_title.

No external dependencies needed; uses regex line-by-line parsing.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator, AsyncIterator

from vektra_shared.types import DocumentChunk, ElementType, ExtractionRequest

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")


class MarkdownExtractor:
    """DocumentExtractor for Markdown files."""

    def supported_types(self) -> set[str]:
        return {"text/markdown", "text/x-markdown"}

    async def extract(self, request: ExtractionRequest) -> AsyncIterator[DocumentChunk]:
        return self._extract_impl(request)

    async def _extract_impl(
        self, request: ExtractionRequest
    ) -> AsyncGenerator[DocumentChunk, None]:
        text = request.content.decode("utf-8", errors="replace")
        lines = text.splitlines()

        current_title: str | None = None
        current_level: int | None = None
        current_lines: list[str] = []
        in_code_block = False

        for line in lines:
            # Track fenced code blocks (``` or ~~~)
            if _FENCE_RE.match(line.strip()):
                in_code_block = not in_code_block
                current_lines.append(line)
                continue

            match = _HEADING_RE.match(line) if not in_code_block else None
            if match:
                # Yield previous section if it has content
                if current_lines:
                    body = "\n".join(current_lines).strip()
                    if body:
                        yield DocumentChunk(
                            text=body,
                            element_type=ElementType.TEXT,
                            content_format="markdown",
                            metadata={
                                **(
                                    {"heading_level": current_level}
                                    if current_level
                                    else {}
                                ),
                                **(
                                    {"section_title": current_title}
                                    if current_title
                                    else {}
                                ),
                            },
                        )
                    current_lines = []

                current_level = len(match.group(1))
                current_title = match.group(2).strip()
                # Include the heading line in the section text
                current_lines.append(line)
            else:
                current_lines.append(line)

        # Yield final section
        if current_lines:
            body = "\n".join(current_lines).strip()
            if body:
                yield DocumentChunk(
                    text=body,
                    element_type=ElementType.TEXT,
                    content_format="markdown",
                    metadata={
                        **({"heading_level": current_level} if current_level else {}),
                        **({"section_title": current_title} if current_title else {}),
                    },
                )
