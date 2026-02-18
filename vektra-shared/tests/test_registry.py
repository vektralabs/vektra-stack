"""Tests for ProviderRegistry (ARCH-039)."""
import pytest

from vektra_shared.registry import ProviderRegistry


class TestProviderRegistry:
    def test_register_and_get(self):
        registry = ProviderRegistry()
        sentinel = object()
        registry.register("embedding", "sentence-transformers", sentinel)
        assert registry.get("embedding", "sentence-transformers") is sentinel

    def test_get_unknown_name_raises_with_available(self):
        registry = ProviderRegistry()
        registry.register("embedding", "sentence-transformers", object())
        with pytest.raises(ValueError) as exc_info:
            registry.get("embedding", "unknown-provider")
        assert "sentence-transformers" in str(exc_info.value)
        assert "unknown-provider" in str(exc_info.value)

    def test_get_unknown_category_raises(self):
        registry = ProviderRegistry()
        with pytest.raises(ValueError) as exc_info:
            registry.get("nonexistent_category", "anything")
        assert "nonexistent_category" in str(exc_info.value)

    def test_list_registered(self):
        registry = ProviderRegistry()
        registry.register("embedding", "a", object())
        registry.register("embedding", "b", object())
        names = registry.list("embedding")
        assert sorted(names) == ["a", "b"]

    def test_list_unknown_category_returns_empty(self):
        registry = ProviderRegistry()
        assert registry.list("nonexistent") == []

    def test_overwrite_registration(self):
        registry = ProviderRegistry()
        first = object()
        second = object()
        registry.register("llm", "litellm", first)
        registry.register("llm", "litellm", second)
        assert registry.get("llm", "litellm") is second

    def test_has_returns_true_when_registered(self):
        registry = ProviderRegistry()
        registry.register("vector_store", "pgvector", object())
        assert registry.has("vector_store", "pgvector") is True

    def test_has_returns_false_when_not_registered(self):
        registry = ProviderRegistry()
        assert registry.has("vector_store", "qdrant") is False

    def test_multiple_categories_independent(self):
        registry = ProviderRegistry()
        a = object()
        b = object()
        registry.register("embedding", "model", a)
        registry.register("llm", "model", b)
        assert registry.get("embedding", "model") is a
        assert registry.get("llm", "model") is b
