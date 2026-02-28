# vektra-core

RAG engine, LLM abstraction, conversation management, and API gateway.

This is the primary deployable component of the Vektra platform. It provides the FastAPI HTTP API, implements `QueryPipeline`, manages API key authentication, and coordinates with `vektra-ingest` and `vektra-index` for document ingestion and retrieval.

See [architecture.md](../.s2s/architecture.md) for the component specification.
