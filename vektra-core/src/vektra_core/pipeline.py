"""SimpleQueryPipeline: RAG query pipeline (ADR-0014, ARCH-036, ARCH-056).

Pipeline steps:
  embed_query       → EmbeddingProvider.embed_query()
  vector_search     → VectorStoreProvider.search()
  retrieval_filter  → score threshold + overlap deduplication (ARCH-056)
  build_prompt      → token budget allocation + Jinja2 template rendering (ARCH-055)
  llm_call          → LitellmProvider.complete() with graceful degradation (ARCH-043)
  safeguard         → SafeguardHook.pre_response()

QueryTrace is emitted via structlog after each execute() call (ARCH-041).
QueryTrace never contains query text or response text (REQ-051 / ADR-0017).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import litellm
import structlog

from vektra_core.budget import allocate_token_budget
from vektra_core.conversation import ConversationStore
from vektra_core.templates import TemplateRenderer
from vektra_shared.config import LLMConfig, QueryPipelineConfig
from vektra_shared.protocols import (
    EmbeddingProvider,
    LLMProvider,
    SafeguardHook,
    VectorStoreProvider,
)
from vektra_shared.types import (
    ChunkRef,
    Message,
    QueryChunk,
    QueryEmbedding,
    QueryRequest,
    QueryResponse,
    QueryTrace,
    SafeguardContext,
    SearchResult,
    SourceRef,
    StepTrace,
)

log = structlog.get_logger(__name__)

_DEFAULT_CONTEXT_WINDOW = 4096


def _elapsed_ms(since: float) -> int:
    return int((time.monotonic() - since) * 1000)


# ---------------------------------------------------------------------------
# Retrieval filter (ARCH-056)
# ---------------------------------------------------------------------------


def _token_overlap_ratio(text_a: str, text_b: str) -> float:
    """Compute Jaccard-style token overlap: intersection / min(size_a, size_b)."""
    tokens_a = set(text_a.lower().split())
    tokens_b = set(text_b.lower().split())
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / min(len(tokens_a), len(tokens_b))


def _apply_retrieval_filter(
    results: list[SearchResult],
    min_score: float,
    dedup_enabled: bool,
) -> list[SearchResult]:
    """Filter by relevance score and remove near-duplicate chunks (ARCH-056).

    Step 1: Remove chunks below min_score.
    Step 2: If dedup_enabled, remove chunks with >80% token overlap with any
            already-selected chunk (keeping higher-scoring ones by processing
            in score-descending order).

    Returns results in score-descending order.
    """
    filtered = [r for r in results if r.score >= min_score]

    if not dedup_enabled:
        return filtered

    # Process by score descending; keep first occurrence of near-duplicates
    kept: list[SearchResult] = []
    for candidate in sorted(filtered, key=lambda r: r.score, reverse=True):
        is_dup = any(
            _token_overlap_ratio(candidate.text_snippet, sel.text_snippet) > 0.8
            for sel in kept
        )
        if not is_dup:
            kept.append(candidate)

    return kept


# ---------------------------------------------------------------------------
# Shared helpers (used by both SimpleQueryPipeline and AdvancedQueryPipeline)
# ---------------------------------------------------------------------------


def _count_tokens_impl(llm: LLMProvider, model: str, text: str) -> int:
    """Count tokens using the LLM provider, with char/4 fallback."""
    try:
        return llm.count_tokens(text, model)
    except Exception:
        return max(1, len(text) // 4)


def _context_window_impl(model: str) -> int:
    """Get context window size from litellm, with default fallback."""
    try:
        return litellm.get_max_tokens(model) or _DEFAULT_CONTEXT_WINDOW
    except Exception:
        return _DEFAULT_CONTEXT_WINDOW


async def _call_llm_with_fallback_impl(
    llm: LLMProvider,
    llm_config: LLMConfig,
    messages: list[Message],
) -> tuple[str | None, str]:
    """Try primary LLM, then fallback model, then context-only (ARCH-043).

    Returns (answer_text | None, model_used).
    None answer means context-only response (REQ-059).
    """
    timeout_s = llm_config.fallback_timeout_ms / 1000.0

    try:
        result = await asyncio.wait_for(
            llm.complete(messages, model=llm_config.provider),
            timeout=timeout_s,
        )
        return result.content, result.model
    except Exception as exc:
        log.warning(
            "llm_primary_failed",
            model=llm_config.provider,
            error=str(exc),
        )

    if llm_config.fallback_model:
        try:
            result = await asyncio.wait_for(
                llm.complete(messages, model=llm_config.fallback_model),
                timeout=timeout_s,
            )
            return result.content, result.model
        except Exception as exc:
            log.warning(
                "llm_fallback_failed",
                model=llm_config.fallback_model,
                error=str(exc),
            )

    log.info("llm_context_only", model=llm_config.provider)
    return None, llm_config.provider


def _trace_to_dict(trace: QueryTrace) -> dict[str, Any]:
    """Serialize QueryTrace to a JSON-safe dict for SSE emission."""
    return {
        "response_id": str(trace.response_id),
        "steps": [
            {
                "name": s.name,
                "duration_ms": s.duration_ms,
                "metadata": s.metadata,
            }
            for s in trace.steps
        ],
        "total_duration_ms": trace.total_duration_ms,
        "chunks_retrieved": [
            {"chunk_id": c.chunk_id, "score": c.score} for c in trace.chunks_retrieved
        ],
        "llm_model": trace.llm_model,
        "prompt_version": trace.prompt_version,
        "created_at": trace.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# SimpleQueryPipeline
# ---------------------------------------------------------------------------


class SimpleQueryPipeline:
    """Implements QueryPipeline Protocol (Phase 1).

    All dependencies are injected at construction time by infra-app-entrypoint.
    """

    def __init__(
        self,
        *,
        embedding: EmbeddingProvider,
        vector_store: VectorStoreProvider,
        llm: LLMProvider,
        llm_config: LLMConfig,
        safeguard: SafeguardHook,
        conversation_store: ConversationStore,
        renderer: TemplateRenderer,
        pipeline_config: QueryPipelineConfig,
    ) -> None:
        self._embedding = embedding
        self._vector_store = vector_store
        self._llm = llm
        self._llm_config = llm_config
        self._safeguard = safeguard
        self._conversation_store = conversation_store
        self._renderer = renderer
        self._config = pipeline_config

    def _count_tokens(self, text: str) -> int:
        return _count_tokens_impl(self._llm, self._llm_config.provider, text)

    def _context_window(self) -> int:
        return _context_window_impl(self._llm_config.provider)

    async def _call_llm_with_fallback(
        self,
        messages: list[Message],
    ) -> tuple[str | None, str]:
        return await _call_llm_with_fallback_impl(self._llm, self._llm_config, messages)

    async def _run_pre_query_safeguard(
        self, query: QueryRequest, steps: list[StepTrace]
    ) -> bool:
        """Run pre_query safeguard (ARCH-049). Returns True if blocked."""
        t0 = time.monotonic()
        sg_ctx = SafeguardContext(
            namespace=query.namespace,
            conversation_id=query.conversation_id,
        )
        try:
            query_hash = hashlib.sha256(query.question.encode()).hexdigest()[:16]
            sg_pre = await self._safeguard.pre_query(query_hash, sg_ctx)
            steps.append(
                StepTrace(
                    name="pre_query_safeguard",
                    duration_ms=_elapsed_ms(t0),
                    metadata={"allowed": sg_pre.allowed},
                )
            )
            return not sg_pre.allowed
        except Exception as exc:
            log.error("pre_query_safeguard_failed", error=str(exc))
            steps.append(
                StepTrace(
                    name="pre_query_safeguard",
                    duration_ms=_elapsed_ms(t0),
                    metadata={"skipped": True, "error": str(exc)},
                )
            )
            return False

    async def execute(
        self,
        query: QueryRequest,
    ) -> tuple[QueryResponse, QueryTrace]:
        """Execute the full RAG pipeline, returning response + trace."""
        t_total = time.monotonic()
        response_id = uuid4()
        steps: list[StepTrace] = []

        # Pre-query safeguard (ARCH-049)
        if await self._run_pre_query_safeguard(query, steps):
            trace = QueryTrace(
                response_id=response_id,
                steps=steps,
                total_duration_ms=_elapsed_ms(t_total),
                chunks_retrieved=[],
                llm_model=self._llm_config.provider,
                prompt_version=self._renderer.prompt_version,
                created_at=datetime.now(UTC),
            )
            return QueryResponse(
                response_id=response_id,
                answer=None,
                sources=[],
                conversation_id=query.conversation_id,
            ), trace

        # Step 1: Embed query
        t0 = time.monotonic()
        dense = await self._embedding.embed_query(query.question)
        steps.append(StepTrace(name="embed_query", duration_ms=_elapsed_ms(t0)))

        # Step 2: Vector search
        t0 = time.monotonic()
        results = await self._vector_store.search(
            namespace=query.namespace,
            query_embedding=QueryEmbedding(dense=dense),
            top_k=query.top_k,
            search_mode=query.search_mode,
            filters=query.filters,
        )
        steps.append(
            StepTrace(
                name="vector_search",
                duration_ms=_elapsed_ms(t0),
                metadata={"retrieved": len(results)},
            )
        )

        # Step 3: Retrieval filter
        t0 = time.monotonic()
        filtered = _apply_retrieval_filter(
            results,
            min_score=self._config.min_relevance_score,
            dedup_enabled=self._config.chunk_dedup_enabled,
        )
        no_relevant_context = len(filtered) == 0
        steps.append(
            StepTrace(
                name="retrieval_filter",
                duration_ms=_elapsed_ms(t0),
                metadata={
                    "before": len(results),
                    "after": len(filtered),
                    "no_relevant_context": no_relevant_context,
                },
            )
        )

        # Post-retrieval safeguard (DEBT-003)
        safeguard_blocked = False
        if filtered:
            t0 = time.monotonic()
            sg_ctx_post = SafeguardContext(
                namespace=query.namespace,
                conversation_id=query.conversation_id,
            )
            try:
                query_hash = hashlib.sha256(query.question.encode()).hexdigest()[:16]
                sg_post = await self._safeguard.post_retrieval(
                    query_hash, filtered, sg_ctx_post
                )
                if not sg_post.allowed:
                    filtered = []
                    safeguard_blocked = True
                elif sg_post.filtered_ids:
                    excluded = set(sg_post.filtered_ids)
                    filtered = [r for r in filtered if r.chunk_id not in excluded]
                steps.append(
                    StepTrace(
                        name="post_retrieval_safeguard",
                        duration_ms=_elapsed_ms(t0),
                        metadata={
                            "allowed": sg_post.allowed,
                            "filtered_out": len(sg_post.filtered_ids or []),
                            "remaining": len(filtered),
                        },
                    )
                )
            except Exception as exc:
                log.error("post_retrieval_safeguard_failed", error=str(exc))
                steps.append(
                    StepTrace(
                        name="post_retrieval_safeguard",
                        duration_ms=_elapsed_ms(t0),
                        metadata={"skipped": True, "error": str(exc)},
                    )
                )

        # No relevant context or safeguard blocked → skip LLM, return early
        if no_relevant_context or safeguard_blocked:
            trace = QueryTrace(
                response_id=response_id,
                steps=steps,
                total_duration_ms=_elapsed_ms(t_total),
                chunks_retrieved=[],
                llm_model=self._llm_config.provider,
                prompt_version=self._renderer.prompt_version,
                created_at=datetime.now(UTC),
            )
            log.info(
                "query_no_relevant_context",
                response_id=str(response_id),
                namespace=query.namespace,
            )
            return QueryResponse(
                response_id=response_id,
                answer=None,
                sources=[],
                conversation_id=query.conversation_id,
                context_only=safeguard_blocked,
                no_relevant_context=no_relevant_context,
            ), trace

        # Step 4: Build prompt
        t0 = time.monotonic()
        history: list[dict[str, Any]] = []
        if query.conversation_id is not None:
            history = await self._conversation_store.get_history(query.conversation_id)

        # Token budget allocation
        system_text = self._renderer.render_system(namespace=query.namespace)
        system_tokens = self._count_tokens(system_text)
        question_tokens = self._count_tokens(query.question)
        # DEBT-004: sort by score descending so budget allocator selects
        # highest-scoring chunks first, regardless of provider ordering
        filtered = sorted(filtered, key=lambda r: r.score, reverse=True)
        chunk_inputs = [(r.score, self._count_tokens(r.text_snippet)) for r in filtered]
        history_tokens = [
            self._count_tokens((t["question"] or "") + " " + (t["answer"] or ""))
            for t in history
        ]

        selected_chunk_idx, selected_history_idx = allocate_token_budget(
            context_window=self._context_window(),
            system_tokens=system_tokens,
            question_tokens=question_tokens,
            chunks=chunk_inputs,
            history_turns=history_tokens,
            reserve=self._config.response_token_reserve,
            chunk_ratio=self._config.context_chunk_ratio,
        )

        selected_chunks = [filtered[i] for i in selected_chunk_idx]
        selected_history = [history[i] for i in selected_history_idx]

        context_text = self._renderer.render_context(
            [{"text": r.text_snippet, "score": r.score} for r in selected_chunks]
        )
        conv_text = self._renderer.render_conversation(selected_history)

        messages: list[Message] = [Message(role="system", content=system_text)]
        if conv_text.strip():
            messages.append(
                Message(role="user", content=f"Previous conversation:\n{conv_text}")
            )
        messages.append(
            Message(
                role="user",
                content=f"Context:\n{context_text}\n\nQuestion: {query.question}",
            )
        )

        steps.append(
            StepTrace(
                name="build_prompt",
                duration_ms=_elapsed_ms(t0),
                metadata={
                    "prompt_version": self._renderer.prompt_version,
                    "chunks_in_prompt": len(selected_chunks),
                    "history_turns_in_prompt": len(selected_history),
                },
            )
        )

        # Sources from budget-selected chunks only (not all filtered)
        sources = [
            SourceRef(
                doc_id=r.document_id,
                chunk_id=r.chunk_id,
                score=r.score,
                snippet=r.text_snippet,
                citation_id=uuid4(),
                document_version=r.document_version,
            )
            for r in selected_chunks
        ]

        # Step 5: LLM call with graceful degradation
        t0 = time.monotonic()
        answer, llm_model = await self._call_llm_with_fallback(messages)
        steps.append(
            StepTrace(
                name="llm_call",
                duration_ms=_elapsed_ms(t0),
                metadata={"model": llm_model, "context_only": answer is None},
            )
        )

        # Step 6: Safeguard pre_response
        t0 = time.monotonic()
        sg_ctx = SafeguardContext(
            namespace=query.namespace,
            conversation_id=query.conversation_id,
        )
        try:
            sg_result = await self._safeguard.pre_response(answer or "", sg_ctx)
            if not sg_result.allowed:
                answer = None
            elif sg_result.modified_content is not None:
                answer = sg_result.modified_content
            steps.append(
                StepTrace(
                    name="safeguard",
                    duration_ms=_elapsed_ms(t0),
                    metadata={"allowed": sg_result.allowed},
                )
            )
        except Exception as exc:
            log.error("pre_response_safeguard_failed", error=str(exc))
            steps.append(
                StepTrace(
                    name="safeguard",
                    duration_ms=_elapsed_ms(t0),
                    metadata={"skipped": True, "error": str(exc)},
                )
            )

        # Save conversation turn
        if query.conversation_id is not None:
            await self._conversation_store.add_turn(
                query.conversation_id, query.question, answer
            )

        total_ms = _elapsed_ms(t_total)
        trace = QueryTrace(
            response_id=response_id,
            steps=steps,
            total_duration_ms=total_ms,
            chunks_retrieved=[
                ChunkRef(chunk_id=r.chunk_id, score=r.score) for r in filtered
            ],
            llm_model=llm_model,
            prompt_version=self._renderer.prompt_version,
            created_at=datetime.now(UTC),
        )

        log.info(
            "query_complete",
            response_id=str(response_id),
            duration_ms=total_ms,
            llm_model=llm_model,
            chunks_retrieved=len(filtered),
            pipeline="simple",
        )

        return QueryResponse(
            response_id=response_id,
            answer=answer,
            sources=sources,
            conversation_id=query.conversation_id,
            context_only=answer is None and not no_relevant_context,
            no_relevant_context=no_relevant_context,
        ), trace

    async def execute_stream(self, query: QueryRequest) -> AsyncIterator[QueryChunk]:
        """Return an async iterator that yields QueryChunk events for SSE streaming."""
        return self._stream(query)

    async def _stream(self, query: QueryRequest) -> AsyncGenerator[QueryChunk, None]:
        """Async generator for streaming response (SSE) with trace emission (DEBT-002)."""

        t_total = time.monotonic()
        response_id = uuid4()
        steps: list[StepTrace] = []

        # Pre-query safeguard (ARCH-049)
        if await self._run_pre_query_safeguard(query, steps):
            yield QueryChunk(type="sources", data=[])
            trace = QueryTrace(
                response_id=response_id,
                steps=steps,
                total_duration_ms=_elapsed_ms(t_total),
                chunks_retrieved=[],
                llm_model=self._llm_config.provider,
                prompt_version=self._renderer.prompt_version,
                created_at=datetime.now(UTC),
            )
            yield QueryChunk(type="trace", data=_trace_to_dict(trace))
            yield QueryChunk(type="done", data="")
            return

        # Step 1: Embed query
        t0 = time.monotonic()
        dense = await self._embedding.embed_query(query.question)
        steps.append(StepTrace(name="embed_query", duration_ms=_elapsed_ms(t0)))

        # Step 2: Vector search
        t0 = time.monotonic()
        results = await self._vector_store.search(
            namespace=query.namespace,
            query_embedding=QueryEmbedding(dense=dense),
            top_k=query.top_k,
            search_mode=query.search_mode,
            filters=query.filters,
        )
        steps.append(
            StepTrace(
                name="vector_search",
                duration_ms=_elapsed_ms(t0),
                metadata={"retrieved": len(results)},
            )
        )

        # Step 3: Retrieval filter
        t0 = time.monotonic()
        filtered = _apply_retrieval_filter(
            results,
            min_score=self._config.min_relevance_score,
            dedup_enabled=self._config.chunk_dedup_enabled,
        )
        no_relevant_context = len(filtered) == 0
        steps.append(
            StepTrace(
                name="retrieval_filter",
                duration_ms=_elapsed_ms(t0),
                metadata={
                    "before": len(results),
                    "after": len(filtered),
                    "no_relevant_context": no_relevant_context,
                },
            )
        )

        if no_relevant_context or not filtered:
            yield QueryChunk(type="sources", data=[])
            trace = QueryTrace(
                response_id=response_id,
                steps=steps,
                total_duration_ms=_elapsed_ms(t_total),
                chunks_retrieved=[],
                llm_model=self._llm_config.provider,
                prompt_version=self._renderer.prompt_version,
                created_at=datetime.now(UTC),
            )
            yield QueryChunk(type="trace", data=_trace_to_dict(trace))
            yield QueryChunk(type="done", data="")
            return

        # Post-retrieval safeguard (DEBT-003)
        safeguard_blocked = False
        t0 = time.monotonic()
        sg_ctx = SafeguardContext(
            namespace=query.namespace,
            conversation_id=query.conversation_id,
        )
        try:
            query_hash = hashlib.sha256(query.question.encode()).hexdigest()[:16]
            sg_result = await self._safeguard.post_retrieval(
                query_hash, filtered, sg_ctx
            )
            if not sg_result.allowed:
                filtered = []
                safeguard_blocked = True
            elif sg_result.filtered_ids:
                excluded = set(sg_result.filtered_ids)
                filtered = [r for r in filtered if r.chunk_id not in excluded]
            steps.append(
                StepTrace(
                    name="post_retrieval_safeguard",
                    duration_ms=_elapsed_ms(t0),
                    metadata={
                        "allowed": sg_result.allowed,
                        "filtered_out": len(sg_result.filtered_ids or []),
                        "remaining": len(filtered),
                    },
                )
            )
        except Exception as exc:
            log.error("post_retrieval_safeguard_failed", error=str(exc))
            steps.append(
                StepTrace(
                    name="post_retrieval_safeguard",
                    duration_ms=_elapsed_ms(t0),
                    metadata={"skipped": True, "error": str(exc)},
                )
            )

        # Safeguard blocked all results → early return
        if safeguard_blocked:
            yield QueryChunk(type="sources", data=[])
            trace = QueryTrace(
                response_id=response_id,
                steps=steps,
                total_duration_ms=_elapsed_ms(t_total),
                chunks_retrieved=[],
                llm_model=self._llm_config.provider,
                prompt_version=self._renderer.prompt_version,
                created_at=datetime.now(UTC),
            )
            yield QueryChunk(type="trace", data=_trace_to_dict(trace))
            yield QueryChunk(type="done", data="")
            return

        # Step 4: Build prompt (with token budget allocation, ARCH-055)
        t0 = time.monotonic()
        history: list[dict[str, Any]] = []
        if query.conversation_id is not None:
            history = await self._conversation_store.get_history(query.conversation_id)

        system_text = self._renderer.render_system(namespace=query.namespace)
        system_tokens = self._count_tokens(system_text)
        question_tokens = self._count_tokens(query.question)
        # DEBT-004: sort by score descending so budget allocator selects
        # highest-scoring chunks first, regardless of provider ordering
        filtered = sorted(filtered, key=lambda r: r.score, reverse=True)
        chunk_inputs = [(r.score, self._count_tokens(r.text_snippet)) for r in filtered]
        history_tokens = [
            self._count_tokens((t["question"] or "") + " " + (t["answer"] or ""))
            for t in history
        ]

        selected_chunk_idx, selected_history_idx = allocate_token_budget(
            context_window=self._context_window(),
            system_tokens=system_tokens,
            question_tokens=question_tokens,
            chunks=chunk_inputs,
            history_turns=history_tokens,
            reserve=self._config.response_token_reserve,
            chunk_ratio=self._config.context_chunk_ratio,
        )

        selected_chunks = [filtered[i] for i in selected_chunk_idx]
        selected_history = [history[i] for i in selected_history_idx]

        context_text = self._renderer.render_context(
            [{"text": r.text_snippet, "score": r.score} for r in selected_chunks]
        )
        conv_text = self._renderer.render_conversation(selected_history)

        messages: list[Message] = [Message(role="system", content=system_text)]
        if conv_text.strip():
            messages.append(
                Message(role="user", content=f"Previous conversation:\n{conv_text}")
            )
        messages.append(
            Message(
                role="user",
                content=f"Context:\n{context_text}\n\nQuestion: {query.question}",
            )
        )
        steps.append(
            StepTrace(
                name="build_prompt",
                duration_ms=_elapsed_ms(t0),
                metadata={
                    "prompt_version": self._renderer.prompt_version,
                    "chunks_in_prompt": len(selected_chunks),
                    "history_turns_in_prompt": len(selected_history),
                },
            )
        )

        # Step 5: Stream LLM tokens
        t0 = time.monotonic()
        full_answer_parts: list[str] = []
        try:
            token_stream = await self._llm.stream(
                messages, model=self._llm_config.provider
            )
            async for chunk in token_stream:
                if chunk.content:
                    full_answer_parts.append(chunk.content)
                    yield QueryChunk(type="token", data=chunk.content)
            steps.append(
                StepTrace(
                    name="llm_stream",
                    duration_ms=_elapsed_ms(t0),
                    metadata={"model": self._llm_config.provider},
                )
            )
        except Exception as exc:
            log.warning("stream_llm_failed", error=str(exc))
            steps.append(
                StepTrace(
                    name="llm_stream",
                    duration_ms=_elapsed_ms(t0),
                    metadata={"error": str(exc)},
                )
            )
            yield QueryChunk(type="error", data="LLM unavailable")
            trace = QueryTrace(
                response_id=response_id,
                steps=steps,
                total_duration_ms=_elapsed_ms(t_total),
                chunks_retrieved=[
                    ChunkRef(chunk_id=r.chunk_id, score=r.score) for r in filtered
                ],
                llm_model=self._llm_config.provider,
                prompt_version=self._renderer.prompt_version,
                created_at=datetime.now(UTC),
            )
            yield QueryChunk(type="trace", data=_trace_to_dict(trace))
            yield QueryChunk(type="done", data="")
            return

        full_answer = "".join(full_answer_parts) or None

        # Step 6: Safeguard pre_response (post-stream, on accumulated answer)
        t0 = time.monotonic()
        try:
            sg_result = await self._safeguard.pre_response(full_answer or "", sg_ctx)
            if not sg_result.allowed:
                log.warning("stream_safeguard_blocked", namespace=query.namespace)
                full_answer = None
            elif sg_result.modified_content is not None:
                full_answer = sg_result.modified_content
            steps.append(
                StepTrace(
                    name="safeguard",
                    duration_ms=_elapsed_ms(t0),
                    metadata={"allowed": sg_result.allowed},
                )
            )
        except Exception as exc:
            log.error("pre_response_safeguard_failed", error=str(exc))
            steps.append(
                StepTrace(
                    name="safeguard",
                    duration_ms=_elapsed_ms(t0),
                    metadata={"skipped": True, "error": str(exc)},
                )
            )

        # Save conversation turn
        if query.conversation_id is not None and full_answer:
            await self._conversation_store.add_turn(
                query.conversation_id, query.question, full_answer
            )

        # Yield sources (only budget-selected chunks)
        sources_data = [
            {
                "doc_id": str(r.document_id),
                "chunk_id": r.chunk_id,
                "score": r.score,
                "snippet": r.text_snippet,
                "citation_id": str(uuid4()),
                "document_version": r.document_version,
            }
            for r in selected_chunks
        ]
        yield QueryChunk(type="sources", data=sources_data)

        # Emit trace before done (DEBT-002)
        trace = QueryTrace(
            response_id=response_id,
            steps=steps,
            total_duration_ms=_elapsed_ms(t_total),
            chunks_retrieved=[
                ChunkRef(chunk_id=r.chunk_id, score=r.score) for r in filtered
            ],
            llm_model=self._llm_config.provider,
            prompt_version=self._renderer.prompt_version,
            created_at=datetime.now(UTC),
        )
        log.info(
            "query_stream_complete",
            response_id=str(response_id),
            duration_ms=trace.total_duration_ms,
            pipeline="simple",
        )
        yield QueryChunk(type="trace", data=_trace_to_dict(trace))
        yield QueryChunk(type="done", data="")
