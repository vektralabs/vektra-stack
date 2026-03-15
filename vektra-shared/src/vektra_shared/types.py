"""Shared domain types for the Vektra platform.

All types in this module are Pydantic models or standard dataclasses.
ORM models live inside each component, not here. This module has no
imports from other vektra_* packages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, TypedDict
from uuid import UUID

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class SearchMode(str, Enum):
    DENSE = "dense"
    SPARSE = "sparse"
    HYBRID = "hybrid"


class ElementType(str, Enum):
    TEXT = "text"
    TABLE = "table"
    TITLE = "title"
    LIST = "list"
    IMAGE = "image"  # Phase 2: Unstructured image elements
    HEADER = "header"  # Phase 2: page/section headers
    FOOTER = "footer"  # Phase 2: page footers
    FIGURE_CAPTION = "caption"  # Phase 2: figure/table captions
    PAGE_BREAK = "page_break"  # Phase 2: page boundary markers
    FORMULA = "formula"  # Phase 2: mathematical formulas


# ---------------------------------------------------------------------------
# Primitive/value types
# ---------------------------------------------------------------------------


@dataclass
class SparseVector:
    """Sparse embedding vector (indices + values)."""

    indices: list[int]
    values: list[float]


@dataclass
class BoundingBox:
    """PDF bounding box for element coordinates (Phase 2)."""

    page: int
    x0: float  # left
    y0: float  # top
    x1: float  # right
    y1: float  # bottom


# ---------------------------------------------------------------------------
# Chunk and embedding types
# ---------------------------------------------------------------------------


class ChunkMetadata(TypedDict, total=False):
    """Metadata attached to a document chunk.

    Base fields (always present after extraction), plus optional generic
    filterable fields. Arbitrary additional keys are accepted (stored as JSONB).

    Domain-specific fields (Phase 2 verticals):
      - course_id: str - e-learning course identifier
      - module_id: str - e-learning module/section identifier
      - academic_year: str - academic year (e.g., "2025-2026")
      - document_version: int - source document version (REQ-056)

    These keys are accepted at runtime via JSONB storage. They are
    documented here for type awareness but not enforced by the TypedDict
    (total=False allows arbitrary additional keys at runtime).
    """

    page: int
    position: int
    source_file: str
    content_type: str | None  # MIME type or operator-defined category
    language: str | None  # ISO 639-1 code
    # Domain-specific (Phase 2, optional)
    document_version: int
    course_id: str
    module_id: str
    academic_year: str


@dataclass
class DocumentChunk:
    """A chunk of text extracted from a source document.

    Phase 1: element_type always TEXT, content_format always "text",
    parent_id and coordinates always None, index_version always 1.
    """

    text: str
    element_type: ElementType = ElementType.TEXT
    content_format: str = "text"  # "text" | "html" | "markdown"
    metadata: dict[str, Any] = field(default_factory=dict)
    parent_id: str | None = None  # parent-child hierarchy (Phase 2)
    coordinates: BoundingBox | None = None  # PDF highlighting (Phase 2)
    index_version: int = 1  # zero-downtime reindex (ARCH-045)


@dataclass
class ChunkEmbedding:
    """A chunk with its dense (and optionally sparse) embedding, ready for storage."""

    chunk_id: str  # str, not UUID (ARCH-051 portability)
    text: str
    dense: list[float]
    sparse: SparseVector | None = None  # Phase 2: hybrid search
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Search / retrieval types
# ---------------------------------------------------------------------------


class SearchFilters(TypedDict, total=False):
    """Filters applied to vector search.

    Arbitrary additional keys are accepted at runtime (JSONB is schema-free).
    """

    content_type: str | list[str]  # MIME type or operator-defined category
    language: str | list[str]  # ISO 639-1 code


@dataclass
class QueryEmbedding:
    """Combined embedding for a search query (dense always present, sparse Phase 2)."""

    dense: list[float]
    sparse: SparseVector | None = None


@dataclass
class SearchResult:
    """Single result from VectorStoreProvider.search()."""

    chunk_id: str  # str, not UUID (ARCH-051)
    score: float
    text_snippet: str
    document_id: UUID
    document_version: int = 1  # from SourceDocument.version (REQ-056)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChunkRef:
    """Lightweight chunk reference recorded in QueryTrace."""

    chunk_id: str
    score: float


# ---------------------------------------------------------------------------
# Extraction types
# ---------------------------------------------------------------------------


@dataclass
class ExtractionRequest:
    """Input to DocumentExtractor.extract()."""

    content: bytes  # raw file bytes
    content_type: str  # MIME type from magic bytes detection (ARCH-042)
    filename: str  # original filename
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# LLM types
# ---------------------------------------------------------------------------


@dataclass
class Message:
    """A single message in an LLM conversation."""

    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class CompletionResponse:
    """Response from LLMProvider.complete()."""

    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass
class CompletionChunk:
    """Streaming token chunk from LLMProvider.stream()."""

    content: str
    done: bool = False


@dataclass
class HealthStatus:
    """Returned by provider health_check() methods."""

    status: str  # "healthy" | "degraded" | "unhealthy"
    message: str | None = None
    latency_ms: int | None = None


# ---------------------------------------------------------------------------
# Safeguard types
# ---------------------------------------------------------------------------


@dataclass
class SafeguardContext:
    """Context passed to SafeguardHook methods."""

    namespace: str = "default"
    conversation_id: UUID | None = None
    key_scope: str = "admin"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SafeguardResult:
    """Result from a SafeguardHook method.

    allowed=False blocks the request. filtered_ids removes specific chunks
    (post_retrieval only). modified_content replaces the original text (ARCH-049).
    """

    allowed: bool = True
    reason: str | None = None
    filtered_ids: list[str] | None = None
    modified_content: str | None = None
    annotations: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Query pipeline types
# ---------------------------------------------------------------------------


@dataclass
class QueryRequest:
    """Input to QueryPipeline.execute()."""

    question: str
    namespace: str = "default"
    conversation_id: UUID | None = None
    top_k: int = 5
    search_mode: SearchMode = SearchMode.DENSE
    filters: SearchFilters | None = None
    stream: bool = False


@dataclass
class SourceRef:
    """A source reference included in a QueryResponse."""

    doc_id: UUID
    chunk_id: str  # str, not UUID (ARCH-051)
    score: float
    snippet: str
    citation_id: UUID
    document_version: int = 1  # from SearchResult (REQ-056)


@dataclass
class QueryResponse:
    """Response from QueryPipeline.execute().

    answer is None when context_only=True or no_relevant_context=True.
    no_relevant_context=True when all retrieved chunks score below the
    minimum relevance threshold (ARCH-056, REQ-066).
    """

    response_id: UUID
    answer: str | None
    sources: list[SourceRef]
    conversation_id: UUID | None
    context_only: bool = False
    no_relevant_context: bool = False
    confidence_tier: str | None = None  # Phase 2: HIGH | MEDIUM | LOW


@dataclass
class QueryChunk:
    """SSE streaming chunk from QueryPipeline.execute_stream()."""

    type: str  # "token" | "sources" | "trace" | "error" | "done"
    data: str | dict[str, Any] | list[Any] = field(default_factory=str)


# ---------------------------------------------------------------------------
# Query observability types (ARCH-041)
# ---------------------------------------------------------------------------


@dataclass
class StepTrace:
    """Trace for a single step in the query pipeline."""

    name: str  # embed_query | vector_search | retrieval_filter | build_prompt | llm_call | safeguard
    duration_ms: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class QueryTrace:
    """Full RAG observability trace for a query (ARCH-041)."""

    response_id: UUID
    steps: list[StepTrace]
    total_duration_ms: int
    chunks_retrieved: list[ChunkRef]
    llm_model: str
    prompt_version: str  # SHA-256[:8] of concatenated template sources (ARCH-048)
    created_at: datetime


# ---------------------------------------------------------------------------
# Document and namespace types
# ---------------------------------------------------------------------------


@dataclass
class SourceDocument:
    """Metadata about an ingested source document.

    status and index_version are NOT fields here - they belong to
    ingest_jobs and document_chunks respectively (see ARCH-058).
    """

    id: UUID
    namespace_id: str
    filename: str
    content_hash: str  # SHA-256 of raw file bytes (REQ-034)
    content_type: str  # MIME type, auto-detected via python-magic
    file_size_bytes: int
    created_at: datetime
    updated_at: datetime
    filename_aliases: list[str] = field(default_factory=list)
    chunk_count: int | None = None
    version: int = 1  # document version (REQ-056)
    supersedes_id: UUID | None = None
    deleted_at: datetime | None = None
    deletion_reason: str | None = None


@dataclass
class Namespace:
    """A namespace for multi-tenant document isolation (ARCH-047)."""

    id: str  # "default", "corso-ml-2026"
    display_name: str | None
    created_at: datetime
    updated_at: datetime
    owner_key_id: UUID | None = None
    quota_chunks: int | None = None
    quota_documents: int | None = None
    quota_bytes: int | None = None
    config: dict[str, Any] = field(default_factory=dict)
    retention_days: int | None = None


# ---------------------------------------------------------------------------
# Ingest job type
# ---------------------------------------------------------------------------


@dataclass
class IngestJobStatus:
    """Status of an asynchronous ingest job (ARCH-059, API response type)."""

    id: UUID
    status: str  # "pending" | "processing" | "indexed" | "failed"
    phase: str | None = None  # "extracting" | "chunking" | "embedding"
    chunk_count: int | None = None
    error_code: str | None = None  # ERR-INGEST-xxx on failure
    error_message: str | None = None
    percentage: int | None = None  # progress if determinable (NFR-010)
