"""Unit tests for vektra_shared.config (Pydantic settings schemas)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vektra_shared.config import (
    IngestConfig,
    LLMConfig,
    QueryPipelineConfig,
    RerankConfig,
    RewriteConfig,
    VektraSettings,
    WebhookConfig,
)


class TestLLMConfig:
    def test_defaults(self) -> None:
        cfg = LLMConfig(VEKTRA_LLM_PROVIDER="ollama/llama3")
        assert cfg.provider == "ollama/llama3"
        assert cfg.api_key is None
        assert cfg.fallback_model is None
        assert cfg.fallback_timeout_ms == 60000
        assert cfg.context_only_enabled is True

    def test_all_fields(self) -> None:
        cfg = LLMConfig(
            VEKTRA_LLM_PROVIDER="openai/gpt-4o",
            VEKTRA_LLM_API_KEY="sk-test",
            VEKTRA_LLM_FALLBACK_MODEL="ollama/llama3",
            VEKTRA_LLM_FALLBACK_TIMEOUT_MS=5000,
            VEKTRA_LLM_CONTEXT_ONLY_ENABLED=False,
        )
        assert cfg.provider == "openai/gpt-4o"
        assert cfg.api_key == "sk-test"
        assert cfg.fallback_model == "ollama/llama3"
        assert cfg.fallback_timeout_ms == 5000
        assert cfg.context_only_enabled is False


class TestQueryPipelineConfig:
    def test_defaults(self) -> None:
        cfg = QueryPipelineConfig(
            VEKTRA_QUERY_PIPELINE="simple",
            VEKTRA_MIN_RELEVANCE_SCORE=0.3,
            VEKTRA_CONTEXT_CHUNK_RATIO=0.6,
            VEKTRA_CHUNK_DEDUP_ENABLED=True,
            VEKTRA_RESPONSE_TOKEN_RESERVE=1024,
        )
        assert cfg.query_pipeline == "simple"
        assert cfg.min_relevance_score == 0.3
        assert cfg.context_chunk_ratio == 0.6
        assert cfg.chunk_dedup_enabled is True
        assert cfg.response_token_reserve == 1024
        assert cfg.prompt_templates_dir is None
        assert cfg.grounding_mode == "strict"

    def test_grounding_mode_hybrid(self) -> None:
        cfg = QueryPipelineConfig(VEKTRA_PROMPT_GROUNDING_MODE="hybrid")
        assert cfg.grounding_mode == "hybrid"

    def test_grounding_mode_invalid(self) -> None:
        with pytest.raises(ValidationError, match="grounding_mode"):
            QueryPipelineConfig(VEKTRA_PROMPT_GROUNDING_MODE="invalid")

    def test_eval_mode_default_false(self) -> None:
        cfg = QueryPipelineConfig()
        assert cfg.eval_mode is False

    def test_debug_log_queries_default_false(self) -> None:
        cfg = QueryPipelineConfig()
        assert cfg.debug_log_queries is False

    def test_eval_mode_from_env(self) -> None:
        cfg = QueryPipelineConfig(VEKTRA_EVAL_MODE=True)
        assert cfg.eval_mode is True

    def test_rescue_defaults_off(self) -> None:
        cfg = QueryPipelineConfig()
        assert cfg.retrieval_rescue_top_k == 0
        assert cfg.retrieval_rescue_floor == 0.02

    def test_rescue_from_env(self) -> None:
        cfg = QueryPipelineConfig(
            VEKTRA_RETRIEVAL_RESCUE_TOP_K=3,
            VEKTRA_RETRIEVAL_RESCUE_FLOOR=0.005,
        )
        assert cfg.retrieval_rescue_top_k == 3
        assert cfg.retrieval_rescue_floor == 0.005

    def test_rescue_top_k_negative_invalid(self) -> None:
        with pytest.raises(ValidationError, match="VEKTRA_RETRIEVAL_RESCUE_TOP_K"):
            QueryPipelineConfig(VEKTRA_RETRIEVAL_RESCUE_TOP_K=-1)

    def test_rescue_floor_above_one_invalid(self) -> None:
        with pytest.raises(ValidationError, match="VEKTRA_RETRIEVAL_RESCUE_FLOOR"):
            QueryPipelineConfig(VEKTRA_RETRIEVAL_RESCUE_FLOOR=1.5)

    def test_debug_log_queries_from_env(self) -> None:
        cfg = QueryPipelineConfig(VEKTRA_DEBUG_LOG_QUERIES=True)
        assert cfg.debug_log_queries is True


class TestRewriteConfig:
    def test_defaults(self) -> None:
        cfg = RewriteConfig()
        assert cfg.enabled is True
        assert cfg.model is None

    def test_env_var_override(self) -> None:
        cfg = RewriteConfig(
            VEKTRA_QUERY_REWRITE_ENABLED=False,
            VEKTRA_QUERY_REWRITE_MODEL="ollama/llama3",
        )
        assert cfg.enabled is False
        assert cfg.model == "ollama/llama3"


class TestRerankConfig:
    def test_defaults(self) -> None:
        cfg = RerankConfig()
        assert cfg.enabled is True
        assert cfg.provider == "cross-encoder"
        assert cfg.model == "BAAI/bge-reranker-v2-m3"
        assert cfg.top_k == 5

    def test_env_var_override(self) -> None:
        cfg = RerankConfig(
            VEKTRA_RERANK_ENABLED=False,
            VEKTRA_RERANK_PROVIDER="cohere",
            VEKTRA_RERANK_MODEL="rerank-english-v3.0",
            VEKTRA_RERANK_TOP_K=10,
        )
        assert cfg.enabled is False
        assert cfg.provider == "cohere"
        assert cfg.model == "rerank-english-v3.0"
        assert cfg.top_k == 10


class TestWebhookConfig:
    def test_defaults(self) -> None:
        cfg = WebhookConfig()
        assert cfg.url is None
        assert cfg.secret is None
        assert cfg.timeout_seconds == 5.0

    def test_all_fields_set(self) -> None:
        cfg = WebhookConfig(
            VEKTRA_WEBHOOK_URL="https://example.com/hook",
            VEKTRA_WEBHOOK_SECRET="my-secret",
            VEKTRA_WEBHOOK_TIMEOUT=10.0,
        )
        assert cfg.url == "https://example.com/hook"
        assert cfg.secret == "my-secret"
        assert cfg.timeout_seconds == 10.0


class TestIngestConfigDualStrategy:
    def test_fixed_strategy_default_passes(self) -> None:
        cfg = IngestConfig()
        assert cfg.chunking_strategy == "fixed"
        assert cfg.parent_child_levels == 0
        assert cfg.table_split is False

    def test_fixed_strategy_with_zero_levels_passes(self) -> None:
        cfg = IngestConfig(
            VEKTRA_CHUNKING_STRATEGY="fixed",
            VEKTRA_PARENT_CHILD_LEVELS=0,
        )
        assert cfg.parent_child_levels == 0

    def test_dual_strategy_with_valid_levels_passes(self) -> None:
        cfg = IngestConfig(
            VEKTRA_CHUNKING_STRATEGY="dual",
            VEKTRA_PARENT_CHILD_LEVELS=2,
        )
        assert cfg.chunking_strategy == "dual"
        assert cfg.parent_child_levels == 2

    def test_dual_strategy_with_zero_levels_raises(self) -> None:
        with pytest.raises(ValidationError, match="parent_child_levels"):
            IngestConfig(
                VEKTRA_CHUNKING_STRATEGY="dual",
                VEKTRA_PARENT_CHILD_LEVELS=0,
            )


class TestQueryPipelineConfigNested:
    def test_rewrite_and_rerank_defaults(self) -> None:
        cfg = QueryPipelineConfig()
        assert cfg.rewrite.enabled is True
        assert cfg.rewrite.model is None
        assert cfg.rerank.enabled is True
        assert cfg.rerank.provider == "cross-encoder"
        assert cfg.rerank.top_k == 5


class TestVektraSettings:
    """Tests for the root settings aggregator and its validators."""

    def _make(self, **overrides: object) -> VektraSettings:
        defaults = {"VEKTRA_LLM_PROVIDER": "ollama/llama3"}
        defaults.update(overrides)
        return VektraSettings(**defaults)

    def test_defaults_with_required_only(self) -> None:
        s = self._make()
        assert s.llm_provider == "ollama/llama3"
        assert s.embedding_model == "paraphrase-multilingual-MiniLM-L12-v2"
        assert s.port == 8000

    def test_context_chunk_ratio_valid(self) -> None:
        s = self._make(VEKTRA_CONTEXT_CHUNK_RATIO=0.5)
        assert s.context_chunk_ratio == 0.5

    def test_context_chunk_ratio_zero_invalid(self) -> None:
        with pytest.raises(ValidationError, match="context_chunk_ratio"):
            self._make(VEKTRA_CONTEXT_CHUNK_RATIO=0.0)

    def test_context_chunk_ratio_one_invalid(self) -> None:
        with pytest.raises(ValidationError, match="context_chunk_ratio"):
            self._make(VEKTRA_CONTEXT_CHUNK_RATIO=1.0)

    def test_min_relevance_score_valid_zero(self) -> None:
        s = self._make(VEKTRA_MIN_RELEVANCE_SCORE=0.0)
        assert s.min_relevance_score == 0.0

    def test_min_relevance_score_valid_one(self) -> None:
        s = self._make(VEKTRA_MIN_RELEVANCE_SCORE=1.0)
        assert s.min_relevance_score == 1.0

    def test_min_relevance_score_negative_invalid(self) -> None:
        with pytest.raises(ValidationError, match="min_relevance_score"):
            self._make(VEKTRA_MIN_RELEVANCE_SCORE=-0.1)

    def test_min_relevance_score_above_one_invalid(self) -> None:
        with pytest.raises(ValidationError, match="min_relevance_score"):
            self._make(VEKTRA_MIN_RELEVANCE_SCORE=1.1)

    def test_grounding_mode_invalid_at_settings_level(self) -> None:
        with pytest.raises(ValidationError, match="prompt_grounding_mode"):
            self._make(VEKTRA_PROMPT_GROUNDING_MODE="invalid")

    def test_as_llm_config(self) -> None:
        s = self._make(
            VEKTRA_LLM_API_KEY="sk-test",
            VEKTRA_LLM_FALLBACK_MODEL="ollama/llama3",
        )
        llm = s.as_llm_config()
        assert isinstance(llm, LLMConfig)
        assert llm.provider == "ollama/llama3"
        assert llm.api_key == "sk-test"
        assert llm.fallback_model == "ollama/llama3"
