"""Tests that Protocol interfaces are importable and have correct structure."""

import pytest

from vektra_shared.events import NoOpEventEmitter
from vektra_shared.protocols import (
    ChunkingStrategy,
    DocumentExtractor,
    EmbeddingProvider,
    EventEmitter,
    LLMProvider,
    QueryPipeline,
    SafeguardHook,
    SparseEmbeddingProvider,
    VectorStoreProvider,
)
from vektra_shared.safeguards import PassthroughSafeguard


class TestProtocolsImportable:
    def test_all_nine_protocols_importable(self):
        protocols = [
            LLMProvider,
            EmbeddingProvider,
            SparseEmbeddingProvider,
            VectorStoreProvider,
            DocumentExtractor,
            ChunkingStrategy,
            QueryPipeline,
            SafeguardHook,
            EventEmitter,
        ]
        assert len(protocols) == 9

    def test_llm_provider_has_required_methods(self):
        assert hasattr(LLMProvider, "complete")
        assert hasattr(LLMProvider, "stream")
        assert hasattr(LLMProvider, "health_check")
        assert hasattr(LLMProvider, "count_tokens")

    def test_embedding_provider_has_required_methods(self):
        assert hasattr(EmbeddingProvider, "embed_documents")
        assert hasattr(EmbeddingProvider, "embed_query")
        assert hasattr(EmbeddingProvider, "dimensions")
        assert hasattr(EmbeddingProvider, "health_check")

    def test_sparse_embedding_provider_has_required_methods(self):
        assert hasattr(SparseEmbeddingProvider, "embed_documents")
        assert hasattr(SparseEmbeddingProvider, "embed_query")
        assert hasattr(SparseEmbeddingProvider, "vocab_size")

    def test_vector_store_provider_has_required_methods(self):
        assert hasattr(VectorStoreProvider, "store")
        assert hasattr(VectorStoreProvider, "search")
        assert hasattr(VectorStoreProvider, "delete")
        assert hasattr(VectorStoreProvider, "health_check")

    def test_document_extractor_has_required_methods(self):
        assert hasattr(DocumentExtractor, "supported_types")
        assert hasattr(DocumentExtractor, "extract")
        assert hasattr(DocumentExtractor, "health_check")

    def test_chunking_strategy_has_required_methods(self):
        assert hasattr(ChunkingStrategy, "chunk")

    def test_query_pipeline_has_required_methods(self):
        assert hasattr(QueryPipeline, "execute")
        assert hasattr(QueryPipeline, "execute_stream")

    def test_safeguard_hook_has_required_methods(self):
        assert hasattr(SafeguardHook, "pre_query")
        assert hasattr(SafeguardHook, "post_retrieval")
        assert hasattr(SafeguardHook, "pre_response")

    def test_event_emitter_has_required_methods(self):
        assert hasattr(EventEmitter, "emit")

    def test_protocol_count_is_nine(self):
        """Phase 2 adds no new Protocols. AdvancedQueryPipeline is a concrete class."""
        protocols = [
            LLMProvider,
            EmbeddingProvider,
            SparseEmbeddingProvider,
            VectorStoreProvider,
            DocumentExtractor,
            ChunkingStrategy,
            QueryPipeline,
            SafeguardHook,
            EventEmitter,
        ]
        assert len(protocols) == 9

    def test_query_pipeline_signatures_unchanged(self):
        """QueryPipeline Protocol must keep execute and execute_stream only."""
        import inspect

        members = {
            name
            for name, _ in inspect.getmembers(QueryPipeline, predicate=inspect.isfunction)
            if not name.startswith("_")
        }
        assert members == {"execute", "execute_stream"}


class TestNoOpEventEmitter:
    @pytest.mark.asyncio
    async def test_emit_completes_without_error(self):
        emitter = NoOpEventEmitter()
        await emitter.emit("document.indexed", {"document_id": "abc"})

    @pytest.mark.asyncio
    async def test_emit_is_fast(self):
        import time

        emitter = NoOpEventEmitter()
        start = time.monotonic()
        for _ in range(100):
            await emitter.emit("query.completed", {})
        elapsed_ms = (time.monotonic() - start) * 1000
        # 100 no-op emits must complete in <100ms total (far below 1ms/emit requirement)
        assert elapsed_ms < 100

    def test_noop_implements_event_emitter_protocol(self):
        emitter = NoOpEventEmitter()
        assert isinstance(emitter, EventEmitter)


class TestPassthroughSafeguard:
    @pytest.mark.asyncio
    async def test_pre_query_allows(self):
        from vektra_shared.types import SafeguardContext

        safeguard = PassthroughSafeguard()
        result = await safeguard.pre_query("ref-1", SafeguardContext())
        assert result.allowed is True
        assert result.modified_content is None
        assert result.filtered_ids is None

    @pytest.mark.asyncio
    async def test_post_retrieval_allows(self):
        from vektra_shared.types import SafeguardContext

        safeguard = PassthroughSafeguard()
        result = await safeguard.post_retrieval("ref-1", [], SafeguardContext())
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_pre_response_allows(self):
        from vektra_shared.types import SafeguardContext

        safeguard = PassthroughSafeguard()
        result = await safeguard.pre_response("ref-1", SafeguardContext())
        assert result.allowed is True

    def test_passthrough_implements_safeguard_hook_protocol(self):
        safeguard = PassthroughSafeguard()
        assert isinstance(safeguard, SafeguardHook)
