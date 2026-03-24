"""AdvancedQueryPipeline: Phase 2 RAG pipeline with query rewriting, hybrid search, and reranking.

10-step pipeline (ARCH-036, ARCH-061, ADR-0014, ADR-0023):
  0. query_rewrite       - LLM call to resolve pronouns/references (skip if no history)
  1. embed_query         - EmbeddingProvider.embed_query(rewritten_query)
  2. sparse_embed        - SparseEmbeddingProvider.embed_query() (skip if not registered)
  3. vector_search       - HYBRID if sparse available, else DENSE
  4. rerank              - cross-encoder reranking (skip if not configured)
  5. retrieval_filter    - score threshold + overlap dedup (ARCH-056)
  6. post_retrieval      - SafeguardHook.post_retrieval() (DEBT-003)
  7. build_prompt        - token budget + Jinja2 rendering (ARCH-055)
  8. llm_call            - LLM with graceful degradation (ARCH-043)
  9. pre_response        - SafeguardHook.pre_response() (ARCH-049)
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import structlog

from vektra_core.budget import allocate_token_budget
from vektra_core.conversation import ConversationStore
from vektra_core.pipeline import (
    _apply_retrieval_filter,
    _call_llm_with_fallback_impl,
    _context_window_impl,
    _count_tokens_impl,
    _elapsed_ms,
    _history_to_messages,
    _trace_to_dict,
)
from vektra_core.reranker import RerankerService
from vektra_core.templates import TemplateRenderer
from vektra_shared.config import LLMConfig, QueryPipelineConfig
from vektra_shared.protocols import (
    EmbeddingProvider,
    LLMProvider,
    SafeguardHook,
    SparseEmbeddingProvider,
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
    SearchMode,
    SearchResult,
    SourceRef,
    StepTrace,
)

log = structlog.get_logger(__name__)

_REWRITE_TOP_K = 20  # Fetch more candidates for reranking


class AdvancedQueryPipeline:
    """Phase 2 QueryPipeline with query rewriting, hybrid search, and reranking.

    Implements QueryPipeline Protocol. All dependencies injected at construction.
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
        sparse_embedding: SparseEmbeddingProvider | None = None,
        reranker: RerankerService | None = None,
    ) -> None:
        self._embedding = embedding
        self._vector_store = vector_store
        self._llm = llm
        self._llm_config = llm_config
        self._safeguard = safeguard
        self._conversation_store = conversation_store
        self._renderer = renderer
        self._config = pipeline_config
        self._sparse_embedding = sparse_embedding
        self._reranker = reranker
        self._rewrite_enabled = pipeline_config.rewrite.enabled

    # -- Helpers (delegating to shared module-level functions) --

    def _count_tokens(self, text: str) -> int:
        return _count_tokens_impl(self._llm, self._llm_config.provider, text)

    def _context_window(self) -> int:
        return _context_window_impl(self._llm_config.provider)

    async def _call_llm_with_fallback(
        self,
        messages: list[Message],
    ) -> tuple[str | None, str]:
        return await _call_llm_with_fallback_impl(self._llm, self._llm_config, messages)

    # -- Step 0: Query rewriting (ARCH-061) --

    async def _rewrite_query(
        self,
        question: str,
        history: list[dict[str, str | None]],
    ) -> tuple[str, StepTrace]:
        """Rewrite query using LLM to resolve pronouns and references."""
        t0 = time.monotonic()

        if not self._rewrite_enabled or not history:
            return question, StepTrace(
                name="query_rewrite",
                duration_ms=_elapsed_ms(t0),
                metadata={"rewritten": False, "history_turns_used": 0},
            )

        try:
            prompt_text = self._renderer.render_template(
                "rewrite.j2", history=history, question=question
            )

            rewrite_model = self._config.rewrite.model or self._llm_config.provider
            timeout_s = self._llm_config.fallback_timeout_ms / 1000.0
            result = await asyncio.wait_for(
                self._llm.complete(
                    [Message(role="user", content=prompt_text)],
                    model=rewrite_model,
                    temperature=0.0,
                ),
                timeout=timeout_s,
            )

            rewritten = result.content.strip()
            if not rewritten:
                rewritten = question

            original_hash = hashlib.sha256(question.encode()).hexdigest()[:8]
            return rewritten, StepTrace(
                name="query_rewrite",
                duration_ms=_elapsed_ms(t0),
                metadata={
                    "original_query_hash": original_hash,
                    "rewritten": True,
                    "history_turns_used": len(history),
                },
            )
        except Exception as exc:
            log.warning("query_rewrite_failed", error=str(exc))
            return question, StepTrace(
                name="query_rewrite",
                duration_ms=_elapsed_ms(t0),
                metadata={"rewritten": False, "error": str(exc)},
            )

    # -- Pre-LLM steps shared between execute() and execute_stream() --

    async def _run_pre_llm_steps(
        self,
        query: QueryRequest,
    ) -> tuple[
        list[StepTrace],
        list[SearchResult],
        bool,
        str,
        list[dict[str, str | None]],
    ]:
        """Run steps 0-6 (rewrite through post_retrieval safeguard).

        Returns (steps, filtered_results, no_relevant_context, effective_query, history).
        """
        steps: list[StepTrace] = []

        # Step -1: Pre-query safeguard (ARCH-049)
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
            if not sg_pre.allowed:
                return steps, [], False, query.question, []
        except Exception as exc:
            log.error("pre_query_safeguard_failed", error=str(exc))
            steps.append(
                StepTrace(
                    name="pre_query_safeguard",
                    duration_ms=_elapsed_ms(t0),
                    metadata={"skipped": True, "error": str(exc)},
                )
            )

        # Get conversation history (needed for rewrite and prompt)
        history: list[dict[str, str | None]] = []
        if query.conversation_id is not None:
            history = await self._conversation_store.get_history(query.conversation_id)

        # Step 0: Query rewrite
        effective_query, rewrite_step = await self._rewrite_query(
            query.question, history
        )
        steps.append(rewrite_step)

        # Step 1: Embed query (dense)
        t0 = time.monotonic()
        dense = await self._embedding.embed_query(effective_query)
        steps.append(StepTrace(name="embed_query", duration_ms=_elapsed_ms(t0)))

        # Step 2: Sparse embed (optional)
        sparse = None
        if self._sparse_embedding is not None:
            t0 = time.monotonic()
            try:
                sparse = await self._sparse_embedding.embed_query(effective_query)
                steps.append(
                    StepTrace(name="sparse_embed", duration_ms=_elapsed_ms(t0))
                )
            except Exception as exc:
                log.warning("sparse_embed_failed", error=str(exc))
                steps.append(
                    StepTrace(
                        name="sparse_embed",
                        duration_ms=_elapsed_ms(t0),
                        metadata={"skipped": True, "error": str(exc)},
                    )
                )

        # Step 3: Vector search
        t0 = time.monotonic()
        search_mode = SearchMode.HYBRID if sparse is not None else SearchMode.DENSE
        # Fetch more candidates for reranking
        fetch_k = max(query.top_k, _REWRITE_TOP_K) if self._reranker else query.top_k
        results = await self._vector_store.search(
            namespace=query.namespace,
            query_embedding=QueryEmbedding(dense=dense, sparse=sparse),
            top_k=fetch_k,
            search_mode=search_mode,
            filters=query.filters,
        )
        steps.append(
            StepTrace(
                name="vector_search",
                duration_ms=_elapsed_ms(t0),
                metadata={
                    "retrieved": len(results),
                    "search_mode": search_mode.value,
                },
            )
        )

        # Step 4: Rerank (optional)
        if self._reranker and results:
            t0 = time.monotonic()
            try:
                results = await self._reranker.rerank(
                    effective_query, results, top_k=query.top_k
                )
                steps.append(
                    StepTrace(
                        name="rerank",
                        duration_ms=_elapsed_ms(t0),
                        metadata={"after_rerank": len(results)},
                    )
                )
            except Exception as exc:
                log.warning("rerank_failed", error=str(exc))
                results = results[: query.top_k]
                steps.append(
                    StepTrace(
                        name="rerank",
                        duration_ms=_elapsed_ms(t0),
                        metadata={"skipped": True, "error": str(exc)},
                    )
                )

        # Step 5: Retrieval filter (ARCH-056)
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

        # Step 6: Post-retrieval safeguard (DEBT-003)
        if filtered:
            t0 = time.monotonic()
            sg_ctx = SafeguardContext(
                namespace=query.namespace,
                conversation_id=query.conversation_id,
            )
            try:
                query_hash = hashlib.sha256(effective_query.encode()).hexdigest()[:16]
                sg_result = await self._safeguard.post_retrieval(
                    query_hash, filtered, sg_ctx
                )
                if not sg_result.allowed:
                    filtered = []
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

        # Recheck: post_retrieval safeguard may have filtered all chunks
        if not no_relevant_context and not filtered:
            no_relevant_context = True

        return steps, filtered, no_relevant_context, effective_query, history

    def _build_prompt(
        self,
        query: QueryRequest,
        filtered: list[SearchResult],
        history: list[dict[str, str | None]],
    ) -> tuple[
        list[Message], list[SearchResult], list[dict[str, str | None]], StepTrace
    ]:
        """Build the LLM prompt with token budget allocation (ARCH-055)."""
        t0 = time.monotonic()

        system_text = self._renderer.render_system(namespace=query.namespace)
        system_tokens = self._count_tokens(system_text)
        question_tokens = self._count_tokens(query.question)

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

        messages: list[Message] = [Message(role="system", content=system_text)]
        messages.extend(_history_to_messages(selected_history))
        messages.append(
            Message(
                role="user",
                content=f"{context_text}\n\nQuestion: {query.question}",
            )
        )

        step = StepTrace(
            name="build_prompt",
            duration_ms=_elapsed_ms(t0),
            metadata={
                "prompt_version": self._renderer.prompt_version,
                "chunks_in_prompt": len(selected_chunks),
                "history_turns_in_prompt": len(selected_history),
            },
        )
        return messages, selected_chunks, selected_history, step

    # -- execute() --

    async def execute(
        self,
        query: QueryRequest,
    ) -> tuple[QueryResponse, QueryTrace]:
        """Execute the full 10-step advanced RAG pipeline."""
        t_total = time.monotonic()
        response_id = uuid4()

        # Steps 0-6
        (
            steps,
            filtered,
            no_relevant_context,
            _effective_query,
            history,
        ) = await self._run_pre_llm_steps(query)

        # No relevant context -> skip LLM
        if no_relevant_context or not filtered:
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
                context_only=not no_relevant_context,
                no_relevant_context=no_relevant_context,
            ), trace

        # Step 7: Build prompt
        messages, selected_chunks, _, prompt_step = self._build_prompt(
            query, filtered, history
        )
        steps.append(prompt_step)

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

        # Step 8: LLM call with graceful degradation
        t0 = time.monotonic()
        answer, llm_model = await self._call_llm_with_fallback(messages)
        steps.append(
            StepTrace(
                name="llm_call",
                duration_ms=_elapsed_ms(t0),
                metadata={"model": llm_model, "context_only": answer is None},
            )
        )

        # Step 9: Pre-response safeguard (ARCH-049)
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

        # Save conversation turn (best-effort: DB failure should not turn
        # a successful query into a 500)
        if query.conversation_id is not None:
            try:
                await self._conversation_store.add_turn(
                    query.conversation_id, query.question, answer
                )
            except Exception as exc:
                log.warning("conversation_turn_store_failed", error=str(exc))

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
            pipeline="advanced",
        )

        return QueryResponse(
            response_id=response_id,
            answer=answer,
            sources=sources,
            conversation_id=query.conversation_id,
            context_only=answer is None and not no_relevant_context,
            no_relevant_context=no_relevant_context,
        ), trace

    # -- execute_stream() --

    async def execute_stream(
        self,
        query: QueryRequest,
    ) -> AsyncIterator[QueryChunk]:
        """Return an async iterator that yields QueryChunk events for SSE streaming."""
        return self._stream(query)

    async def _stream(
        self,
        query: QueryRequest,
    ) -> AsyncGenerator[QueryChunk, None]:
        """Async generator for streaming response (SSE) with trace emission (DEBT-002)."""
        t_total = time.monotonic()
        response_id = uuid4()

        # Steps 0-6
        (
            steps,
            filtered,
            no_relevant_context,
            _effective_query,
            history,
        ) = await self._run_pre_llm_steps(query)

        if no_relevant_context or not filtered:
            yield QueryChunk(type="sources", data=[])
            # Emit trace before done (DEBT-002)
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

        # Step 7: Build prompt
        messages, selected_chunks, _, prompt_step = self._build_prompt(
            query, filtered, history
        )
        steps.append(prompt_step)

        # Step 8: Stream LLM tokens
        t0 = time.monotonic()
        full_answer_parts: list[str] = []
        llm_model = self._llm_config.provider
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
                    metadata={"model": llm_model},
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
            # Still emit trace on error
            trace = QueryTrace(
                response_id=response_id,
                steps=steps,
                total_duration_ms=_elapsed_ms(t_total),
                chunks_retrieved=[
                    ChunkRef(chunk_id=r.chunk_id, score=r.score) for r in filtered
                ],
                llm_model=llm_model,
                prompt_version=self._renderer.prompt_version,
                created_at=datetime.now(UTC),
            )
            yield QueryChunk(type="trace", data=_trace_to_dict(trace))
            yield QueryChunk(type="done", data="")
            return

        full_answer = "".join(full_answer_parts) or None

        # Step 9: Pre-response safeguard
        t0 = time.monotonic()
        sg_ctx = SafeguardContext(
            namespace=query.namespace,
            conversation_id=query.conversation_id,
        )
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

        # Save conversation turn (best-effort)
        if query.conversation_id is not None and full_answer:
            try:
                await self._conversation_store.add_turn(
                    query.conversation_id, query.question, full_answer
                )
            except Exception as exc:
                log.warning("conversation_turn_store_failed", error=str(exc))

        # Yield sources (only budget-selected chunks, not all filtered)
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
            llm_model=llm_model,
            prompt_version=self._renderer.prompt_version,
            created_at=datetime.now(UTC),
        )
        log.info(
            "query_stream_complete",
            response_id=str(response_id),
            duration_ms=trace.total_duration_ms,
            pipeline="advanced",
        )
        yield QueryChunk(type="trace", data=_trace_to_dict(trace))
        yield QueryChunk(type="done", data="")
