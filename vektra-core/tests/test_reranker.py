"""Unit tests for RerankerService and create_reranker factory."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from vektra_core.reranker import (
    RerankerService,
    _default_model_for_provider,
    _sigmoid,
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

    # FlashRank-style scores (already 0-1)
    ranked_items = [
        SimpleNamespace(doc_id=2, score=0.95),  # doc C first
        SimpleNamespace(doc_id=0, score=0.80),  # doc A second
        SimpleNamespace(doc_id=1, score=0.10),  # doc B third
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


async def test_rerank_propagates_flashrank_scores():
    """FlashRank scores (0-1) are propagated as-is (BUG-015)."""
    results = [
        _make_result(0.5, "doc A"),
        _make_result(0.3, "doc B"),
    ]

    ranked_items = [
        SimpleNamespace(doc_id=1, score=0.85),
        SimpleNamespace(doc_id=0, score=0.40),
    ]
    mock_ranker = MagicMock()
    mock_ranker.rank.return_value = SimpleNamespace(results=ranked_items)

    service = RerankerService(ranker=mock_ranker)
    reranked = await service.rerank("query", results, top_k=2)

    # Reranker scores replace vector scores
    assert reranked[0].score == 0.85
    assert reranked[1].score == 0.40
    # Original vector scores preserved
    assert reranked[0].original_score == 0.3
    assert reranked[1].original_score == 0.5


async def test_rerank_normalizes_cross_encoder_logits():
    """Cross-encoder logits (can be negative / > 1) are sigmoid-normalized (BUG-015)."""
    results = [
        _make_result(0.6, "doc A"),
        _make_result(0.4, "doc B"),
    ]

    ranked_items = [
        SimpleNamespace(doc_id=0, score=3.5),  # positive logit
        SimpleNamespace(doc_id=1, score=-2.0),  # negative logit
    ]
    mock_ranker = MagicMock()
    mock_ranker.rank.return_value = SimpleNamespace(results=ranked_items)

    service = RerankerService(ranker=mock_ranker)
    reranked = await service.rerank("query", results, top_k=2)

    # Scores normalized via sigmoid
    assert reranked[0].score == _sigmoid(3.5)
    assert reranked[1].score == _sigmoid(-2.0)
    # Normalized scores are in (0, 1)
    assert 0.0 < reranked[0].score < 1.0
    assert 0.0 < reranked[1].score < 1.0
    # Original scores preserved
    assert reranked[0].original_score == 0.6
    assert reranked[1].original_score == 0.4


async def test_rerank_preserves_metadata():
    """Reranked results keep all original fields except score."""
    doc_id = uuid4()
    results = [
        SearchResult(
            chunk_id="chunk-1",
            score=0.7,
            text_snippet="hello",
            document_id=doc_id,
            document_version=3,
            metadata={"page": 5},
        ),
    ]
    ranked_items = [SimpleNamespace(doc_id=0, score=0.99)]
    mock_ranker = MagicMock()
    mock_ranker.rank.return_value = SimpleNamespace(results=ranked_items)

    service = RerankerService(ranker=mock_ranker)
    reranked = await service.rerank("q", results, top_k=1)

    assert reranked[0].chunk_id == "chunk-1"
    assert reranked[0].document_id == doc_id
    assert reranked[0].document_version == 3
    assert reranked[0].metadata == {"page": 5}
    assert reranked[0].score == 0.99
    assert reranked[0].original_score == 0.7


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
    assert result is None


# ---------------------------------------------------------------------------
# _default_model_for_provider
# ---------------------------------------------------------------------------


def test_default_model_flashrank():
    assert _default_model_for_provider("flashrank") == "ms-marco-MiniLM-L-12-v2"


def test_default_model_cross_encoder():
    assert _default_model_for_provider("cross-encoder") == "BAAI/bge-reranker-v2-m3"


def test_default_model_unknown_provider():
    assert _default_model_for_provider("custom") == "custom"


# ---------------------------------------------------------------------------
# _sigmoid
# ---------------------------------------------------------------------------


def test_sigmoid_zero():
    assert _sigmoid(0.0) == 0.5


def test_sigmoid_large_positive():
    assert _sigmoid(10.0) > 0.999


def test_sigmoid_large_negative():
    assert _sigmoid(-10.0) < 0.001


def test_sigmoid_extreme_values_no_overflow():
    """Numerically stable sigmoid must not overflow on extreme logits."""
    assert _sigmoid(1000.0) == 1.0
    assert _sigmoid(-1000.0) == 0.0
