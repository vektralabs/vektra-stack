"""Chunking strategies for vektra-ingest (ARCH-037, REQ-002, REQ-054).

Phase 1: FixedSizeChunking - fixed-size token windows with overlap.
Phase 2: DualStrategyChunking - text/table-aware chunking with parent-child hierarchy.

Both use tiktoken cl100k_base encoding (matches most LLM tokenizers).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any
from uuid import uuid4

import structlog

from vektra_shared.types import DocumentChunk, ElementType

log = structlog.get_logger(__name__)

# Element types that are accumulated as text and split with overlap
_TEXT_TYPES = frozenset(
    {
        ElementType.TEXT,
        ElementType.TITLE,
        ElementType.LIST,
        ElementType.HEADER,
        ElementType.FOOTER,
        ElementType.FIGURE_CAPTION,
        ElementType.FORMULA,
    }
)

# Element types that are skipped entirely
_SKIP_TYPES = frozenset(
    {
        ElementType.PAGE_BREAK,
        ElementType.IMAGE,
    }
)


class FixedSizeChunking:
    """ChunkingStrategy using fixed-size token windows with overlap.

    Tokenizes with tiktoken cl100k_base and splits into chunks of
    `chunk_size` tokens. Adjacent chunks share `chunk_overlap` tokens.

    Args:
        chunk_size: Maximum tokens per chunk (default 1000).
        chunk_overlap: Token overlap between consecutive chunks (default 200).
    """

    def __init__(self, chunk_size: int = 1000, chunk_overlap: int = 200) -> None:
        if chunk_overlap >= chunk_size:
            raise ValueError(
                f"chunk_overlap ({chunk_overlap}) must be less than "
                f"chunk_size ({chunk_size})"
            )
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap

    async def chunk(
        self, elements: AsyncIterator[DocumentChunk]
    ) -> AsyncIterator[DocumentChunk]:
        return self._chunk_impl(elements)

    async def _chunk_impl(
        self, elements: AsyncIterator[DocumentChunk]
    ) -> AsyncGenerator[DocumentChunk, None]:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")

        # Accumulate all tokens + carry forward the first element's metadata
        all_tokens: list[int] = []
        first_metadata: dict[str, Any] = {}

        async for element in elements:
            tokens = enc.encode(element.text)
            if not all_tokens and tokens:
                first_metadata = dict(element.metadata)
            all_tokens.extend(tokens)

        if not all_tokens:
            return

        step = self._chunk_size - self._chunk_overlap
        chunk_index = 0
        start = 0

        while start < len(all_tokens):
            end = min(start + self._chunk_size, len(all_tokens))
            chunk_tokens = all_tokens[start:end]

            try:
                chunk_text = enc.decode(chunk_tokens)
            except Exception as exc:
                log.warning(
                    "chunk_decode_failed", chunk_index=chunk_index, error=str(exc)
                )
                start += step
                chunk_index += 1
                continue

            yield DocumentChunk(
                text=chunk_text,
                element_type=ElementType.TEXT,
                metadata={
                    **first_metadata,
                    "chunk_index": chunk_index,
                    "token_count": len(chunk_tokens),
                },
            )

            chunk_index += 1

            if end >= len(all_tokens):
                break
            start += step


class DualStrategyChunking:
    """ChunkingStrategy with text/table-aware splitting and parent-child hierarchy.

    Behavior by element type:
    - TEXT, TITLE, LIST, HEADER, FOOTER, FIGURE_CAPTION, FORMULA:
      Accumulated into a text buffer, then split with overlap (same as
      FixedSizeChunking). Each child chunk references a parent chunk.
    - TABLE: Never split. Each table element becomes exactly one chunk
      with content_format="html" (or original format).
    - PAGE_BREAK, IMAGE: Skipped (metadata-only markers).

    Parent-child hierarchy (2 levels):
    - Level 0 (parent): every parent_chunk_size tokens, a parent chunk is
      emitted with the full accumulated text. parent_id = None.
    - Level 1 (child): normal fixed-size chunks with overlap.
      parent_id = ID of the enclosing parent chunk.

    Args:
        text_chunk_size: Maximum tokens per child text chunk (default 1000).
        text_chunk_overlap: Token overlap between consecutive text chunks (default 200).
        parent_chunk_size: Token threshold for parent chunk boundaries (default 3000).
    """

    def __init__(
        self,
        text_chunk_size: int = 1000,
        text_chunk_overlap: int = 200,
        parent_chunk_size: int = 3000,
    ) -> None:
        if text_chunk_overlap >= text_chunk_size:
            raise ValueError(
                f"text_chunk_overlap ({text_chunk_overlap}) must be less than "
                f"text_chunk_size ({text_chunk_size})"
            )
        if parent_chunk_size < text_chunk_size:
            raise ValueError(
                f"parent_chunk_size ({parent_chunk_size}) must be >= "
                f"text_chunk_size ({text_chunk_size})"
            )
        self._text_chunk_size = text_chunk_size
        self._text_chunk_overlap = text_chunk_overlap
        self._parent_chunk_size = parent_chunk_size

    async def chunk(
        self, elements: AsyncIterator[DocumentChunk]
    ) -> AsyncIterator[DocumentChunk]:
        return self._chunk_impl(elements)

    async def _chunk_impl(
        self, elements: AsyncIterator[DocumentChunk]
    ) -> AsyncGenerator[DocumentChunk, None]:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")

        # Accumulate text elements and table elements in order
        # We process elements in two passes:
        # 1. Collect all elements, separating text runs from tables
        # 2. Split text runs into parent + child chunks, emit tables as-is

        # A "segment" is either a text run (tokens + metadata) or a table element
        segments: list[
            tuple[str, Any]
        ] = []  # ("text", (tokens_list, metadata)) or ("table", DocumentChunk)
        current_text_tokens: list[int] = []
        current_segment_metadata: dict[str, Any] = {}

        async for element in elements:
            if element.element_type in _SKIP_TYPES:
                continue

            if element.element_type == ElementType.TABLE:
                # Flush any accumulated text tokens as a text segment
                if current_text_tokens:
                    segments.append(
                        ("text", (list(current_text_tokens), current_segment_metadata))
                    )
                    current_text_tokens = []
                    current_segment_metadata = {}
                segments.append(("table", element))
            elif element.element_type in _TEXT_TYPES:
                tokens = enc.encode(element.text)
                if not current_text_tokens and tokens:
                    current_segment_metadata = dict(element.metadata)
                current_text_tokens.extend(tokens)
            # Unknown types: treat as text
            else:
                tokens = enc.encode(element.text)
                current_text_tokens.extend(tokens)

        # Flush remaining text tokens
        if current_text_tokens:
            segments.append(
                ("text", (list(current_text_tokens), current_segment_metadata))
            )

        # Now emit chunks from segments
        chunk_index = 0

        for seg_type, seg_data in segments:
            if seg_type == "table":
                table_el: DocumentChunk = seg_data
                content_format = table_el.content_format
                if content_format == "text":
                    content_format = "html"
                yield DocumentChunk(
                    text=table_el.text,
                    element_type=ElementType.TABLE,
                    content_format=content_format,
                    metadata={
                        **table_el.metadata,
                        "chunk_index": chunk_index,
                    },
                    parent_id=None,  # tables are standalone
                    coordinates=table_el.coordinates,
                )
                chunk_index += 1

            elif seg_type == "text":
                text_tokens: list[int]
                seg_metadata: dict[str, Any]
                text_tokens, seg_metadata = seg_data
                if not text_tokens:
                    continue

                # Split into parent-sized sections, then child chunks within each
                parent_start = 0
                while parent_start < len(text_tokens):
                    parent_end = min(
                        parent_start + self._parent_chunk_size, len(text_tokens)
                    )
                    parent_tokens = text_tokens[parent_start:parent_end]

                    # Emit parent chunk (level 0)
                    parent_id = str(uuid4())
                    try:
                        parent_text = enc.decode(parent_tokens)
                    except Exception as exc:
                        log.warning("parent_chunk_decode_failed", error=str(exc))
                        parent_start = parent_end
                        continue

                    yield DocumentChunk(
                        text=parent_text,
                        element_type=ElementType.TEXT,
                        metadata={
                            **seg_metadata,
                            "chunk_index": chunk_index,
                            "token_count": len(parent_tokens),
                            "chunk_level": "parent",
                        },
                        parent_id=None,
                    )
                    chunk_index += 1

                    # Emit child chunks (level 1) within this parent
                    step = self._text_chunk_size - self._text_chunk_overlap
                    child_start = 0

                    while child_start < len(parent_tokens):
                        child_end = min(
                            child_start + self._text_chunk_size, len(parent_tokens)
                        )
                        child_tokens = parent_tokens[child_start:child_end]

                        try:
                            child_text = enc.decode(child_tokens)
                        except Exception as exc:
                            log.warning("child_chunk_decode_failed", error=str(exc))
                            child_start += step
                            continue

                        yield DocumentChunk(
                            text=child_text,
                            element_type=ElementType.TEXT,
                            metadata={
                                **seg_metadata,
                                "chunk_index": chunk_index,
                                "token_count": len(child_tokens),
                                "chunk_level": "child",
                            },
                            parent_id=parent_id,
                        )
                        chunk_index += 1

                        if child_end >= len(parent_tokens):
                            break
                        child_start += step

                    parent_start = parent_end
