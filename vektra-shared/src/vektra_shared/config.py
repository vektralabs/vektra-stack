"""Pydantic settings schemas for all Vektra configuration (ARCH-060).

All VEKTRA_* environment variables are declared here. This is the single
source of truth for operator documentation and startup validation (ARCH-057 step 1).

37 variables total: 35 VEKTRA_* + 2 external (OPENAI_API_KEY, ANTHROPIC_API_KEY).
"""
from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMConfig(BaseSettings):
    """LLM provider configuration (ARCH-043, REQ-059)."""

    model_config = SettingsConfigDict(env_prefix="VEKTRA_LLM_", extra="ignore")

    provider: str = Field(
        ...,
        alias="VEKTRA_LLM_PROVIDER",
        description="LLM model identifier in litellm format (e.g., 'ollama/llama3', 'openai/gpt-4o').",
    )
    api_key: str | None = Field(
        None,
        alias="VEKTRA_LLM_API_KEY",
        description="API key for the LLM provider. Not needed for Ollama.",
    )
    fallback_model: str | None = Field(
        None,
        alias="VEKTRA_LLM_FALLBACK_MODEL",
        description="Fallback model used when primary model times out.",
    )
    fallback_timeout_ms: int = Field(
        30000,
        alias="VEKTRA_LLM_FALLBACK_TIMEOUT_MS",
        description="Timeout in ms before switching to fallback model.",
    )
    context_only_enabled: bool = Field(
        True,
        alias="VEKTRA_LLM_CONTEXT_ONLY_ENABLED",
        description="Return chunks without LLM synthesis when both models fail.",
    )

    model_config = SettingsConfigDict(env_prefix="", extra="ignore", populate_by_name=True)


class EmbeddingConfig(BaseSettings):
    """Embedding provider configuration (ARCH-035, ADR-0013)."""

    model_config = SettingsConfigDict(env_prefix="VEKTRA_", extra="ignore")

    embedding_provider: str = Field(
        "sentence-transformers",
        alias="VEKTRA_EMBEDDING_PROVIDER",
        description="EmbeddingProvider implementation. Phase 2 option: 'tei'.",
    )
    embedding_model: str = Field(
        "all-MiniLM-L6-v2",
        alias="VEKTRA_EMBEDDING_MODEL",
        description="Embedding model name within the selected provider.",
    )
    sparse_embedding_provider: str | None = Field(
        None,
        alias="VEKTRA_SPARSE_EMBEDDING_PROVIDER",
        description="SparseEmbeddingProvider. Phase 1: not registered. Phase 2: 'fastembed-bm25', 'splade'.",
    )
    sparse_embedding_model: str | None = Field(
        None,
        alias="VEKTRA_SPARSE_EMBEDDING_MODEL",
        description="Sparse embedding model name. Phase 2 only.",
    )

    model_config = SettingsConfigDict(env_prefix="", extra="ignore", populate_by_name=True)


class VectorStoreConfig(BaseSettings):
    """Vector store configuration (ARCH-039, ADR-0012)."""

    vector_store_provider: str = Field(
        "pgvector",
        alias="VEKTRA_VECTOR_STORE_PROVIDER",
        description="VectorStoreProvider implementation. Phase 2 option: 'qdrant'.",
    )
    active_index_version: int = Field(
        1,
        alias="VEKTRA_ACTIVE_INDEX_VERSION",
        description="Active index version for search queries. Change after re-embedding for zero-downtime reindex.",
    )

    model_config = SettingsConfigDict(env_prefix="", extra="ignore", populate_by_name=True)


class QueryPipelineConfig(BaseSettings):
    """Query pipeline configuration (ARCH-055, ARCH-056, ADR-0021)."""

    query_pipeline: str = Field(
        "simple",
        alias="VEKTRA_QUERY_PIPELINE",
        description="QueryPipeline implementation: 'simple' (Phase 1), 'advanced' (Phase 2).",
    )
    min_relevance_score: float = Field(
        0.3,
        alias="VEKTRA_MIN_RELEVANCE_SCORE",
        description="Minimum cosine similarity for chunk inclusion (ARCH-056).",
    )
    chunk_dedup_enabled: bool = Field(
        True,
        alias="VEKTRA_CHUNK_DEDUP_ENABLED",
        description="Enable overlap deduplication for adjacent chunks from the same document.",
    )
    response_token_reserve: int = Field(
        1024,
        alias="VEKTRA_RESPONSE_TOKEN_RESERVE",
        description="Tokens reserved for LLM response generation (ARCH-055).",
    )
    context_chunk_ratio: float = Field(
        0.6,
        alias="VEKTRA_CONTEXT_CHUNK_RATIO",
        description="Fraction of context window allocated to retrieved chunks (ARCH-055).",
    )
    prompt_templates_dir: str | None = Field(
        None,
        alias="VEKTRA_PROMPT_TEMPLATES_DIR",
        description="Directory for Jinja2 templates (system.j2, context.j2, conversation.j2). Falls back to built-in defaults.",
    )

    model_config = SettingsConfigDict(env_prefix="", extra="ignore", populate_by_name=True)


class IngestConfig(BaseSettings):
    """Document ingestion configuration (REQ-016, ARCH-037, ARCH-039)."""

    chunking_strategy: str = Field(
        "fixed",
        alias="VEKTRA_CHUNKING_STRATEGY",
        description="ChunkingStrategy implementation: 'fixed' (Phase 1), 'dual' (Phase 2).",
    )
    chunk_size: int = Field(
        1000,
        alias="VEKTRA_CHUNK_SIZE",
        description="Token count per chunk for fixed-size chunking.",
    )
    chunk_overlap: int = Field(
        200,
        alias="VEKTRA_CHUNK_OVERLAP",
        description="Token overlap between adjacent chunks.",
    )
    max_file_size_mb: int = Field(
        50,
        alias="VEKTRA_MAX_FILE_SIZE_MB",
        description="Maximum file size accepted for ingestion (megabytes).",
    )
    document_extractor: str = Field(
        "pdfplumber",
        alias="VEKTRA_DOCUMENT_EXTRACTOR",
        description="DocumentExtractor implementation: 'pdfplumber' (Phase 1), 'unstructured' (Phase 2).",
    )

    model_config = SettingsConfigDict(env_prefix="", extra="ignore", populate_by_name=True)


class SecurityConfig(BaseSettings):
    """Security and operational configuration (NFR-012, ARCH-025, ARCH-031)."""

    admin_bootstrap_key: str | None = Field(
        None,
        alias="VEKTRA_ADMIN_BOOTSTRAP_KEY",
        description="Single-use credential for initial API key creation. Consumed after first use.",
    )
    safeguard_mode: str = Field(
        "passthrough",
        alias="VEKTRA_SAFEGUARD_MODE",
        description="SafeguardHook implementation: 'passthrough' (Phase 1), 'presidio', 'guardrails-ai' (Phase 2).",
    )
    env: str = Field(
        "development",
        alias="VEKTRA_ENV",
        description="Environment mode. In 'production', non-TLS connections are rejected.",
    )
    multi_tenant: bool = Field(
        False,
        alias="VEKTRA_MULTI_TENANT",
        description="Activate PostgreSQL RLS binding. Phase 1: false (application-level filtering).",
    )
    conversation_key: str | None = Field(
        None,
        alias="VEKTRA_CONVERSATION_KEY",
        description="Symmetric encryption key for conversation content. Phase 2 only.",
    )

    model_config = SettingsConfigDict(env_prefix="", extra="ignore", populate_by_name=True)


class ObservabilityConfig(BaseSettings):
    """Observability, retention, and evaluation configuration."""

    max_conversation_turns: int = Field(
        10,
        alias="VEKTRA_MAX_CONVERSATION_TURNS",
        description="Maximum conversation history turns retained per conversation.",
    )
    audit_retention_days: int = Field(
        90,
        alias="VEKTRA_AUDIT_RETENTION_DAYS",
        description="Audit log retention period in days.",
    )
    retention_days: int | None = Field(
        None,
        alias="VEKTRA_RETENTION_DAYS",
        description="Soft-deleted record retention period. Phase 2: arq cleanup job.",
    )
    analytics_retention_days: int | None = Field(
        None,
        alias="VEKTRA_ANALYTICS_RETENTION_DAYS",
        description="QueryTrace storage retention days. Phase 2 only.",
    )
    eval_mode: bool = Field(
        False,
        alias="VEKTRA_EVAL_MODE",
        description="Enable temporary text capture for batch RAG evaluation. Staging only.",
    )

    model_config = SettingsConfigDict(env_prefix="", extra="ignore", populate_by_name=True)


class ServerConfig(BaseSettings):
    """HTTP server configuration."""

    port: int = Field(
        8000,
        alias="VEKTRA_PORT",
        description="HTTP port for the FastAPI application.",
    )
    startup_llm_check: bool = Field(
        True,
        alias="VEKTRA_STARTUP_LLM_CHECK",
        description="Include LLM connectivity check in startup validation (ARCH-057 step 7).",
    )

    model_config = SettingsConfigDict(env_prefix="", extra="ignore", populate_by_name=True)


class ExternalApiKeys(BaseSettings):
    """External LLM provider API keys (not VEKTRA_-prefixed)."""

    openai_api_key: str | None = Field(None, alias="OPENAI_API_KEY")
    anthropic_api_key: str | None = Field(None, alias="ANTHROPIC_API_KEY")

    model_config = SettingsConfigDict(env_prefix="", extra="ignore", populate_by_name=True)


class VektraSettings(BaseSettings):
    """Root settings: aggregates all VEKTRA_* env vars plus external keys.

    All 37 variables (35 VEKTRA_* + 2 external) are represented here.
    Sub-configs provide logical grouping with defaults; this class is the
    single entry point for ARCH-057 step 1 validation.
    """

    # Database
    database_url: str = Field(
        "postgresql+asyncpg://vektra:vektra@postgres:5432/vektra",
        alias="VEKTRA_DATABASE_URL",
        description="AsyncPG connection string for PostgreSQL.",
    )

    # LLM
    llm_provider: str = Field(
        ...,
        alias="VEKTRA_LLM_PROVIDER",
        description="LLM model identifier in litellm format.",
    )
    llm_api_key: str | None = Field(None, alias="VEKTRA_LLM_API_KEY")
    llm_fallback_model: str | None = Field(None, alias="VEKTRA_LLM_FALLBACK_MODEL")
    llm_fallback_timeout_ms: int = Field(30000, alias="VEKTRA_LLM_FALLBACK_TIMEOUT_MS")
    llm_context_only_enabled: bool = Field(True, alias="VEKTRA_LLM_CONTEXT_ONLY_ENABLED")

    # Embedding
    embedding_provider: str = Field("sentence-transformers", alias="VEKTRA_EMBEDDING_PROVIDER")
    embedding_model: str = Field("all-MiniLM-L6-v2", alias="VEKTRA_EMBEDDING_MODEL")
    sparse_embedding_provider: str | None = Field(None, alias="VEKTRA_SPARSE_EMBEDDING_PROVIDER")
    sparse_embedding_model: str | None = Field(None, alias="VEKTRA_SPARSE_EMBEDDING_MODEL")

    # Vector store
    vector_store_provider: str = Field("pgvector", alias="VEKTRA_VECTOR_STORE_PROVIDER")
    active_index_version: int = Field(1, alias="VEKTRA_ACTIVE_INDEX_VERSION")

    # Query pipeline
    query_pipeline: str = Field("simple", alias="VEKTRA_QUERY_PIPELINE")
    min_relevance_score: float = Field(0.3, alias="VEKTRA_MIN_RELEVANCE_SCORE")
    chunk_dedup_enabled: bool = Field(True, alias="VEKTRA_CHUNK_DEDUP_ENABLED")
    response_token_reserve: int = Field(1024, alias="VEKTRA_RESPONSE_TOKEN_RESERVE")
    context_chunk_ratio: float = Field(0.6, alias="VEKTRA_CONTEXT_CHUNK_RATIO")
    prompt_templates_dir: str | None = Field(None, alias="VEKTRA_PROMPT_TEMPLATES_DIR")

    # Ingest
    chunking_strategy: str = Field("fixed", alias="VEKTRA_CHUNKING_STRATEGY")
    chunk_size: int = Field(1000, alias="VEKTRA_CHUNK_SIZE")
    chunk_overlap: int = Field(200, alias="VEKTRA_CHUNK_OVERLAP")
    max_file_size_mb: int = Field(50, alias="VEKTRA_MAX_FILE_SIZE_MB")
    document_extractor: str = Field("pdfplumber", alias="VEKTRA_DOCUMENT_EXTRACTOR")

    # Conversations
    max_conversation_turns: int = Field(10, alias="VEKTRA_MAX_CONVERSATION_TURNS")
    conversation_key: str | None = Field(None, alias="VEKTRA_CONVERSATION_KEY")

    # Security / auth
    admin_bootstrap_key: str | None = Field(None, alias="VEKTRA_ADMIN_BOOTSTRAP_KEY")
    safeguard_mode: str = Field("passthrough", alias="VEKTRA_SAFEGUARD_MODE")
    env: str = Field("development", alias="VEKTRA_ENV")
    multi_tenant: bool = Field(False, alias="VEKTRA_MULTI_TENANT")

    # Server
    port: int = Field(8000, alias="VEKTRA_PORT")
    startup_llm_check: bool = Field(True, alias="VEKTRA_STARTUP_LLM_CHECK")

    # Observability / retention
    audit_retention_days: int = Field(90, alias="VEKTRA_AUDIT_RETENTION_DAYS")
    retention_days: int | None = Field(None, alias="VEKTRA_RETENTION_DAYS")
    analytics_retention_days: int | None = Field(None, alias="VEKTRA_ANALYTICS_RETENTION_DAYS")
    eval_mode: bool = Field(False, alias="VEKTRA_EVAL_MODE")

    # External API keys (no VEKTRA_ prefix)
    openai_api_key: str | None = Field(None, alias="OPENAI_API_KEY")
    anthropic_api_key: str | None = Field(None, alias="ANTHROPIC_API_KEY")

    model_config = SettingsConfigDict(
        env_prefix="",
        extra="ignore",
        populate_by_name=True,
    )

    @field_validator("context_chunk_ratio")
    @classmethod
    def validate_chunk_ratio(cls, v: float) -> float:
        if not 0.0 < v < 1.0:
            raise ValueError(f"context_chunk_ratio must be between 0 and 1, got {v}")
        return v

    @field_validator("min_relevance_score")
    @classmethod
    def validate_relevance_score(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"min_relevance_score must be between 0 and 1, got {v}")
        return v

    def as_llm_config(self) -> LLMConfig:
        """Extract LLM-specific sub-config."""
        return LLMConfig.model_validate({
            "VEKTRA_LLM_PROVIDER": self.llm_provider,
            "VEKTRA_LLM_API_KEY": self.llm_api_key,
            "VEKTRA_LLM_FALLBACK_MODEL": self.llm_fallback_model,
            "VEKTRA_LLM_FALLBACK_TIMEOUT_MS": self.llm_fallback_timeout_ms,
            "VEKTRA_LLM_CONTEXT_ONLY_ENABLED": self.llm_context_only_enabled,
        })
