"""FixedSizeChunking: fixed-size token-based chunking (ARCH-037, REQ-002).

Phase 1 implementation of the ChunkingStrategy Protocol.

Uses tiktoken cl100k_base encoding (matches most LLM tokenizers).
Splits accumulated text into overlapping windows of VEKTRA_CHUNK_SIZE tokens
with VEKTRA_CHUNK_OVERLAP overlap.

All elements are accumulated in memory before splitting. This is acceptable
for Phase 1 document sizes (<= 50MB). Large documents produce ~50K tokens at
most (a 50MB text file is ~12.5M chars / 4 = ~3M tokens, but realistically
PDFs yield much less usable text).
"""
from __future__ import annotations

from typing import AsyncGenerator, AsyncIterator

import structlog

from vektra_shared.types import DocumentChunk, ElementType

log = structlog.get_logger(__name__)


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
        first_metadata: dict = {}

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
                log.warning("chunk_decode_failed", chunk_index=chunk_index, error=str(exc))
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
