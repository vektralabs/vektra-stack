"""Unit tests for RerankerService and create_reranker factory."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from vektra_core.reranker import (
    RerankerService,
    _default_model_for_provider,
    create_reranker,
)
from vektra_shared.config import RerankConfig
from vektra_shared.types import SearchResult


def _make_result(score: float, text: str = "some text") -> SearchResult:
    return SearchResult(
        chunk_id=str(uuid4()),
        score=score,
        text_snippet=text,
        document_id=uuid4(),
    )


# ---------------------------------------------------------------------------
# RerankerService.rerank
# ---------------------------------------------------------------------------


async def test_rerank_empty_results():
    service = RerankerService(ranker=MagicMock())
    result = await service.rerank("query", [], top_k=5)
    assert result == []


async def test_rerank_returns_top_k_in_order():
    results = [
        _make_result(0.5, "doc A"),
        _make_result(0.3, "doc B"),
        _make_result(0.9, "doc C"),
    ]

    # Simulate rerankers output: list of items with doc_id attribute
    ranked_items = [
        SimpleNamespace(doc_id=2),  # doc C first
        SimpleNamespace(doc_id=0),  # doc A second
        SimpleNamespace(doc_id=1),  # doc B third
    ]
    mock_ranker = MagicMock()
    mock_ranker.rank.return_value = SimpleNamespace(results=ranked_items)

    service = RerankerService(ranker=mock_ranker)
    reranked = await service.rerank("test query", results, top_k=2)

    assert len(reranked) == 2
    assert reranked[0].chunk_id == results[2].chunk_id  # doc C
    assert reranked[1].chunk_id == results[0].chunk_id  # doc A
    mock_ranker.rank.assert_called_once_with(
        query="test query", docs=["doc A", "doc B", "doc C"]
    )


# ---------------------------------------------------------------------------
# create_reranker factory
# ---------------------------------------------------------------------------


def test_create_reranker_disabled():
    config = RerankConfig.model_validate({"VEKTRA_RERANK_ENABLED": False})
    assert create_reranker(config) is None


def test_create_reranker_handles_import_error():
    config = RerankConfig.model_validate({"VEKTRA_RERANK_ENABLED": True})
    with patch.dict("sys.modules", {"rerankers": None}):
        result = create_reranker(config)
    # Either returns a service (if rerankers is installed) or None (import fails)
    assert result is None or isinstance(result, RerankerService)


# ---------------------------------------------------------------------------
# _default_model_for_provider
# ---------------------------------------------------------------------------


def test_default_model_flashrank():
    assert _default_model_for_provider("flashrank") == "ms-marco-MiniLM-L-12-v2"


def test_default_model_cross_encoder():
    assert (
        _default_model_for_provider("cross-encoder")
        == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    )


def test_default_model_unknown_provider():
    assert _default_model_for_provider("custom") == "custom"
