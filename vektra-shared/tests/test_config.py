"""Unit tests for vektra_shared.config (Pydantic settings schemas)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vektra_shared.config import LLMConfig, QueryPipelineConfig, VektraSettings


class TestLLMConfig:
    def test_defaults(self) -> None:
        cfg = LLMConfig(VEKTRA_LLM_PROVIDER="ollama/llama3")
        assert cfg.provider == "ollama/llama3"
        assert cfg.api_key is None
        assert cfg.fallback_model is None
        assert cfg.fallback_timeout_ms == 30000
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
        cfg = QueryPipelineConfig()
        assert cfg.query_pipeline == "simple"
        assert cfg.min_relevance_score == 0.3
        assert cfg.context_chunk_ratio == 0.6
        assert cfg.chunk_dedup_enabled is True
        assert cfg.response_token_reserve == 1024
        assert cfg.prompt_templates_dir is None


class TestVektraSettings:
    """Tests for the root settings aggregator and its validators."""

    def _make(self, **overrides: object) -> VektraSettings:
        defaults = {"VEKTRA_LLM_PROVIDER": "ollama/llama3"}
        defaults.update(overrides)
        return VektraSettings(**defaults)

    def test_defaults_with_required_only(self) -> None:
        s = self._make()
        assert s.llm_provider == "ollama/llama3"
        assert s.embedding_model == "all-MiniLM-L6-v2"
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
