"""Tests for SentenceTransformersProvider."""
import pytest

from vektra_index.providers.sentence_transformers import SentenceTransformersProvider


class TestSentenceTransformersProvider:
    """Basic provider tests (uses real model - slow but necessary for dimensionality check)."""

    @pytest.fixture(scope="class")
    def provider(self):
        return SentenceTransformersProvider("all-MiniLM-L6-v2")

    def test_dimensions_returns_384(self, provider):
        assert provider.dimensions() == 384

    @pytest.mark.asyncio
    async def test_embed_query_returns_384_floats(self, provider):
        embedding = await provider.embed_query("test query")
        assert len(embedding) == 384
        assert all(isinstance(v, float) for v in embedding)

    @pytest.mark.asyncio
    async def test_embed_documents_returns_batch(self, provider):
        texts = ["first document", "second document", "third document"]
        embeddings = await provider.embed_documents(texts)
        assert len(embeddings) == 3
        for emb in embeddings:
            assert len(emb) == 384

    @pytest.mark.asyncio
    async def test_health_check_returns_healthy(self, provider):
        status = await provider.health_check()
        assert status.status == "healthy"
        assert status.latency_ms is not None

    def test_singleton_model_reuse(self):
        """Two providers with same model_name share the model singleton."""
        from vektra_index.providers.sentence_transformers import _model as module_model
        p1 = SentenceTransformersProvider("all-MiniLM-L6-v2")
        p2 = SentenceTransformersProvider("all-MiniLM-L6-v2")
        # Both should return the same model object
        m1 = p1._model()
        m2 = p2._model()
        assert m1 is m2
