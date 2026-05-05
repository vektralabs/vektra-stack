# vektra-ingest

Document processing pipeline for the Vektra platform.

Handles document extraction (`DocumentExtractor` protocol), chunking (`ChunkingStrategy` protocol), and background job execution via arq with PostgreSQL job persistence.

## Extractors

- **pdfplumber** (default): native text extraction from PDFs, DOCX, PPTX. No external runtime dependencies.
- **Unstructured** (Phase 2, opt-in via the `INSTALL_UNSTRUCTURED=true` Docker build arg, not a runtime env var): adds OCR support for scanned PDFs (Tesseract + Poppler) and 10-class element classification (text, title, list, table, image, header, footer, figure caption, page break, formula). Heavier install footprint.

Provider selected via `VEKTRA_DOCUMENT_EXTRACTOR=pdfplumber|unstructured`.

## Chunking

- **Fixed-size** (default): token-based chunks with configurable overlap. Predictable and fast.
- **Dual strategy** (Phase 2): semantic splitting with table preservation, optional parent-child hierarchy. Activate via `VEKTRA_CHUNKING_STRATEGY=dual`.

Sparse embeddings (Phase 2, BM25 via `fastembed`) are computed during ingestion when `VEKTRA_SPARSE_EMBEDDING_PROVIDER` is set, enabling hybrid search at query time.

## Async ingest

Files larger than 10 MB are processed asynchronously via arq. The ingestion endpoint returns a `job_id`; clients poll `GET /api/v1/ingest/jobs/{job_id}/status` until completion.

The job row distinguishes:

- **Status** — terminal-or-running state: `processing`, `indexed`, `failed`.
- **Phase** — fine-grained progress while `status=processing`: `extracting`, `chunking`, `embedding`. Cleared once the job reaches a terminal status.

Clients typically watch `status` to decide when to stop polling; `phase` and `percentage` drive progress UI.

Batch operations (`POST /api/v1/ingest/batch`, batch deletion) and document version tracking are also implemented in Phase 2.

See [architecture.md](../.s2s/architecture.md) for the component specification.
