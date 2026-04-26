# vektra-core

RAG engine, LLM abstraction, conversation management, and API gateway.

This is the primary deployable component of the Vektra platform. It provides the FastAPI HTTP API, implements `QueryPipeline`, manages API key authentication, and coordinates with `vektra-ingest` and `vektra-index` for document ingestion and retrieval.

## Source citations

Every source in a query response carries a `document_name` field, joined from `source_documents.filename`, so consumers (the chatbot widget, analytics, manual inspection) display human-readable labels instead of chunk UUIDs. Soft-deleted documents (REQ-057) keep their citation with an `(archived)` suffix so traceability is preserved when an answer references content that has since been removed.

The field propagates through both `SimpleQueryPipeline` and `AdvancedQueryPipeline`, in the JSON and SSE response paths.

See [architecture.md](../.s2s/architecture.md) for the component specification.
