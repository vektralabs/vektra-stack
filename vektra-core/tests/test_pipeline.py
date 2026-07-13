"""Unit tests for SimpleQueryPipeline (ADR-0014, ARCH-036, ARCH-056)."""

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from vektra_core.conversation import InMemoryConversationStore
from vektra_core.pipeline import (
    SimpleQueryPipeline,
    _apply_retrieval_filter,
    _context_window_fallback_warned,
    _context_window_impl,
    _count_tokens_impl,
    _token_count_fallback_warned,
    _token_overlap_ratio,
)
from vektra_core.templates import TemplateRenderer
from vektra_shared.config import LLMConfig, QueryPipelineConfig
from vektra_shared.types import (
    CompletionChunk,
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
        "VEKTRA_EVAL_MODE": False,
        "VEKTRA_DEBUG_LOG_QUERIES": False,
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
        safeguard.post_retrieval = AsyncMock(return_value=SafeguardResult(allowed=True))

    return SimpleQueryPipeline(
        embedding=embedding,
        vector_store=vector_store,
        llm=llm,
        llm_config=llm_config or _make_llm_config(),
        safeguard=safeguard,
        conversation_store=InMemoryConversationStore(max_turns=10),
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
    filtered, rescued = _apply_retrieval_filter(
        results, min_score=0.3, dedup_enabled=False
    )
    scores = [r.score for r in filtered]
    assert all(s >= 0.3 for s in scores)
    assert len(filtered) == 2
    assert rescued == 0


def test_retrieval_filter_dedup_removes_near_duplicate():
    text_a = "the quick brown fox jumps over the lazy dog and more words here"
    text_b = "the quick brown fox jumps over the lazy dog and more words there"  # >80% overlap
    results = [
        _make_search_result(0.9, text_a),
        _make_search_result(0.7, text_b),
    ]
    filtered, _ = _apply_retrieval_filter(results, min_score=0.3, dedup_enabled=True)
    assert len(filtered) == 1
    assert filtered[0].score == 0.9  # higher-scoring one kept


def test_retrieval_filter_no_dedup_keeps_all():
    text_a = "the quick brown fox"
    text_b = "the quick brown fox"  # identical
    results = [
        _make_search_result(0.9, text_a),
        _make_search_result(0.7, text_b),
    ]
    filtered, _ = _apply_retrieval_filter(results, min_score=0.3, dedup_enabled=False)
    assert len(filtered) == 2


def test_retrieval_filter_rescue_disabled_by_default():
    results = [
        _make_search_result(0.09, "part one"),
        _make_search_result(0.07, "part two"),
    ]
    filtered, rescued = _apply_retrieval_filter(
        results, min_score=0.15, dedup_enabled=False
    )
    assert filtered == []
    assert rescued == 0


def test_retrieval_filter_rescue_keeps_top_k_above_floor():
    results = [
        _make_search_result(0.004, "below floor"),
        _make_search_result(0.09, "part one"),
        _make_search_result(0.05, "part three"),
        _make_search_result(0.07, "part two"),
    ]
    filtered, rescued = _apply_retrieval_filter(
        results,
        min_score=0.15,
        dedup_enabled=False,
        rescue_top_k=3,
        rescue_floor=0.02,
    )
    assert [r.score for r in filtered] == [0.09, 0.07, 0.05]
    assert rescued == 3


def test_retrieval_filter_rescue_all_below_floor_returns_empty():
    results = [
        _make_search_result(0.01, "noise"),
        _make_search_result(0.005, "more noise"),
    ]
    filtered, rescued = _apply_retrieval_filter(
        results,
        min_score=0.15,
        dedup_enabled=False,
        rescue_top_k=3,
        rescue_floor=0.02,
    )
    assert filtered == []
    assert rescued == 0


def test_retrieval_filter_rescue_not_triggered_when_survivors_exist():
    results = [
        _make_search_result(0.5, "strong chunk"),
        _make_search_result(0.09, "weak chunk"),
    ]
    filtered, rescued = _apply_retrieval_filter(
        results,
        min_score=0.15,
        dedup_enabled=False,
        rescue_top_k=3,
        rescue_floor=0.02,
    )
    assert [r.score for r in filtered] == [0.5]
    assert rescued == 0


def test_retrieval_filter_rescued_chunks_pass_through_dedup():
    text = "the quick brown fox jumps over the lazy dog and more words here"
    results = [
        _make_search_result(0.09, text),
        _make_search_result(0.07, text + " again"),  # >80% overlap with the first
    ]
    filtered, rescued = _apply_retrieval_filter(
        results,
        min_score=0.15,
        dedup_enabled=True,
        rescue_top_k=3,
        rescue_floor=0.02,
    )
    assert len(filtered) == 1
    assert filtered[0].score == 0.09
    assert rescued == 2  # rescued before dedup


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
    # pre_query, embed, search, filter, post_retrieval, build_prompt,
    # document_names, llm, safeguard
    assert len(trace.steps) == 9
    step_names = [s.name for s in trace.steps]
    assert "pre_query_safeguard" in step_names
    assert "embed_query" in step_names
    assert "post_retrieval_safeguard" in step_names
    assert "document_names" in step_names
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


async def test_execute_no_relevant_context_hybrid_calls_llm():
    """In hybrid mode, LLM is called even when no chunks pass threshold."""
    results = [_make_search_result(0.1, "irrelevant chunk")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    llm = MagicMock()
    completion = CompletionResponse(
        content="I know from my training that...",
        model="ollama/llama3",
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
    )
    llm.complete = AsyncMock(return_value=completion)
    llm.count_tokens = MagicMock(return_value=10)

    pipeline = _make_pipeline(
        vector_store=vector_store,
        llm=llm,
        pipeline_config=_make_pipeline_config(**{"VEKTRA_MIN_RELEVANCE_SCORE": 0.5}),
    )
    query = QueryRequest(question="Something specific", grounding_mode="hybrid")
    response, _trace = await pipeline.execute(query)

    assert response.answer is not None
    llm.complete.assert_awaited_once()


async def test_execute_empty_vector_results():
    """Empty vector search → no_relevant_context=True (no chunks to answer from)."""
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=[])

    pipeline = _make_pipeline(vector_store=vector_store)
    query = QueryRequest(question="Who?")
    response, _trace = await pipeline.execute(query)

    # Empty index: no results at all → no_relevant_context=True, LLM not called
    assert response.no_relevant_context is True
    assert response.answer is None


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

    conv_store = InMemoryConversationStore()
    pipeline = _make_pipeline(vector_store=vector_store)
    pipeline._conversation_store = conv_store

    cid = uuid4()
    query = QueryRequest(question="Q1?", conversation_id=cid)
    _response, _ = await pipeline.execute(query)

    history = await conv_store.get_history(cid)
    assert len(history) == 1
    assert history[0]["question"] == "Q1?"
    assert history[0]["answer"] == "The answer."


async def test_execute_post_retrieval_blocked():
    """When post_retrieval returns allowed=False, pipeline clears results and skips LLM."""
    results = [_make_search_result(0.8, "sensitive context")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    safeguard = AsyncMock()
    safeguard.pre_query = AsyncMock(return_value=SafeguardResult(allowed=True))
    safeguard.post_retrieval = AsyncMock(
        return_value=SafeguardResult(allowed=False, reason="blocked by policy")
    )
    safeguard.pre_response = AsyncMock(return_value=SafeguardResult(allowed=True))

    llm = MagicMock()
    llm.count_tokens = MagicMock(return_value=10)
    llm.complete = AsyncMock()

    pipeline = _make_pipeline(vector_store=vector_store, safeguard=safeguard, llm=llm)
    query = QueryRequest(question="Show me PII")
    response, trace = await pipeline.execute(query)

    # Pipeline should produce empty-context response (no LLM call)
    assert response.answer is None
    assert response.sources == []
    # LLM not called because filtered is empty after blocked post_retrieval
    llm.complete.assert_not_awaited()
    # Trace records allowed=False
    sg_steps = [s for s in trace.steps if s.name == "post_retrieval_safeguard"]
    assert len(sg_steps) == 1
    assert sg_steps[0].metadata.get("allowed") is False


async def test_execute_populates_document_name(monkeypatch):
    """FEAT-012: execute() attaches document filename to each SourceRef when the
    DB lookup succeeds, and leaves document_name=None when it returns an empty map
    (DB not initialised or all documents missing)."""
    from vektra_core import pipeline as pipeline_mod

    doc_a = uuid4()
    doc_b = uuid4()
    results = [
        _make_search_result(0.9, "chunk from A", doc_id=doc_a),
        _make_search_result(0.8, "chunk from B", doc_id=doc_b),
    ]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    # Case 1: DB lookup returns a mapping (keys stringified per helper contract)
    async def _fake_fetch_hit(doc_ids):
        # Called with the list of document ids from selected_chunks
        assert set(doc_ids) == {doc_a, doc_b}
        return {str(doc_a): "lecture-07.pdf", str(doc_b): "slides.pptx"}

    monkeypatch.setattr(pipeline_mod, "_fetch_document_names", _fake_fetch_hit)

    pipeline = _make_pipeline(vector_store=vector_store)
    response, _ = await pipeline.execute(QueryRequest(question="Q?"))
    names = {s.doc_id: s.document_name for s in response.sources}
    assert names == {doc_a: "lecture-07.pdf", doc_b: "slides.pptx"}

    # Case 2: DB lookup returns empty → document_name stays None for all sources
    async def _fake_fetch_miss(doc_ids):
        return {}

    monkeypatch.setattr(pipeline_mod, "_fetch_document_names", _fake_fetch_miss)
    response2, _ = await pipeline.execute(QueryRequest(question="Q?"))
    assert all(s.document_name is None for s in response2.sources)


async def test_fetch_document_names_marks_archived(monkeypatch):
    """Soft-deleted documents are returned with ``(archived)`` suffix (REQ-057).

    The citation must still match what was retrieved from the vector store;
    we append a marker rather than hiding the document so students can see
    that the source exists but is no longer available.
    """
    from vektra_core import pipeline as pipeline_mod

    rows = [
        ("doc-a", "active.pdf", None),
        ("doc-b", "gone.pdf", "2026-04-20T10:00:00+00:00"),
    ]

    class _FakeResult:
        def all(self):
            return rows

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def execute(self, *_a, **_k):
            return _FakeResult()

    def _factory():
        return _FakeSession()

    # Bypass get_session_factory() so the helper uses our fake session
    from vektra_shared import db as db_mod

    monkeypatch.setattr(db_mod, "get_session_factory", lambda: _factory)

    result = await pipeline_mod._fetch_document_names(["doc-a", "doc-b"])
    assert result == {"doc-a": "active.pdf", "doc-b": "gone.pdf (archived)"}


# ---------------------------------------------------------------------------
# Streaming tests (_stream / execute_stream)
# ---------------------------------------------------------------------------


async def _collect_stream(pipeline, query):
    """Helper: collect all chunks from execute_stream into a list."""
    stream = await pipeline.execute_stream(query)
    return [chunk async for chunk in stream]


async def test_stream_happy_path():
    """Stream yields token, sources, trace, done events."""
    results = [_make_search_result(0.8, "relevant context about RAG")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    llm = MagicMock()
    llm.count_tokens = MagicMock(return_value=10)

    async def _mock_stream_gen():
        for text in ["Hello", " world"]:
            yield CompletionChunk(content=text)

    llm.stream = AsyncMock(return_value=_mock_stream_gen())

    pipeline = _make_pipeline(vector_store=vector_store, llm=llm)
    query = QueryRequest(question="What is RAG?")
    chunks = await _collect_stream(pipeline, query)

    types = [c.type for c in chunks]
    assert "token" in types
    assert "sources" in types
    assert "trace" in types
    assert types[-1] == "done"

    token_chunks = [c for c in chunks if c.type == "token"]
    assert "".join(c.data for c in token_chunks) == "Hello world"


async def test_stream_no_relevant_context():
    """Stream with all chunks below threshold yields empty sources + trace + done."""
    results = [_make_search_result(0.1, "low score")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    pipeline = _make_pipeline(
        vector_store=vector_store,
        pipeline_config=_make_pipeline_config(**{"VEKTRA_MIN_RELEVANCE_SCORE": 0.5}),
    )
    query = QueryRequest(question="Something")
    chunks = await _collect_stream(pipeline, query)

    types = [c.type for c in chunks]
    assert types == ["sources", "trace", "done"]
    assert chunks[0].data == []


async def test_stream_llm_error_yields_error_and_done():
    """When LLM stream fails, yield error + trace + done."""
    results = [_make_search_result(0.9, "good context")]
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=results)

    llm = MagicMock()
    llm.count_tokens = MagicMock(return_value=10)

    async def _failing_stream():
        raise ConnectionError("LLM down")
        yield  # make it an async generator

    llm.stream = AsyncMock(return_value=_failing_stream())

    pipeline = _make_pipeline(vector_store=vector_store, llm=llm)
    query = QueryRequest(question="Test")
    chunks = await _collect_stream(pipeline, query)

    types = [c.type for c in chunks]
    assert "error" in types
    assert "trace" in types
    assert types[-1] == "done"


# ---------------------------------------------------------------------------
# _context_window_impl (BUG-017)
# ---------------------------------------------------------------------------


def test_context_window_uses_configured_value():
    """Configured context_window takes priority over litellm lookup."""
    result = _context_window_impl("nonexistent/model", configured_window=32768)
    assert result == 32768


def test_context_window_fallback_to_default():
    """Unknown model without configured window falls back to 4096 with warning."""
    _context_window_fallback_warned.discard("test/unknown-model-ctx")
    result = _context_window_impl("test/unknown-model-ctx")
    assert result == 4096
    assert "test/unknown-model-ctx" in _context_window_fallback_warned


def test_context_window_fallback_warns_once(capsys):
    """Fallback warning is emitted on first call, silent on second."""
    _context_window_fallback_warned.discard("test/warn-once-model")
    _context_window_impl("test/warn-once-model")
    first = capsys.readouterr()
    assert "context_window_fallback" in first.out
    _context_window_impl("test/warn-once-model")
    second = capsys.readouterr()
    assert "context_window_fallback" not in second.out


# ---------------------------------------------------------------------------
# _count_tokens_impl fallback warning (BUG-017)
# ---------------------------------------------------------------------------


def test_count_tokens_fallback_warns():
    """Token count fallback emits warning on first use per model."""
    _token_count_fallback_warned.discard("test/token-fallback-model")
    mock_llm = MagicMock()
    mock_llm.count_tokens.side_effect = Exception("unsupported")
    result = _count_tokens_impl(mock_llm, "test/token-fallback-model", "hello world")
    assert result == max(1, len("hello world") // 4)
    assert "test/token-fallback-model" in _token_count_fallback_warned


def test_count_tokens_fallback_warns_once(capsys):
    """Token count fallback warning emitted on first call, silent on second."""
    _token_count_fallback_warned.discard("test/token-once-model")
    mock_llm = MagicMock()
    mock_llm.count_tokens.side_effect = Exception("unsupported")
    _count_tokens_impl(mock_llm, "test/token-once-model", "first")
    first = capsys.readouterr()
    assert "token_count_fallback" in first.out
    _count_tokens_impl(mock_llm, "test/token-once-model", "second")
    second = capsys.readouterr()
    assert "token_count_fallback" not in second.out
