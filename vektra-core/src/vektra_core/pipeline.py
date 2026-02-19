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
import time
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, AsyncIterator
from uuid import UUID, uuid4

import litellm
import structlog

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

from vektra_core.budget import allocate_token_budget
from vektra_core.conversation import ConversationStore
from vektra_core.templates import TemplateRenderer

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

    # Restore stable order (by original position in results)
    order = {r.chunk_id: i for i, r in enumerate(results)}
    return sorted(kept, key=lambda r: order.get(r.chunk_id, 0))


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
        try:
            return self._llm.count_tokens(text, self._llm_config.provider)
        except Exception:
            return max(1, len(text) // 4)

    def _context_window(self) -> int:
        try:
            return litellm.get_max_tokens(self._llm_config.provider) or _DEFAULT_CONTEXT_WINDOW
        except Exception:
            return _DEFAULT_CONTEXT_WINDOW

    async def _call_llm_with_fallback(
        self,
        messages: list[Message],
    ) -> tuple[str | None, str]:
        """Try primary LLM, then fallback model, then context-only.

        Returns (answer_text | None, model_used).
        None answer means context-only response (REQ-059, ARCH-043).
        """
        timeout_s = self._llm_config.fallback_timeout_ms / 1000.0

        # Primary model
        try:
            result = await asyncio.wait_for(
                self._llm.complete(messages, model=self._llm_config.provider),
                timeout=timeout_s,
            )
            return result.content, result.model
        except Exception as exc:
            log.warning(
                "llm_primary_failed",
                model=self._llm_config.provider,
                error=str(exc),
            )

        # Fallback model
        if self._llm_config.fallback_model:
            try:
                result = await asyncio.wait_for(
                    self._llm.complete(messages, model=self._llm_config.fallback_model),
                    timeout=timeout_s,
                )
                return result.content, result.model
            except Exception as exc:
                log.warning(
                    "llm_fallback_failed",
                    model=self._llm_config.fallback_model,
                    error=str(exc),
                )

        # Context-only: return chunks without LLM synthesis
        log.info("llm_context_only", model=self._llm_config.provider)
        return None, self._llm_config.provider

    async def execute(
        self,
        query: QueryRequest,
    ) -> tuple[QueryResponse, QueryTrace]:
        """Execute the full RAG pipeline, returning response + trace."""
        t_total = time.monotonic()
        response_id = uuid4()
        steps: list[StepTrace] = []

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
        steps.append(StepTrace(
            name="vector_search",
            duration_ms=_elapsed_ms(t0),
            metadata={"retrieved": len(results)},
        ))

        # Step 3: Retrieval filter
        t0 = time.monotonic()
        filtered = _apply_retrieval_filter(
            results,
            min_score=self._config.min_relevance_score,
            dedup_enabled=self._config.chunk_dedup_enabled,
        )
        no_relevant_context = len(results) > 0 and len(filtered) == 0
        steps.append(StepTrace(
            name="retrieval_filter",
            duration_ms=_elapsed_ms(t0),
            metadata={
                "before": len(results),
                "after": len(filtered),
                "no_relevant_context": no_relevant_context,
            },
        ))

        # Sources from filtered results (each gets a unique citation_id)
        sources = [
            SourceRef(
                doc_id=r.document_id,
                chunk_id=r.chunk_id,
                score=r.score,
                snippet=r.text_snippet,
                citation_id=uuid4(),
                document_version=r.document_version,
            )
            for r in filtered
        ]

        # No relevant context → skip LLM, return early
        if no_relevant_context:
            trace = QueryTrace(
                response_id=response_id,
                steps=steps,
                total_duration_ms=_elapsed_ms(t_total),
                chunks_retrieved=[],
                llm_model=self._llm_config.provider,
                prompt_version=self._renderer.prompt_version,
                created_at=datetime.now(timezone.utc),
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
                no_relevant_context=True,
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
            messages.append(Message(role="user", content=f"Previous conversation:\n{conv_text}"))
        messages.append(Message(
            role="user",
            content=f"Context:\n{context_text}\n\nQuestion: {query.question}",
        ))

        steps.append(StepTrace(
            name="build_prompt",
            duration_ms=_elapsed_ms(t0),
            metadata={
                "prompt_version": self._renderer.prompt_version,
                "chunks_in_prompt": len(selected_chunks),
                "history_turns_in_prompt": len(selected_history),
            },
        ))

        # Step 5: LLM call with graceful degradation
        t0 = time.monotonic()
        answer, llm_model = await self._call_llm_with_fallback(messages)
        steps.append(StepTrace(
            name="llm_call",
            duration_ms=_elapsed_ms(t0),
            metadata={"model": llm_model, "context_only": answer is None},
        ))

        # Step 6: Safeguard pre_response
        t0 = time.monotonic()
        sg_ctx = SafeguardContext(
            namespace=query.namespace,
            conversation_id=query.conversation_id,
        )
        sg_result = await self._safeguard.pre_response(str(response_id), sg_ctx)
        if not sg_result.allowed:
            answer = None
        elif sg_result.modified_content is not None:
            answer = sg_result.modified_content
        steps.append(StepTrace(
            name="safeguard",
            duration_ms=_elapsed_ms(t0),
            metadata={"allowed": sg_result.allowed},
        ))

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
            chunks_retrieved=[ChunkRef(chunk_id=r.chunk_id, score=r.score) for r in filtered],
            llm_model=llm_model,
            prompt_version=self._renderer.prompt_version,
            created_at=datetime.now(timezone.utc),
        )

        log.info(
            "query_complete",
            response_id=str(response_id),
            duration_ms=total_ms,
            llm_model=llm_model,
            chunks_retrieved=len(filtered),
            # No query text or response text (REQ-051)
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
        """Async generator for streaming response (SSE)."""
        response_id = uuid4()

        # Steps 1-3: embed, search, filter
        dense = await self._embedding.embed_query(query.question)
        results = await self._vector_store.search(
            namespace=query.namespace,
            query_embedding=QueryEmbedding(dense=dense),
            top_k=query.top_k,
            search_mode=query.search_mode,
            filters=query.filters,
        )
        filtered = _apply_retrieval_filter(
            results,
            min_score=self._config.min_relevance_score,
            dedup_enabled=self._config.chunk_dedup_enabled,
        )
        no_relevant_context = len(results) > 0 and len(filtered) == 0

        if no_relevant_context or not filtered:
            yield QueryChunk(type="sources", data=[])
            yield QueryChunk(type="done", data="")
            return

        # Step 4: Build prompt
        history: list[dict[str, Any]] = []
        if query.conversation_id is not None:
            history = await self._conversation_store.get_history(query.conversation_id)

        system_text = self._renderer.render_system(namespace=query.namespace)
        context_text = self._renderer.render_context(
            [{"text": r.text_snippet, "score": r.score} for r in filtered]
        )
        conv_text = self._renderer.render_conversation(history)

        messages: list[Message] = [Message(role="system", content=system_text)]
        if conv_text.strip():
            messages.append(Message(role="user", content=f"Previous conversation:\n{conv_text}"))
        messages.append(Message(
            role="user",
            content=f"Context:\n{context_text}\n\nQuestion: {query.question}",
        ))

        # Step 5: Safeguard pre_response (before streaming)
        sg_ctx = SafeguardContext(
            namespace=query.namespace,
            conversation_id=query.conversation_id,
        )
        sg_result = await self._safeguard.pre_response(str(response_id), sg_ctx)
        if not sg_result.allowed:
            yield QueryChunk(type="error", data="Request blocked by safeguard")
            return

        # Step 6: Stream LLM tokens
        full_answer_parts: list[str] = []
        try:
            token_stream = await self._llm.stream(messages, model=self._llm_config.provider)
            async for chunk in token_stream:
                if chunk.content:
                    full_answer_parts.append(chunk.content)
                    yield QueryChunk(type="token", data=chunk.content)
        except Exception as exc:
            log.warning("stream_llm_failed", error=str(exc))
            yield QueryChunk(type="error", data="LLM unavailable")
            return

        # Save conversation turn
        full_answer = "".join(full_answer_parts) or None
        if query.conversation_id is not None and full_answer:
            await self._conversation_store.add_turn(
                query.conversation_id, query.question, full_answer
            )

        # Yield sources + done
        sources_data = [
            {
                "doc_id": str(r.document_id),
                "chunk_id": r.chunk_id,
                "score": r.score,
                "snippet": r.text_snippet,
                "citation_id": str(uuid4()),
                "document_version": r.document_version,
            }
            for r in filtered
        ]
        yield QueryChunk(type="sources", data=sources_data)
        yield QueryChunk(type="done", data="")
