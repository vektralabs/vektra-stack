"""Unit tests for RerankerService, TEIRerankerService and create_reranker."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from vektra_core.reranker import (
    RerankerService,
    RerankResult,
    TEIRerankerService,
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
    assert isinstance(result, RerankResult)
    assert result.top_k == []
    assert result.all_scores == []


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
    result = await service.rerank("test query", results, top_k=2)

    assert len(result.top_k) == 2
    assert result.top_k[0].chunk_id == results[2].chunk_id  # doc C
    assert result.top_k[1].chunk_id == results[0].chunk_id  # doc A
    # all_scores includes ALL 3 candidates, not just top_k
    assert len(result.all_scores) == 3
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
    result = await service.rerank("query", results, top_k=2)

    # Reranker scores replace vector scores
    assert result.top_k[0].score == 0.85
    assert result.top_k[1].score == 0.40
    # Original vector scores preserved
    assert result.top_k[0].original_score == 0.3
    assert result.top_k[1].original_score == 0.5
    # all_scores tracks both candidates
    assert len(result.all_scores) == 2


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
    result = await service.rerank("query", results, top_k=2)

    # Scores normalized via sigmoid
    assert result.top_k[0].score == _sigmoid(3.5)
    assert result.top_k[1].score == _sigmoid(-2.0)
    # Normalized scores are in (0, 1)
    assert 0.0 < result.top_k[0].score < 1.0
    assert 0.0 < result.top_k[1].score < 1.0
    # Original scores preserved
    assert result.top_k[0].original_score == 0.6
    assert result.top_k[1].original_score == 0.4
    # all_scores contains sigmoid-normalized values (rounded)
    assert len(result.all_scores) == 2
    assert result.all_scores[0][1] == round(_sigmoid(3.5), 4)
    assert result.all_scores[1][1] == round(_sigmoid(-2.0), 4)


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
    result = await service.rerank("q", results, top_k=1)

    assert result.top_k[0].chunk_id == "chunk-1"
    assert result.top_k[0].document_id == doc_id
    assert result.top_k[0].document_version == 3
    assert result.top_k[0].metadata == {"page": 5}
    assert result.top_k[0].score == 0.99
    assert result.top_k[0].original_score == 0.7


async def test_all_scores_includes_items_beyond_top_k():
    """all_scores contains entries for ALL candidates, not just top_k (DEBT-014)."""
    results = [_make_result(0.5 + i * 0.05, f"doc {i}") for i in range(5)]

    ranked_items = [
        SimpleNamespace(doc_id=4, score=0.95),
        SimpleNamespace(doc_id=3, score=0.80),
        SimpleNamespace(doc_id=2, score=0.60),
        SimpleNamespace(doc_id=1, score=0.30),
        SimpleNamespace(doc_id=0, score=0.10),
    ]
    mock_ranker = MagicMock()
    mock_ranker.rank.return_value = SimpleNamespace(results=ranked_items)

    service = RerankerService(ranker=mock_ranker)
    result = await service.rerank("query", results, top_k=2)

    # top_k has only 2 results
    assert len(result.top_k) == 2
    assert result.top_k[0].chunk_id == results[4].chunk_id
    assert result.top_k[1].chunk_id == results[3].chunk_id

    # all_scores has ALL 5 candidates
    assert len(result.all_scores) == 5
    # Scores are in descending order (reranker order)
    scores = [s[1] for s in result.all_scores]
    assert scores == sorted(scores, reverse=True)
    # Each entry is (chunk_id, reranker_score, original_score)
    for chunk_id, reranker_score, original_score in result.all_scores:
        assert isinstance(chunk_id, str)
        assert 0.0 <= reranker_score <= 1.0
        assert 0.0 <= original_score <= 1.0


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


# ---------------------------------------------------------------------------
# TEIRerankerService (FEAT-024)
# ---------------------------------------------------------------------------


def _tei_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://tei.test"
    )


async def test_tei_rerank_empty_results():
    service = TEIRerankerService(url="http://tei.test", _client=_tei_client(None))
    result = await service.rerank("query", [], top_k=5)
    assert result.top_k == []
    assert result.all_scores == []


async def test_tei_rerank_orders_and_normalizes():
    results = [
        _make_result(0.5, "doc A"),
        _make_result(0.3, "doc B"),
        _make_result(0.9, "doc C"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/rerank"
        assert body["query"] == "test query"
        assert body["texts"] == ["doc A", "doc B", "doc C"]
        assert body["raw_scores"] is False
        return httpx.Response(
            200,
            json=[
                {"index": 2, "score": 0.95},
                {"index": 0, "score": 0.80},
                {"index": 1, "score": 0.10},
            ],
        )

    service = TEIRerankerService(url="http://tei.test", _client=_tei_client(handler))
    result = await service.rerank("test query", results, top_k=2)

    assert len(result.top_k) == 2
    assert result.top_k[0].chunk_id == results[2].chunk_id
    assert result.top_k[0].score == 0.95
    assert result.top_k[0].original_score == 0.9  # BUG-015 preserved
    assert result.top_k[1].chunk_id == results[0].chunk_id
    assert len(result.all_scores) == 3


async def test_tei_rerank_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    service = TEIRerankerService(url="http://tei.test", _client=_tei_client(handler))
    with pytest.raises(httpx.HTTPStatusError):
        await service.rerank("q", [_make_result(0.5)], top_k=2)


def test_create_reranker_tei_provider():
    config = RerankConfig(
        VEKTRA_RERANK_ENABLED=True,
        VEKTRA_RERANK_PROVIDER="tei",
        VEKTRA_RERANK_TEI_URL="http://tei.test",
    )
    reranker = create_reranker(config)
    assert isinstance(reranker, TEIRerankerService)


def test_create_reranker_tei_invalid_url_returns_none():
    """A malformed TEI URL degrades to None instead of aborting startup."""
    config = RerankConfig(
        VEKTRA_RERANK_ENABLED=True,
        VEKTRA_RERANK_PROVIDER="tei",
        VEKTRA_RERANK_TEI_URL="http://[invalid",
    )
    assert create_reranker(config) is None
