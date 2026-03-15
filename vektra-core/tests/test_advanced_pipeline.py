"""Unit tests for AdvancedQueryPipeline (Phase 2)."""

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from vektra_core.advanced_pipeline import AdvancedQueryPipeline
from vektra_core.conversation import InMemoryConversationStore
from vektra_core.reranker import RerankerService
from vektra_core.templates import TemplateRenderer
from vektra_shared.config import LLMConfig, QueryPipelineConfig
from vektra_shared.types import (
    CompletionChunk,
    CompletionResponse,
    QueryRequest,
    SafeguardResult,
    SearchResult,
    SparseVector,
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
        "VEKTRA_QUERY_PIPELINE": "advanced",
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


def _make_safeguard() -> AsyncMock:
    sg = AsyncMock()
    sg.pre_response = AsyncMock(return_value=SafeguardResult(allowed=True))
    sg.pre_query = AsyncMock(return_value=SafeguardResult(allowed=True))
    sg.post_retrieval = AsyncMock(return_value=SafeguardResult(allowed=True))
    return sg


def _make_pipeline(
    *,
    embedding=None,
    vector_store=None,
    llm=None,
    safeguard=None,
    sparse_embedding=None,
    reranker=None,
    llm_config=None,
    pipeline_config=None,
    conversation_store=None,
) -> AdvancedQueryPipeline:
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
        safeguard = _make_safeguard()

    return AdvancedQueryPipeline(
        embedding=embedding,
        vector_store=vector_store,
        llm=llm,
        llm_config=llm_config or _make_llm_config(),
        safeguard=safeguard,
        conversation_store=conversation_store
        or InMemoryConversationStore(max_turns=10),
        renderer=TemplateRenderer(),
        pipeline_config=pipeline_config or _make_pipeline_config(),
        sparse_embedding=sparse_embedding,
        reranker=reranker,
    )


# ---------------------------------------------------------------------------
# Basic execute tests
# ---------------------------------------------------------------------------


async def test_execute_returns_response_and_trace():
    """Basic execute with a single relevant result."""
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
    step_names = [s.name for s in trace.steps]
    assert "query_rewrite" in step_names
    assert "embed_query" in step_names
    assert "post_retrieval_safeguard" in step_names
    assert "llm_call" in step_names
    assert "safeguard" in step_names


async def test_execute_no_relevant_context():
    """When all chunks score below threshold, no_relevant_context=True."""
    results = [_make_search_result(0.1, "irrelevant")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    pipeline = _make_pipeline(vector_store=vector_store)
    response, _trace = await pipeline.execute(QueryRequest(question="test"))

    assert response.no_relevant_context is True
    assert response.answer is None


# ---------------------------------------------------------------------------
# Query rewriting
# ---------------------------------------------------------------------------


async def test_query_rewrite_with_history():
    """Rewriting rewrites when conversation history exists."""
    conv_store = InMemoryConversationStore(max_turns=10)
    cid = uuid4()
    await conv_store.add_turn(
        cid, "What animals are in the fable?", "A fox and a crow."
    )

    # Mock LLM to return a rewritten query
    llm = MagicMock()
    llm.count_tokens = MagicMock(return_value=10)
    # First call: rewrite. Second call: LLM answer
    rewrite_response = CompletionResponse(
        content="What animals are mentioned in the fable about the fox and crow?",
        model="ollama/llama3",
        prompt_tokens=10,
        completion_tokens=15,
        total_tokens=25,
    )
    answer_response = CompletionResponse(
        content="The fable mentions a fox and a crow.",
        model="ollama/llama3",
        prompt_tokens=50,
        completion_tokens=20,
        total_tokens=70,
    )
    llm.complete = AsyncMock(side_effect=[rewrite_response, answer_response])

    results = [_make_search_result(0.8, "The fox and crow fable")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    pipeline = _make_pipeline(
        llm=llm,
        vector_store=vector_store,
        conversation_store=conv_store,
    )
    query = QueryRequest(question="Which ones?", conversation_id=cid)
    _response, trace = await pipeline.execute(query)

    # Verify rewrite happened
    rewrite_step = next(s for s in trace.steps if s.name == "query_rewrite")
    assert rewrite_step.metadata["rewritten"] is True
    assert rewrite_step.metadata["history_turns_used"] == 1


async def test_query_rewrite_skipped_no_history():
    """Rewriting skips when there is no conversation history."""
    results = [_make_search_result(0.8, "context")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    pipeline = _make_pipeline(vector_store=vector_store)
    _, trace = await pipeline.execute(QueryRequest(question="test"))

    rewrite_step = next(s for s in trace.steps if s.name == "query_rewrite")
    assert rewrite_step.metadata["rewritten"] is False


async def test_query_rewrite_disabled(monkeypatch):
    """Rewriting skips when disabled via config."""
    monkeypatch.setenv("VEKTRA_QUERY_REWRITE_ENABLED", "false")
    config = _make_pipeline_config()
    results = [_make_search_result(0.8, "context")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    conv_store = InMemoryConversationStore(max_turns=10)
    cid = uuid4()
    await conv_store.add_turn(cid, "prev", "answer")

    pipeline = _make_pipeline(
        vector_store=vector_store,
        pipeline_config=config,
        conversation_store=conv_store,
    )
    _, trace = await pipeline.execute(
        QueryRequest(question="test", conversation_id=cid)
    )

    rewrite_step = next(s for s in trace.steps if s.name == "query_rewrite")
    assert rewrite_step.metadata["rewritten"] is False


# ---------------------------------------------------------------------------
# Reranking
# ---------------------------------------------------------------------------


async def test_reranking_narrows_results():
    """Reranker reorders and limits results to top_k."""
    results = [
        _make_search_result(0.6, "less relevant"),
        _make_search_result(0.5, "least relevant"),
        _make_search_result(0.9, "most relevant"),
    ]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    # Mock reranker to return only the best result
    reranker = AsyncMock(spec=RerankerService)
    reranker.rerank = AsyncMock(return_value=[results[2]])

    pipeline = _make_pipeline(vector_store=vector_store, reranker=reranker)
    response, trace = await pipeline.execute(QueryRequest(question="test", top_k=1))

    reranker.rerank.assert_awaited_once()
    assert len(response.sources) == 1
    step_names = [s.name for s in trace.steps]
    assert "rerank" in step_names


async def test_reranking_fallback_on_failure():
    """When reranker fails, results pass through with warning."""
    results = [_make_search_result(0.8, "context")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    reranker = AsyncMock(spec=RerankerService)
    reranker.rerank = AsyncMock(side_effect=RuntimeError("model crash"))

    pipeline = _make_pipeline(vector_store=vector_store, reranker=reranker)
    response, trace = await pipeline.execute(QueryRequest(question="test"))

    assert response.answer is not None
    rerank_step = next(s for s in trace.steps if s.name == "rerank")
    assert rerank_step.metadata.get("skipped") is True


# ---------------------------------------------------------------------------
# Hybrid search
# ---------------------------------------------------------------------------


async def test_hybrid_search_when_sparse_available():
    """When sparse embedding is available, search uses HYBRID mode."""
    results = [_make_search_result(0.8, "context")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    sparse = AsyncMock()
    sparse.embed_query = AsyncMock(
        return_value=SparseVector(indices=[1, 2], values=[0.5, 0.3])
    )

    pipeline = _make_pipeline(vector_store=vector_store, sparse_embedding=sparse)
    await pipeline.execute(QueryRequest(question="test"))

    call_args = vector_store.search.call_args
    assert call_args.kwargs["search_mode"].value == "hybrid"


async def test_dense_fallback_when_sparse_fails():
    """When sparse embedding fails, fall back to DENSE search."""
    results = [_make_search_result(0.8, "context")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    sparse = AsyncMock()
    sparse.embed_query = AsyncMock(side_effect=RuntimeError("sparse failed"))

    pipeline = _make_pipeline(vector_store=vector_store, sparse_embedding=sparse)
    response, _trace = await pipeline.execute(QueryRequest(question="test"))

    call_args = vector_store.search.call_args
    assert call_args.kwargs["search_mode"].value == "dense"
    assert response.answer is not None


# ---------------------------------------------------------------------------
# Post-retrieval safeguard
# ---------------------------------------------------------------------------


async def test_post_retrieval_filters_chunks():
    """Post-retrieval safeguard removes chunks by filtered_ids."""
    r1 = _make_search_result(0.9, "clean chunk")
    r2 = _make_search_result(0.8, "PII chunk with John Smith")

    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=[r1, r2])

    safeguard = _make_safeguard()
    safeguard.post_retrieval = AsyncMock(
        return_value=SafeguardResult(allowed=True, filtered_ids=[r2.chunk_id])
    )

    pipeline = _make_pipeline(vector_store=vector_store, safeguard=safeguard)
    response, _trace = await pipeline.execute(QueryRequest(question="test"))

    assert len(response.sources) == 1
    assert response.sources[0].chunk_id == r1.chunk_id


async def test_post_retrieval_safeguard_failure_continues():
    """Post-retrieval safeguard failure is gracefully handled."""
    results = [_make_search_result(0.8, "context")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    safeguard = _make_safeguard()
    safeguard.post_retrieval = AsyncMock(side_effect=RuntimeError("safeguard crash"))

    pipeline = _make_pipeline(vector_store=vector_store, safeguard=safeguard)
    response, trace = await pipeline.execute(QueryRequest(question="test"))

    assert response.answer is not None
    sg_step = next(s for s in trace.steps if s.name == "post_retrieval_safeguard")
    assert sg_step.metadata.get("skipped") is True


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


async def test_query_rewrite_failure_uses_original():
    """When rewrite LLM call fails, original query is used."""
    conv_store = InMemoryConversationStore(max_turns=10)
    cid = uuid4()
    await conv_store.add_turn(cid, "prev", "answer")

    llm = MagicMock()
    llm.count_tokens = MagicMock(return_value=10)
    # First call (rewrite) fails, second call (answer) succeeds
    answer_response = CompletionResponse(
        content="The answer.",
        model="ollama/llama3",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
    )
    llm.complete = AsyncMock(
        side_effect=[RuntimeError("rewrite failed"), answer_response]
    )

    results = [_make_search_result(0.8, "context")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    pipeline = _make_pipeline(
        llm=llm,
        vector_store=vector_store,
        conversation_store=conv_store,
    )
    response, trace = await pipeline.execute(
        QueryRequest(question="test", conversation_id=cid)
    )

    assert response.answer == "The answer."
    rewrite_step = next(s for s in trace.steps if s.name == "query_rewrite")
    assert rewrite_step.metadata["rewritten"] is False


async def test_pre_response_safeguard_failure_returns_answer():
    """When pre_response safeguard fails, unmodified answer is returned."""
    results = [_make_search_result(0.8, "context")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    safeguard = _make_safeguard()
    safeguard.pre_response = AsyncMock(side_effect=RuntimeError("safeguard crash"))

    llm = MagicMock()
    llm.count_tokens = MagicMock(return_value=10)
    llm.complete = AsyncMock(
        return_value=CompletionResponse(
            content="The answer.",
            model="ollama/llama3",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )
    )

    pipeline = _make_pipeline(llm=llm, vector_store=vector_store, safeguard=safeguard)

    # pre_response raises, pipeline degrades gracefully: unmodified answer returned
    response, trace = await pipeline.execute(QueryRequest(question="test"))
    assert response.answer == "The answer."
    safeguard_steps = [s for s in trace.steps if s.name == "safeguard"]
    assert len(safeguard_steps) == 1
    assert safeguard_steps[0].metadata.get("skipped") is True


# ---------------------------------------------------------------------------
# Streaming with trace (DEBT-002)
# ---------------------------------------------------------------------------


async def test_stream_emits_trace_before_done():
    """Streaming emits a trace event before the done event."""
    results = [_make_search_result(0.8, "context")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    llm = MagicMock()
    llm.count_tokens = MagicMock(return_value=10)
    llm.complete = AsyncMock(
        return_value=CompletionResponse(
            content="The answer.",
            model="ollama/llama3",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )
    )

    async def _mock_stream(*args, **kwargs):
        yield CompletionChunk(content="Hello", done=False)
        yield CompletionChunk(content=" world", done=True)

    llm.stream = AsyncMock(return_value=_mock_stream())

    pipeline = _make_pipeline(llm=llm, vector_store=vector_store)
    chunks = []
    stream = await pipeline.execute_stream(QueryRequest(question="test"))
    async for chunk in stream:
        chunks.append(chunk)

    types = [c.type for c in chunks]
    assert "trace" in types
    assert "done" in types
    # Trace must come before done
    trace_idx = types.index("trace")
    done_idx = types.index("done")
    assert trace_idx < done_idx

    # Trace data is a dict
    trace_chunk = chunks[trace_idx]
    assert isinstance(trace_chunk.data, dict)
    assert "response_id" in trace_chunk.data
    assert "steps" in trace_chunk.data


async def test_stream_no_relevant_context_still_emits_trace():
    """Even with no relevant context, stream emits trace."""
    results = [_make_search_result(0.1, "irrelevant")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    pipeline = _make_pipeline(vector_store=vector_store)
    chunks = []
    stream = await pipeline.execute_stream(QueryRequest(question="test"))
    async for chunk in stream:
        chunks.append(chunk)

    types = [c.type for c in chunks]
    assert "trace" in types
    assert "done" in types
