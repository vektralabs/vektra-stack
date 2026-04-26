# vektra-ingest

Document processing pipeline for the Vektra platform.

Handles document extraction (`DocumentExtractor` protocol), chunking (`ChunkingStrategy` protocol), and background job execution via arq with PostgreSQL job persistence.

## Extractors

- **pdfplumber** (default): native text extraction from PDFs, DOCX, PPTX. No external runtime dependencies.
- **Unstructured** (Phase 2, opt-in via `INSTALL_UNSTRUCTURED=true`): adds OCR support for scanned PDFs and 10-class element classification (titles, lists, tables, etc.). Heavier install footprint.

Provider selected via `VEKTRA_DOCUMENT_EXTRACTOR=pdfplumber|unstructured`.

## Chunking

- **Fixed-size** (default): token-based chunks with configurable overlap. Predictable and fast.
- **Dual strategy** (Phase 2): semantic splitting with table preservation, optional parent-child hierarchy. Activate via `VEKTRA_CHUNKING_STRATEGY=dual`.

Sparse embeddings (Phase 2, BM25 via `fastembed`) are computed during ingestion when `VEKTRA_SPARSE_EMBEDDING_PROVIDER` is set, enabling hybrid search at query time.

## Async ingest

Files larger than 10 MB are processed asynchronously via arq. The ingestion endpoint returns a `job_id`; clients poll `GET /api/v1/ingest/jobs/{job_id}/status` until completion. Phase-aware lifecycle (`processing`, `extracting`, `chunking`, `embedding`, `indexing`, `indexed`, `failed`) is captured in the job row.

Batch operations (`POST /api/v1/ingest/batch`, batch deletion) and document version tracking are also implemented in Phase 2.

See [architecture.md](../.s2s/architecture.md) for the component specification.
