"""Unit tests for FastEmbedBM25Provider (ARCH-053)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from vektra_shared.types import SparseVector


class FakeSparseEmbedding:
    """Mimics fastembed SparseEmbedding with numpy arrays."""

    def __init__(self, indices: list[int], values: list[float]) -> None:
        self.indices = np.array(indices, dtype=np.int32)
        self.values = np.array(values, dtype=np.float32)


class TestFastEmbedBM25Provider:
    """Tests with mocked fastembed model."""

    def _make_mock_model(self) -> MagicMock:
        model = MagicMock()
        model.embed.return_value = iter([
            FakeSparseEmbedding([0, 5, 12], [0.5, 0.8, 0.3]),
            FakeSparseEmbedding([1, 7], [0.6, 0.4]),
        ])
        model.query_embed.return_value = iter([
            FakeSparseEmbedding([2, 9], [0.7, 0.9]),
        ])
        return model

    @pytest.mark.asyncio
    async def test_embed_documents_returns_sparse_vectors(self):
        mock_model = self._make_mock_model()
        with patch(
            "vektra_index.providers.fastembed_bm25._get_sparse_model",
            return_value=mock_model,
        ), patch(
            "vektra_index.providers.fastembed_bm25._sparse_model",
            mock_model,
        ):
            from vektra_index.providers.fastembed_bm25 import FastEmbedBM25Provider

            # Bypass __init__ eager loading
            provider = object.__new__(FastEmbedBM25Provider)
            provider._model_name = "Qdrant/bm25"

            result = await provider.embed_documents(["hello world", "test doc"])

        assert len(result) == 2
        assert isinstance(result[0], SparseVector)
        assert result[0].indices == [0, 5, 12]
        assert result[0].values == pytest.approx([0.5, 0.8, 0.3], abs=1e-5)
        assert isinstance(result[1], SparseVector)
        assert result[1].indices == [1, 7]

    @pytest.mark.asyncio
    async def test_embed_query_returns_sparse_vector(self):
        mock_model = self._make_mock_model()
        with patch(
            "vektra_index.providers.fastembed_bm25._get_sparse_model",
            return_value=mock_model,
        ), patch(
            "vektra_index.providers.fastembed_bm25._sparse_model",
            mock_model,
        ):
            from vektra_index.providers.fastembed_bm25 import FastEmbedBM25Provider

            provider = object.__new__(FastEmbedBM25Provider)
            provider._model_name = "Qdrant/bm25"

            result = await provider.embed_query("test query")

        assert isinstance(result, SparseVector)
        assert result.indices == [2, 9]
        assert result.values == pytest.approx([0.7, 0.9], abs=1e-5)

    @pytest.mark.asyncio
    async def test_vocab_size_returns_none(self):
        mock_model = self._make_mock_model()
        with patch(
            "vektra_index.providers.fastembed_bm25._get_sparse_model",
            return_value=mock_model,
        ), patch(
            "vektra_index.providers.fastembed_bm25._sparse_model",
            mock_model,
        ):
            from vektra_index.providers.fastembed_bm25 import FastEmbedBM25Provider

            provider = object.__new__(FastEmbedBM25Provider)
            provider._model_name = "Qdrant/bm25"

            assert provider.vocab_size() is None


class TestFastEmbedImportGuard:
    """Test that missing fastembed raises a clear error."""

    def test_import_error_when_fastembed_missing(self):
        import vektra_index.providers.fastembed_bm25 as mod

        # Reset singleton
        original_model = mod._sparse_model
        original_name = mod._sparse_model_name
        mod._sparse_model = None
        mod._sparse_model_name = None

        try:
            with patch.dict("sys.modules", {"fastembed": None}):
                with pytest.raises(ImportError, match="fastembed is required"):
                    mod._get_sparse_model("Qdrant/bm25")
        finally:
            mod._sparse_model = original_model
            mod._sparse_model_name = original_name
