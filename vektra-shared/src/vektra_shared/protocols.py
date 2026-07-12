"""Protocol interfaces for the Vektra platform (ARCH-029, ARCH-035 to ARCH-038).

All 9 Protocol interfaces are defined here. Components implement these
interfaces - they never import each other directly (ADR-0005).

Phase 1 implementations live in their respective component packages:
- LLMProvider: vektra_core (LitellmProvider)
- EmbeddingProvider: vektra_index (SentenceTransformersProvider)
- SparseEmbeddingProvider: not registered in Phase 1 (ARCH-053)
- VectorStoreProvider: vektra_index (PgvectorProvider)
- DocumentExtractor: vektra_ingest (PdfplumberExtractor, WordExtractor, PowerPointExtractor)
- ChunkingStrategy: vektra_ingest (FixedSizeChunking)
- QueryPipeline: vektra_core (SimpleQueryPipeline)
- SafeguardHook: vektra_shared (PassthroughSafeguard - Phase 1 default)
- EventEmitter: vektra_shared (NoOpEventEmitter - Phase 1 default)
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, Protocol, runtime_checkable

from vektra_shared.types import (
    ChunkEmbedding,
    CompletionChunk,
    CompletionResponse,
    DocumentChunk,
    ExtractionRequest,
    HealthStatus,
    Message,
    QueryChunk,
    QueryEmbedding,
    QueryRequest,
    QueryResponse,
    QueryTrace,
    SafeguardContext,
    SafeguardResult,
    SearchFilters,
    SearchMode,
    SearchResult,
    SparseVector,
)


@runtime_checkable
class LLMProvider(Protocol):
    """Multi-provider LLM abstraction with graceful degradation (ADR-0008)."""

    async def complete(
        self,
        messages: list[Message],
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> CompletionResponse: ...

    async def stream(
        self,
        messages: list[Message],
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[CompletionChunk]: ...

    async def health_check(self) -> HealthStatus: ...

    def count_tokens(self, text: str, model: str) -> int: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Shared embedding generation with asymmetric model support (ADR-0013)."""

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, query: str) -> list[float]: ...

    def dimensions(self) -> int: ...

    async def health_check(self) -> HealthStatus: ...


@runtime_checkable
class SparseEmbeddingProvider(Protocol):
    """Sparse vector generation for hybrid search (ARCH-053).

    Phase 1: not registered in ProviderRegistry.
    Phase 2: BM25 or SPLADE via fastembed.
    """

    async def embed_documents(self, texts: list[str]) -> list[SparseVector]: ...

    async def embed_query(self, text: str) -> SparseVector: ...

    def vocab_size(self) -> int | None: ...


@runtime_checkable
class VectorStoreProvider(Protocol):
    """Pluggable vector store backend (REQ-050, ARCH-051, ARCH-052)."""

    async def store(
        self,
        namespace: str,
        chunks: Sequence[ChunkEmbedding],
    ) -> list[str]: ...

    async def search(
        self,
        namespace: str,
        query_embedding: QueryEmbedding,
        top_k: int,
        search_mode: SearchMode = SearchMode.DENSE,
        filters: SearchFilters | None = None,
        raw_filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]: ...

    async def retrieve(
        self,
        namespace: str,
        chunk_ids: list[str],
    ) -> list[SearchResult]: ...

    async def delete(self, namespace: str, ids: list[str]) -> int: ...

    async def health_check(self) -> HealthStatus: ...


@runtime_checkable
class DocumentExtractor(Protocol):
    """Document content extraction with element classification."""

    def supported_types(self) -> set[str]: ...

    async def extract(
        self,
        request: ExtractionRequest,
    ) -> AsyncIterator[DocumentChunk]: ...

    async def health_check(self) -> HealthStatus: ...


@runtime_checkable
class ChunkingStrategy(Protocol):
    """Pluggable text chunking strategy (ARCH-037).

    Phase 1: FixedSizeChunking (1000 tokens, 200 overlap).
    """

    async def chunk(
        self,
        elements: AsyncIterator[DocumentChunk],
    ) -> AsyncIterator[DocumentChunk]: ...


@runtime_checkable
class QueryPipeline(Protocol):
    """RAG query pipeline abstraction (ADR-0014).

    Phase 1: SimpleQueryPipeline (embed -> search -> relevance_filter
    -> build_prompt -> LLM, with graceful degradation).
    """

    async def execute(
        self,
        query: QueryRequest,
    ) -> tuple[QueryResponse, QueryTrace]: ...

    async def execute_stream(
        self,
        query: QueryRequest,
    ) -> AsyncIterator[QueryChunk]: ...


@runtime_checkable
class SafeguardHook(Protocol):
    """Pre/post query safeguards with content modification support (ARCH-049).

    Three trust boundary points: pre_query, post_retrieval, pre_response.
    Phase 1: PassthroughSafeguard (no-op).
    """

    async def pre_query(
        self,
        query_ref: str,
        context: SafeguardContext,
    ) -> SafeguardResult: ...

    async def post_retrieval(
        self,
        query_ref: str,
        results: list[SearchResult],
        context: SafeguardContext,
    ) -> SafeguardResult: ...

    async def pre_response(
        self,
        response_ref: str,
        context: SafeguardContext,
    ) -> SafeguardResult: ...


@runtime_checkable
class EventEmitter(Protocol):
    """Internal event hooks (ARCH-038).

    Phase 1: NoOpEventEmitter (events silently discarded).
    Emission points: document.indexed, document.failed, query.completed,
    safeguard.triggered, apikey.created, apikey.revoked.
    """

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None: ...
