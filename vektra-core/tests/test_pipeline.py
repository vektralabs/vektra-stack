"""Unit tests for SimpleQueryPipeline (ADR-0014, ARCH-036, ARCH-056)."""

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from vektra_core.conversation import ConversationStore
from vektra_core.pipeline import (
    SimpleQueryPipeline,
    _apply_retrieval_filter,
    _token_overlap_ratio,
)
from vektra_core.templates import TemplateRenderer
from vektra_shared.config import LLMConfig, QueryPipelineConfig
from vektra_shared.types import (
    CompletionResponse,
    QueryRequest,
    SafeguardResult,
    SearchResult,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_llm_config(**overrides) -> LLMConfig:
    defaults = {
        "VEKTRA_LLM_PROVIDER": "ollama/llama3",
        "VEKTRA_LLM_FALLBACK_TIMEOUT_MS": 5000,
    }
    defaults.update(overrides)
    return LLMConfig.model_validate(defaults)


def _make_pipeline_config(**overrides) -> QueryPipelineConfig:
    defaults = {
        "VEKTRA_MIN_RELEVANCE_SCORE": 0.3,
        "VEKTRA_CHUNK_DEDUP_ENABLED": True,
        "VEKTRA_RESPONSE_TOKEN_RESERVE": 512,
        "VEKTRA_CONTEXT_CHUNK_RATIO": 0.6,
    }
    defaults.update(overrides)
    return QueryPipelineConfig.model_validate(defaults)


def _make_search_result(
    score: float, text: str = "some text", doc_id: UUID | None = None
) -> SearchResult:
    return SearchResult(
        chunk_id=str(uuid4()),
        score=score,
        text_snippet=text,
        document_id=doc_id or uuid4(),
        document_version=1,
    )


def _make_pipeline(
    *,
    embedding: object | None = None,
    vector_store: object | None = None,
    llm: object | None = None,
    safeguard: object | None = None,
    llm_config: LLMConfig | None = None,
    pipeline_config: QueryPipelineConfig | None = None,
) -> SimpleQueryPipeline:
    if embedding is None:
        embedding = AsyncMock()
        embedding.embed_query = AsyncMock(return_value=[0.1] * 4)

    if vector_store is None:
        vector_store = AsyncMock()
        vector_store.search = AsyncMock(return_value=[])

    if llm is None:
        llm = MagicMock()
        completion = CompletionResponse(
            content="The answer.",
            model="ollama/llama3",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
        )
        llm.complete = AsyncMock(return_value=completion)
        llm.count_tokens = MagicMock(return_value=10)

    if safeguard is None:
        safeguard = AsyncMock()
        safeguard.pre_response = AsyncMock(return_value=SafeguardResult(allowed=True))
        safeguard.pre_query = AsyncMock(return_value=SafeguardResult(allowed=True))

    return SimpleQueryPipeline(
        embedding=embedding,
        vector_store=vector_store,
        llm=llm,
        llm_config=llm_config or _make_llm_config(),
        safeguard=safeguard,
        conversation_store=ConversationStore(max_turns=10),
        renderer=TemplateRenderer(),
        pipeline_config=pipeline_config or _make_pipeline_config(),
    )


# ---------------------------------------------------------------------------
# Retrieval filter unit tests
# ---------------------------------------------------------------------------


def test_token_overlap_identical_texts():
    assert _token_overlap_ratio("hello world", "hello world") == 1.0


def test_token_overlap_no_overlap():
    ratio = _token_overlap_ratio("hello world", "foo bar baz")
    assert ratio == 0.0


def test_token_overlap_partial():
    ratio = _token_overlap_ratio("hello world foo", "hello world bar")
    # intersection={hello, world}, min_size=3 → 2/3
    assert abs(ratio - 2 / 3) < 0.01


def test_retrieval_filter_removes_low_score():
    results = [
        _make_search_result(0.5, "good chunk"),
        _make_search_result(0.2, "bad chunk"),
        _make_search_result(0.8, "great chunk"),
    ]
    filtered = _apply_retrieval_filter(results, min_score=0.3, dedup_enabled=False)
    scores = [r.score for r in filtered]
    assert all(s >= 0.3 for s in scores)
    assert len(filtered) == 2


def test_retrieval_filter_dedup_removes_near_duplicate():
    text_a = "the quick brown fox jumps over the lazy dog and more words here"
    text_b = "the quick brown fox jumps over the lazy dog and more words there"  # >80% overlap
    results = [
        _make_search_result(0.9, text_a),
        _make_search_result(0.7, text_b),
    ]
    filtered = _apply_retrieval_filter(results, min_score=0.3, dedup_enabled=True)
    assert len(filtered) == 1
    assert filtered[0].score == 0.9  # higher-scoring one kept


def test_retrieval_filter_no_dedup_keeps_all():
    text_a = "the quick brown fox"
    text_b = "the quick brown fox"  # identical
    results = [
        _make_search_result(0.9, text_a),
        _make_search_result(0.7, text_b),
    ]
    filtered = _apply_retrieval_filter(results, min_score=0.3, dedup_enabled=False)
    assert len(filtered) == 2


# ---------------------------------------------------------------------------
# Pipeline execute() tests
# ---------------------------------------------------------------------------


async def test_execute_returns_response_and_trace():
    """Basic execute: returns QueryResponse with response_id and QueryTrace."""
    results = [_make_search_result(0.8, "relevant context about RAG")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    pipeline = _make_pipeline(vector_store=vector_store)
    query = QueryRequest(question="What is RAG?")
    response, trace = await pipeline.execute(query)

    assert isinstance(response.response_id, UUID)
    assert response.answer == "The answer."
    assert len(response.sources) == 1
    assert trace.response_id == response.response_id
    assert len(trace.steps) == 6  # embed, search, filter, build_prompt, llm, safeguard
    step_names = [s.name for s in trace.steps]
    assert "embed_query" in step_names
    assert "llm_call" in step_names
    assert "safeguard" in step_names


async def test_execute_no_relevant_context():
    """When all chunks score below threshold, no_relevant_context=True."""
    results = [_make_search_result(0.1, "irrelevant chunk")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    pipeline = _make_pipeline(
        vector_store=vector_store,
        pipeline_config=_make_pipeline_config(**{"VEKTRA_MIN_RELEVANCE_SCORE": 0.5}),
    )
    query = QueryRequest(question="Something specific")
    response, _trace = await pipeline.execute(query)

    assert response.no_relevant_context is True
    assert response.answer is None
    assert response.sources == []


async def test_execute_empty_vector_results():
    """Empty vector search → no_relevant_context=False (different: empty search, not below threshold)."""
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=[])

    pipeline = _make_pipeline(vector_store=vector_store)
    query = QueryRequest(question="Who?")
    response, _trace = await pipeline.execute(query)

    # Empty index: no results retrieved at all → no_relevant_context=False but sources=[]
    assert response.no_relevant_context is False
    # LLM is still called (with empty context)
    assert response.answer == "The answer."


async def test_execute_graceful_degradation_timeout():
    """LLM timeout with no fallback → context_only=True, answer=None."""
    results = [_make_search_result(0.9, "good context")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    llm = MagicMock()
    llm.complete = AsyncMock(side_effect=TimeoutError())
    llm.count_tokens = MagicMock(return_value=10)

    pipeline = _make_pipeline(
        vector_store=vector_store,
        llm=llm,
        llm_config=_make_llm_config(**{"VEKTRA_LLM_FALLBACK_TIMEOUT_MS": 1}),
    )
    query = QueryRequest(question="Explain RAG")
    response, _trace = await pipeline.execute(query)

    assert response.context_only is True
    assert response.answer is None


async def test_execute_graceful_degradation_fallback_succeeds():
    """Primary LLM fails, fallback model succeeds."""
    results = [_make_search_result(0.9, "good context")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    call_count = 0

    async def _side_effect(messages, model, **kwargs):
        nonlocal call_count
        call_count += 1
        if model == "ollama/llama3":
            raise ConnectionError("Primary down")
        return CompletionResponse(
            content="Fallback answer",
            model="ollama/llama3-mini",
            prompt_tokens=5,
            completion_tokens=10,
            total_tokens=15,
        )

    llm = MagicMock()
    llm.complete = AsyncMock(side_effect=_side_effect)
    llm.count_tokens = MagicMock(return_value=10)

    pipeline = _make_pipeline(
        vector_store=vector_store,
        llm=llm,
        llm_config=_make_llm_config(
            **{"VEKTRA_LLM_FALLBACK_MODEL": "ollama/llama3-mini"}
        ),
    )
    query = QueryRequest(question="Explain RAG")
    response, _trace = await pipeline.execute(query)

    assert response.answer == "Fallback answer"
    assert call_count == 2  # primary + fallback


async def test_execute_safeguard_blocks_response():
    """Safeguard pre_response blocks → answer=None."""
    results = [_make_search_result(0.9, "relevant context")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    safeguard = AsyncMock()
    safeguard.pre_response = AsyncMock(
        return_value=SafeguardResult(allowed=False, reason="Content policy")
    )

    pipeline = _make_pipeline(vector_store=vector_store, safeguard=safeguard)
    query = QueryRequest(question="Sensitive question")
    response, _trace = await pipeline.execute(query)

    assert response.answer is None


async def test_query_trace_contains_no_pii():
    """QueryTrace must NOT contain question text or response text (REQ-051)."""
    import dataclasses

    results = [_make_search_result(0.8, "some context")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    pipeline = _make_pipeline(vector_store=vector_store)
    question = "My-very-specific-secret-question-XYZ"
    answer_text = "The-very-secret-answer-ABC"

    llm = MagicMock()
    llm.complete = AsyncMock(
        return_value=CompletionResponse(
            content=answer_text,
            model="ollama/llama3",
            prompt_tokens=5,
            completion_tokens=10,
            total_tokens=15,
        )
    )
    llm.count_tokens = MagicMock(return_value=5)
    pipeline._llm = llm

    query = QueryRequest(question=question)
    _response, trace = await pipeline.execute(query)

    trace_dict = dataclasses.asdict(trace)
    trace_str = str(trace_dict)
    assert question not in trace_str
    assert answer_text not in trace_str


async def test_execute_saves_conversation_turn():
    """execute() saves question+answer to conversation store when conversation_id set."""
    results = [_make_search_result(0.9, "context")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    conv_store = ConversationStore()
    pipeline = _make_pipeline(vector_store=vector_store)
    pipeline._conversation_store = conv_store

    cid = uuid4()
    query = QueryRequest(question="Q1?", conversation_id=cid)
    _response, _ = await pipeline.execute(query)

    history = await conv_store.get_history(cid)
    assert len(history) == 1
    assert history[0]["question"] == "Q1?"
    assert history[0]["answer"] == "The answer."
