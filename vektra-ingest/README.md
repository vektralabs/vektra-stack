# vektra-ingest

Document processing pipeline for the Vektra platform.

Handles PDF, Word, and PowerPoint extraction via the `DocumentExtractor` protocol, text chunking via the `ChunkingStrategy` protocol, and background job execution via arq.

See [architecture.md](../.s2s/architecture.md) for the component specification.
