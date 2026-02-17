# Validation scenarios

**Project**: Vektra
**Version**: 0.3
**Date**: 2026-02-17
**Status**: Draft, pending roundtable refinement (QA Lead + Business Analyst)

## Purpose

End-to-end validation scenarios that verify Vektra's functional completeness from the Platform Operator perspective. Each scenario combines:

- **Cockburn use case** structure (actor, trigger, preconditions, flow, outcome)
- **BDD acceptance criteria** (Given/When/Then)
- **arc42 quality scenario** traceability (REQ + ARCH references)

These scenarios serve as:
1. Pre-implementation completeness check (do specs + architecture cover all operator workflows?)
2. Integration test specification (each scenario maps to one or more automated tests)
3. Acceptance criteria for Phase 1 delivery

## Format

```
SC-{CAT}{NN}: [Title]
Actor: [who triggers the scenario]
Trigger: [initiating event]
Preconditions: [required system state]
Flow:
  1. [step]
  2. [step]
Expected outcome: [observable behavior]
Acceptance criteria:
  - Given [context], When [action], Then [measurable result]
Traceability: REQ-xxx, ARCH-xxx
Phase: 1 | 2
```

---

## A. Document lifecycle (ingest)

### SC-A01: Single PDF ingestion with query verification

**Actor**: Platform Operator
**Trigger**: Operator ingests a text-based PDF document
**Preconditions**: Vektra stack running, health check passing, LLM provider configured

**Flow**:
1. Operator calls POST /ingest with a 5-page text PDF (<10MB)
2. System detects content type via magic bytes (application/pdf)
3. System extracts text via PdfplumberExtractor
4. System chunks text (1000 tokens, 200 overlap)
5. System generates embeddings via EmbeddingProvider
6. System stores chunks in VectorStoreProvider with metadata
7. System returns 200 with document_id and chunk count
8. Operator calls POST /query with a question answerable from the document
9. System returns answer with source citations referencing the ingested document

**Expected outcome**: Document is indexed and queryable. Response includes structured citations with document_id, chunk_id, relevance_score, and text_snippet.

**Acceptance criteria**:
- Given a 5-page text PDF, When POST /ingest is called, Then response is 200 within 30s with document_id and status "indexed"
- Given the document is indexed, When POST /query asks about its content, Then response includes at least one citation with the document's document_id
- Given the response, Then each citation includes document_id (UUID), chunk_id, relevance_score (0.0-1.0), text_snippet (first 200 chars)

**Traceability**: REQ-002, REQ-003, REQ-016, REQ-034, REQ-058, ARCH-030, ARCH-036, ARCH-042, ARCH-044
**Phase**: 1

---

### SC-A02: Incremental daily ingestion (n8n pattern)

**Actor**: n8n workflow (external orchestrator)
**Trigger**: Daily scheduled workflow triggers document ingestion
**Preconditions**: Vektra stack running, existing documents already indexed

**Flow**:
1. n8n workflow calls POST /ingest with a new PDF (scoped API key with ingest scope)
2. System validates API key and scope
3. System computes SHA-256 content hash
4. System confirms document is new (hash not in index)
5. System extracts, chunks, embeds, stores
6. System returns 200 with document_id
7. n8n workflow repeats for next document in batch (sequential, single-document per call)

**Expected outcome**: New documents are added alongside existing ones. Existing documents remain queryable. No re-processing of already-indexed documents.

**Acceptance criteria**:
- Given 5 existing documents, When a 6th document is ingested, Then GET /stats shows document_count=6
- Given a document was ingested yesterday, When POST /query references its content, Then citations still include yesterday's document
- Given the ingestion API key has ingest scope, Then POST /ingest succeeds (Phase 1: all scopes permitted)

**Traceability**: REQ-002, REQ-024, REQ-034, ARCH-016, ARCH-017
**Phase**: 1

---

### SC-A03: Mixed format ingestion (PDF + Word + PowerPoint)

**Actor**: Platform Operator
**Trigger**: Operator ingests documents of different formats in sequence
**Preconditions**: Vektra stack running

**Flow**:
1. Operator calls POST /ingest with a PDF document
2. System detects application/pdf via magic bytes, dispatches to PdfplumberExtractor
3. Operator calls POST /ingest with a .docx document
4. System detects application/vnd.openxmlformats-officedocument.wordprocessingml.document, dispatches to WordExtractor
5. Operator calls POST /ingest with a .pptx document
6. System detects application/vnd.openxmlformats-officedocument.presentationml.presentation, dispatches to PowerPointExtractor
7. Operator calls POST /query with a question that requires content from all three documents

**Expected outcome**: All three formats are indexed and queryable. Citations may reference any of the three documents.

**Acceptance criteria**:
- Given PDF, DOCX, PPTX uploaded in sequence, When POST /query is called, Then response may include citations from any of the three formats
- Given a .docx with headings and paragraphs, Then text is extracted preserving reading order
- Given a .pptx with slide titles and text boxes, Then text is extracted in slide order including speaker notes

**Traceability**: REQ-002, REQ-045, REQ-046, REQ-058, ARCH-042
**Phase**: 1

---

### SC-A04: Duplicate document detection (same content, different filename)

**Actor**: Platform Operator
**Trigger**: Operator ingests the same content with a different filename
**Preconditions**: Document already indexed

**Flow**:
1. Operator calls POST /ingest with "report-v1.pdf"
2. System indexes document, returns document_id and content_hash
3. Operator calls POST /ingest with "report-copy.pdf" (identical content, different filename)
4. System computes SHA-256, finds matching hash
5. System returns 200 with existing document_id and status "exists"
6. Operator calls POST /ingest with "report-v2.pdf" (same filename prefix, different content)
7. System computes SHA-256, finds no match for hash but filename collision
8. System returns 409 Conflict with existing document_id and both content hashes

**Expected outcome**: Identical content is deduplicated. Different content with same filename is rejected with 409.

**Acceptance criteria**:
- Given document A indexed, When same content uploaded as different filename, Then 200 with status "exists" and original document_id
- Given document A indexed, When different content uploaded as same filename, Then 409 with existing document_id and both content hashes
- Given deduplication, Then audit log records "document_deduplicated" action

**Traceability**: REQ-033, REQ-034, REQ-035, BR-005
**Phase**: 1

---

### SC-A05: Document update (new version supersedes old)

**Actor**: Platform Operator
**Trigger**: Operator re-ingests a document after content update
**Preconditions**: Original document already indexed, Phase 1 behavior

**Flow**:
1. Operator ingests "report.pdf" (v1), system indexes it
2. Operator updates the PDF content (new content, same filename)
3. Operator calls POST /ingest with updated "report.pdf"
4. System computes SHA-256, detects same filename but different hash
5. Phase 1: system returns 409 Conflict (version management not active)
6. Operator deletes old document via DELETE /documents/{id}
7. Operator re-ingests updated "report.pdf"
8. System indexes new content

**Expected outcome**: Phase 1 requires manual delete-then-reingest for document updates. Version and supersedes_id fields exist in schema but version always 1.

**Acceptance criteria**:
- Given document indexed, When same filename with different content is uploaded, Then 409 Conflict returned
- Given old document deleted, When new content uploaded with same filename, Then new document_id assigned with version=1
- Given SourceDocument schema, Then version (default 1) and supersedes_id (null) columns exist

**Traceability**: REQ-033, REQ-056, REQ-057, ARCH-040
**Phase**: 1 (version management Phase 2)

---

### SC-A06: Document removal with search verification

**Actor**: Platform Operator
**Trigger**: Operator removes a document and verifies it no longer appears in search results
**Preconditions**: Document indexed and previously returned in query results

**Flow**:
1. Operator calls POST /query, confirms document appears in citations
2. Operator calls DELETE /documents/{id} with namespace parameter
3. System sets deleted_at timestamp and deletion_reason="user_request" (soft delete)
4. System returns 200
5. Operator calls POST /query with same question
6. Document no longer appears in citations
7. Operator calls POST /ingest with same file content
8. System treats as new document (dedup checks exclude soft-deleted)

**Expected outcome**: Soft-deleted document is excluded from all search results and deduplication checks.

**Acceptance criteria**:
- Given document indexed, When DELETE /documents/{id} called, Then deleted_at set, document excluded from search
- Given soft-deleted document, When same content re-ingested, Then new document_id created (not deduplicated)
- Given deletion, Then audit log records deletion event with document_id and reason

**Traceability**: REQ-057, REQ-033, ARCH-040
**Phase**: 1

---

### SC-A07: Boundary conditions (size limit, scanned PDF)

**Actor**: Platform Operator
**Trigger**: Operator attempts ingestion at boundary conditions
**Preconditions**: Vektra stack running

**Flow**:
1. Operator calls POST /ingest with a 10MB PDF (boundary, inclusive)
2. System processes synchronously, returns 200
3. Operator calls POST /ingest with a 15MB PDF (above sync threshold)
4. System returns 202 Accepted with job_id
5. Operator polls GET /ingest/jobs/{id}/status until "indexed"
6. Operator calls POST /ingest with a 50MB PDF (at maximum)
7. System processes asynchronously, returns 202
8. Operator calls POST /ingest with a 51MB PDF (over maximum)
9. System returns 400 with ERR-INGEST-002 and size limit in message
10. Operator calls POST /ingest with a scanned PDF (<100 chars/page average)
11. System returns 400 with ERR-INGEST-003

**Expected outcome**: Clear behavior at each boundary. Scanned PDFs detected and rejected before chunking (fail fast).

**Acceptance criteria**:
- Given 10MB PDF, When POST /ingest, Then synchronous 200 response
- Given 15MB PDF, When POST /ingest, Then 202 with job_id, polling returns "indexed"
- Given 51MB PDF, When POST /ingest, Then 400 with ERR-INGEST-002, remediation includes size limit
- Given scanned PDF (<100 chars/page average), When POST /ingest, Then 400 with ERR-INGEST-003, detection before chunking

**Traceability**: REQ-016, REQ-029, REQ-011, BR-003
**Phase**: 1

---

### SC-A08: Corrupt or unsupported format document

**Actor**: Platform Operator
**Trigger**: Operator attempts ingestion with invalid file
**Preconditions**: Vektra stack running

**Flow**:
1. Operator calls POST /ingest with a .exe file renamed to .pdf
2. System detects actual MIME type via magic bytes (not application/pdf)
3. System returns 400 with ERR-INGEST-001, detected MIME type in message
4. Operator calls POST /ingest with a corrupted PDF (truncated file)
5. System attempts extraction, PdfplumberExtractor fails
6. System returns 400 with ERR-INGEST-001
7. Operator calls POST /ingest with a password-protected .docx
8. WordExtractor returns error
9. System returns 400 with ERR-INGEST-001

**Expected outcome**: Invalid files rejected with clear error codes. Magic bytes detection prevents mislabeled file processing.

**Acceptance criteria**:
- Given .exe renamed to .pdf, When POST /ingest, Then 400 with ERR-INGEST-001 including detected MIME type
- Given corrupted PDF, When POST /ingest, Then 400 with ERR-INGEST-001
- Given password-protected .docx, When POST /ingest, Then 400 with ERR-INGEST-001
- Given mismatch between extension and magic bytes, Then warning logged

**Traceability**: REQ-016, REQ-045, REQ-058, ARCH-042
**Phase**: 1

---

### SC-A09: Low-level chunk storage via direct API (pre-embedded content)

**Actor**: Downstream application with custom embedding pipeline
**Trigger**: External system provides pre-computed embedding vectors for direct storage
**Preconditions**: Source document record exists via prior POST /ingest (Phase 1: the only way to create a source_documents row; chunks at index_version=1 already stored)

**Flow**:
1. External pipeline re-embeds the same document content using a custom model
2. Operator calls POST /documents/{id}/chunks with namespace and list of ChunkEmbedding objects (index_version=2)
3. Each ChunkEmbedding includes: chunk_id, text, embedding_vector, metadata, position
4. System stores chunks directly in VectorStoreProvider without re-embedding
5. System returns 201 with document_id, chunk_count, index_version
6. Operator calls POST /search or POST /query
7. Stored chunks appear in results with correct document_id

**Expected outcome**: Custom embedding pipelines bypass Vektra's extraction and embedding steps. Only the vector store write and query path used.

**Acceptance criteria**:
- Given valid document_id and pre-embedded chunks, When POST /documents/{id}/chunks, Then 201 with chunk_count matching submitted count
- Given stored chunks, When POST /query, Then chunks appear in sources with correct document_id
- Given unknown document_id, When POST /documents/{id}/chunks, Then 404
- Given embedding vector of wrong dimension, When POST /documents/{id}/chunks, Then 422 with remediation including expected dimension
- Given POST /documents/{id}/chunks, Then ingest scope required (query scope returns 403)

**Traceability**: REQ-012, ARCH-051
**Phase**: 1

---

## B. Query lifecycle

### SC-B01: Simple RAG query with verifiable citations

**Actor**: Downstream application (or Platform Operator testing)
**Trigger**: Client submits a question about indexed content
**Preconditions**: At least one document indexed, LLM provider configured and reachable

**Flow**:
1. Client calls POST /query with question text and namespace="default"
2. System validates API key (auth middleware)
3. SafeguardHook.pre_query() runs (Phase 1: pass-through)
4. EmbeddingProvider.embed_query() generates query embedding
5. VectorStoreProvider.search() retrieves top-k chunks (filtered by index_version)
6. Retrieval filter applies relevance threshold and overlap deduplication
7. Token budget allocates model context window
8. Jinja2 templates render prompt (system + context + question)
9. LLMProvider.complete() generates answer
10. SafeguardHook.pre_response() runs (Phase 1: pass-through)
11. System returns QueryResponse with response_id, answer, sources, conversation_id=null

**Expected outcome**: Response includes answer grounded in indexed content, with structured source citations.

**Acceptance criteria**:
- Given indexed documents, When POST /query, Then response includes response_id (UUID) and answer text
- Given the response, Then sources array contains at least one SourceRef with doc_id, chunk_id, score, snippet, citation_id
- Given the response, Then QueryTrace emitted via structlog with response_id, steps, total_duration_ms, prompt_version

**Traceability**: REQ-003, REQ-055, REQ-060, ARCH-036, ARCH-041, ARCH-054, ARCH-055, ARCH-056
**Phase**: 1

---

### SC-B02: Multi-turn conversation (context preserved)

**Actor**: Downstream application
**Trigger**: Client continues a conversation over multiple turns
**Preconditions**: Documents indexed, LLM provider reachable

**Flow**:
1. Client calls POST /query with question, no conversation_id
2. System creates new conversation, returns response with conversation_id
3. Client calls POST /query with follow-up question and same conversation_id
4. System loads encrypted conversation history
5. System includes previous turns in LLM prompt (via conversation.j2 template)
6. Token budget allocates history within remaining budget after system + question + reserve + chunks
7. System returns response maintaining conversation context
8. Client continues for up to 10 turns
9. At turn 11, oldest turns excluded from context (sliding window, first turn preserved)

**Expected outcome**: Conversation context maintained across turns. Token budget manages history inclusion. Conversation stored encrypted.

**Acceptance criteria**:
- Given no conversation_id, When POST /query, Then new conversation_id returned in response
- Given conversation_id from previous turn, When POST /query with follow-up, Then response reflects conversational context
- Given 10 turns, When 11th turn submitted, Then first turn preserved, middle turns excluded with "[N previous turns omitted]" marker
- Given conversation_id, Then conversation content stored encrypted (pgcrypto)

**Traceability**: REQ-049, ARCH-031, ARCH-054, ARCH-055
**Phase**: 1 (in-memory storage, lost on restart)

---

### SC-B03: Query with namespace filtering

**Actor**: Downstream application
**Trigger**: Client queries within a specific namespace
**Preconditions**: Documents indexed in namespace "default" and namespace "project-x"

**Flow**:
1. Operator creates namespace "project-x" (if not exists)
2. Operator ingests documents into namespace "project-x"
3. Operator ingests different documents into namespace "default"
4. Client calls POST /query with namespace="project-x"
5. VectorStoreProvider.search() filters by namespace="project-x"
6. Only chunks from "project-x" namespace are retrieved
7. Response cites only documents from "project-x"

**Expected outcome**: Query results scoped to the specified namespace. No cross-namespace leakage.

**Acceptance criteria**:
- Given documents in namespace "project-x" and "default", When POST /query with namespace="project-x", Then citations only from "project-x" documents
- Given namespace "project-x", Then no results from "default" namespace appear

**Traceability**: REQ-048, ARCH-007, ARCH-047
**Phase**: 1

---

### SC-B04: Query with metadata filtering

**Actor**: Downstream application
**Trigger**: Client queries with metadata filters
**Preconditions**: Documents indexed with varying metadata (content_type, language)

**Flow**:
1. Operator ingests PDF with metadata {content_type: "lecture", language: "en"}
2. Operator ingests PDF with metadata {content_type: "exam", language: "it"}
3. Client calls POST /query with filters={language: "en"}
4. VectorStoreProvider.search() applies JSONB WHERE clause with GIN index
5. Only chunks with language="en" are retrieved
6. Response cites only English documents

**Expected outcome**: Metadata filters combined with vector similarity in single SQL query. Filtered results only.

**Acceptance criteria**:
- Given documents with different languages, When POST /query with filters={language: "en"}, Then only English documents cited
- Given documents with different content_types, When POST /query with filters={content_type: "lecture"}, Then only lecture documents cited
- Given filters, Then SQL query combines vector similarity + JSONB WHERE in single query (not post-filtering)

**Traceability**: REQ-063, ARCH-044
**Phase**: 1

---

### SC-B05: No relevant context detected

**Actor**: Downstream application
**Trigger**: Client asks a question not covered by indexed content
**Preconditions**: Documents indexed, but content does not relate to the query

**Flow**:
1. Client calls POST /query with question completely unrelated to indexed content
2. EmbeddingProvider generates query embedding
3. VectorStoreProvider.search() returns top-k chunks
4. Retrieval filter applies minimum relevance threshold (VEKTRA_MIN_RELEVANCE_SCORE=0.3)
5. All chunks score below threshold
6. System sets QueryResponse.no_relevant_context=True
7. System returns response with answer explaining no relevant information found, empty sources

**Expected outcome**: System explicitly signals "no relevant information" instead of synthesizing from irrelevant context.

**Acceptance criteria**:
- Given unrelated query, When all chunks below relevance threshold, Then no_relevant_context=True in response
- Given no_relevant_context=True, Then answer is null or explains lack of relevant content
- Given no_relevant_context=True, Then sources array is empty
- Given retrieval filter, Then StepTrace for retrieval_filter records chunks_before, chunks_after, threshold

**Traceability**: REQ-003, ARCH-043, ARCH-056
**Phase**: 1

---

### SC-B06: Query on empty index

**Actor**: Platform Operator (testing fresh deployment)
**Trigger**: Client queries before any documents are ingested
**Preconditions**: Vektra stack running, no documents indexed

**Flow**:
1. Client calls POST /query with a question
2. System detects empty index
3. System returns 200 with helpful message guiding operator

**Expected outcome**: Not an error. Helpful guidance returned instead of cryptic failure.

**Acceptance criteria**:
- Given no documents indexed, When POST /query, Then 200 with message "No documents indexed. Ingest documents using POST /ingest first."
- Given empty index response, Then results array is empty (not error status)

**Traceability**: REQ-028, REQ-003
**Phase**: 1

---

### SC-B07: Streaming response (SSE)

**Actor**: Downstream application
**Trigger**: Client requests streaming response
**Preconditions**: Documents indexed, LLM provider reachable

**Flow**:
1. Client calls POST /query with Accept: text/event-stream
2. System processes query through pipeline
3. System calls LLMProvider.stream() instead of complete()
4. System emits SSE events: token chunks as type="token", sources as type="sources", trace as type="trace", final type="done"
5. Client receives first token within 2 seconds
6. If client disconnects mid-stream, system terminates LLM request

**Expected outcome**: Real-time streaming of response tokens via SSE. Clean connection handling.

**Acceptance criteria**:
- Given Accept: text/event-stream, When POST /query, Then response is SSE stream
- Given streaming, Then first token delivered within 2s of request
- Given client disconnect, Then LLM request terminated (no orphan processing)
- Given LLM error mid-stream, Then error event emitted before stream close
- Given Accept: application/json, When POST /query, Then complete response returned (non-streaming)

**Traceability**: REQ-042, ARCH-036
**Phase**: 1

---

### SC-B08: Concurrent queries (load test)

**Actor**: 10 concurrent downstream applications
**Trigger**: 10 simultaneous query requests
**Preconditions**: Documents indexed, LLM provider reachable

**Flow**:
1. 10 clients call POST /query concurrently with different questions
2. System processes all queries through SimpleQueryPipeline
3. EmbeddingProvider handles concurrent embedding requests
4. VectorStoreProvider handles concurrent search operations
5. LLMProvider handles concurrent LLM requests
6. All 10 responses returned within acceptable latency

**Expected outcome**: System handles concurrent load without crash. Latency degradation within bounds.

**Acceptance criteria**:
- Given 10 concurrent queries sustained 60s, Then no crashes or errors
- Given 10 concurrent queries, Then p95 latency < 2x p95 at 1 concurrent

**Traceability**: NFR-011
**Phase**: 1 (TARGET, not blocking)

---

### SC-B09: Standalone semantic search without LLM synthesis

**Actor**: Downstream application
**Trigger**: Client retrieves relevant chunks by semantic similarity without LLM completion
**Preconditions**: Documents indexed in the target namespace

**Flow**:
1. Client calls POST /search with query text and namespace="default"
2. System validates API key (query scope)
3. EmbeddingProvider.embed_query() generates query embedding
4. VectorStoreProvider.search() retrieves top_k chunks by cosine similarity
5. System returns SearchResponse with ranked results
6. No LLM call made, no safeguard hooks invoked, no token budget applied

**Expected outcome**: Fast search response with ranked chunks and scores. Total latency is embed + vector search only (NFR-002 target: p95 <500ms).

**Acceptance criteria**:
- Given indexed documents, When POST /search, Then SearchResponse returned with results list (may be empty)
- Given POST /search, Then no LLM call made (search path bypasses QueryPipeline)
- Given POST /search, Then relevance threshold (VEKTRA_MIN_RELEVANCE_SCORE) not applied (returns raw top_k)
- Given empty index, When POST /search, Then 200 with empty results list (consistent with SC-B06: no documents indexed is not an error)
- Given SearchFilters with language="en", When POST /search, Then results scoped to matching chunks
- Given POST /search, Then total_available = len(results) in Phase 1 (ANN does not count total)

**Traceability**: REQ-012, ARCH-044
**Phase**: 1

---

### SC-B10: Query rejected when text exceeds maximum length

**Actor**: Downstream application
**Trigger**: Client submits query with oversized question text
**Preconditions**: Vektra stack running (no documents needed: error occurs before any retrieval)

**Flow**:
1. Client calls POST /query with question text exceeding the configured maximum length
2. System validates request before generating embeddings
3. System returns 400 with ERR-QUERY-003
4. Error details include max_length and actual_length
5. No embedding generated, no LLM call made (fail fast)

**Expected outcome**: Request rejected early. Clear remediation includes the maximum character count. No wasted compute.

**Acceptance criteria**:
- Given question text exceeding max length, When POST /query, Then 400 with ERR-QUERY-003
- Given ERR-QUERY-003, Then remediation includes maximum character limit and actual character count
- Given ERR-QUERY-003, Then category="PERMANENT" (not retryable)
- Given question within max length, Then request proceeds normally
- Given ERR-QUERY-003, Then no EmbeddingProvider or LLMProvider call made

**Traceability**: REQ-013, ARCH-036
**Phase**: 1

---

## C. Pipeline quality controls

### SC-C01: Relevance threshold filtering

**Actor**: (internal pipeline behavior)
**Trigger**: Query execution with chunks of varying relevance
**Preconditions**: Documents indexed, some tangentially related to query

**Flow**:
1. Query submitted
2. VectorStoreProvider.search() returns top-k chunks with scores
3. Retrieval filter checks each chunk against VEKTRA_MIN_RELEVANCE_SCORE (default 0.3)
4. Chunks below threshold excluded from prompt
5. Remaining chunks passed to build_prompt step
6. StepTrace for retrieval_filter records chunks_before, chunks_after, threshold

**Expected outcome**: Low-relevance chunks excluded from LLM context, improving response quality.

**Acceptance criteria**:
- Given chunks with scores [0.8, 0.5, 0.2, 0.1], When threshold=0.3, Then only [0.8, 0.5] included in prompt
- Given retrieval filter, Then StepTrace metadata includes chunks_before=4, chunks_after=2, threshold=0.3
- Given all chunks below threshold, Then no_relevant_context=True (see SC-B05)

**Traceability**: ARCH-056, ARCH-036
**Phase**: 1

---

### SC-C02: Overlap deduplication

**Actor**: (internal pipeline behavior)
**Trigger**: Adjacent chunks from fixed-size chunking with overlap
**Preconditions**: Document chunked with 200-token overlap (REQ-016)

**Flow**:
1. Query submitted
2. VectorStoreProvider.search() returns chunks including two overlapping chunks from same document (adjacent positions)
3. Retrieval filter detects overlap (same document_id, adjacent positions)
4. Higher-scored chunk retained, lower-scored removed
5. Deduplicated set passed to build_prompt

**Expected outcome**: Redundant overlapping content removed from prompt, maximizing unique context.

**Acceptance criteria**:
- Given chunks at positions 3 and 4 from same document with scores 0.7 and 0.6, When dedup runs, Then only position 3 (score 0.7) retained
- Given chunks from different documents at same positions, Then no deduplication (different documents)

**Traceability**: ARCH-056
**Phase**: 1

---

### SC-C03: Token budget allocation

**Actor**: (internal pipeline behavior)
**Trigger**: Query with conversation history and multiple chunks
**Preconditions**: Conversation active, multiple relevant chunks retrieved

**Flow**:
1. Query submitted with conversation_id (8 previous turns)
2. Model context window: 4096 tokens (example)
3. System prompt: 200 tokens (fixed)
4. User question: 50 tokens (fixed)
5. Response reserve: 1024 tokens (VEKTRA_RESPONSE_TOKEN_RESERVE)
6. Remaining: 4096 - 200 - 50 - 1024 = 2822 tokens
7. Chunk budget: 2822 * 0.6 = 1693 tokens (VEKTRA_CONTEXT_CHUNK_RATIO)
8. History budget: 2822 - 1693 = 1129 tokens
9. Chunks that exceed chunk budget excluded (retained in QueryTrace)
10. History turns that exceed history budget replaced with "[N previous turns omitted]"

**Expected outcome**: Prompt never exceeds model context window. Budget diagnostics recorded.

**Acceptance criteria**:
- Given budget allocation, Then system prompt + question + reserve always included (non-negotiable)
- Given 5 chunks totaling 2000 tokens and chunk budget 1693, Then furthest chunks excluded
- Given 8 history turns and insufficient history budget, Then oldest turns (except first) replaced with marker
- Given build_prompt step, Then StepTrace metadata includes model_context_window, prompt_tokens, chunks_included, chunks_excluded, history_turns_included, history_turns_excluded, budget_utilization

**Traceability**: ARCH-055, ARCH-054
**Phase**: 1

---

### SC-C04: Custom prompt template

**Actor**: Platform Operator
**Trigger**: Operator customizes system prompt template
**Preconditions**: Vektra stack running with default templates

**Flow**:
1. Operator copies built-in templates to custom directory
2. Operator modifies system.j2 to change LLM behavior (e.g., "Always respond in Italian")
3. Operator sets VEKTRA_PROMPT_TEMPLATES_DIR to custom directory
4. Operator restarts Vektra (template loaded during startup validation step 8)
5. Operator calls POST /query
6. LLM responds according to custom system prompt (in Italian)

**Expected outcome**: Custom templates override defaults. Template change reflected in LLM behavior.

**Acceptance criteria**:
- Given custom system.j2 with "respond in Italian", When POST /query in English, Then response in Italian
- Given custom templates, Then startup validation step 8 loads them without error
- Given fallback, When VEKTRA_PROMPT_TEMPLATES_DIR not set, Then built-in defaults used

**Traceability**: REQ-043, ARCH-054, ARCH-057
**Phase**: 1

---

### SC-C05: Prompt versioning regression detection

**Actor**: Platform Operator
**Trigger**: Operator modifies a prompt template and tracks the change
**Preconditions**: Queries already executed with original template

**Flow**:
1. Operator queries, notes prompt_version in QueryTrace (e.g., "a3b2c1d4")
2. Operator modifies context.j2 template
3. Operator restarts Vektra
4. Operator queries again
5. prompt_version in QueryTrace changes (e.g., "e5f6g7h8")
6. Per-template hashes in StepTrace metadata for build_prompt also reflect the change
7. Operator can correlate response quality changes with template version changes

**Expected outcome**: Template changes produce detectable version changes in QueryTrace.

**Acceptance criteria**:
- Given system.j2 unchanged and context.j2 modified, Then prompt_version (composite) changes
- Given build_prompt StepTrace, Then metadata includes per-template hashes (system_hash, context_hash, conversation_hash)
- Given prompt_version, Then computed as SHA-256[:8] of concatenated template sources

**Traceability**: REQ-065, ARCH-048, ARCH-054
**Phase**: 1

---

### SC-C06: EventEmitter emission points execute (Phase 1 NoOp)

**Actor**: (internal pipeline behavior)
**Trigger**: Ingest, query, and delete operations complete
**Preconditions**: Vektra stack running with NoOpEventEmitter (Phase 1 default)

**Flow**:
1. Operator ingests a document (POST /ingest)
2. EventEmitter.emit("document.indexed", {document_id, namespace}) called (NoOp)
3. Operator submits query (POST /query)
4. EventEmitter.emit("query.completed", {response_id, namespace, duration_ms}) called (NoOp)
5. Operator deletes document (DELETE /documents/{id})
6. EventEmitter.emit("document.deleted", {document_id, namespace}) called (NoOp)
7. All three calls complete without side effects

**Expected outcome**: EventEmitter called at all three emission points with correct payloads. Phase 1 NoOp: no side effects. Emission points exist and are reachable - verified via debug log or test double.

**Acceptance criteria**:
- Given NoOpEventEmitter, When POST /ingest completes successfully, Then EventEmitter.emit called for "document.indexed" with document_id and namespace
- Given NoOpEventEmitter, When POST /query completes successfully, Then EventEmitter.emit called for "query.completed" with response_id
- Given NoOpEventEmitter, When DELETE /documents/{id} completes, Then EventEmitter.emit called for "document.deleted"
- Given NoOpEventEmitter, Then emit() calls add <1ms overhead per call
- Given failed ingest (ERR-INGEST-001), Then "document.indexed" event not emitted (only on success)
- Given Phase 2 webhook config, Then same payloads delivered to configured webhook URL

**Traceability**: REQ-061, ARCH-038
**Phase**: 1 (NoOp), Phase 2 (webhook delivery)

---

## D. Safeguards (Phase 1 skeleton + Phase 2 logic)

### SC-D01: Pass-through hook verification (Phase 1)

**Actor**: Platform Operator
**Trigger**: Operator verifies safeguard hook points execute
**Preconditions**: Vektra stack running with PassthroughSafeguard (default)

**Flow**:
1. Operator calls POST /query
2. SafeguardHook.pre_query() called (returns allowed=True, <5ms)
3. SafeguardHook.post_retrieval() called (returns allowed=True, <5ms)
4. SafeguardHook.pre_response() called (returns allowed=True, <5ms)
5. Query completes normally

**Expected outcome**: Hook points execute without affecting behavior. Latency overhead <5ms per hook.

**Acceptance criteria**:
- Given PassthroughSafeguard, When POST /query, Then all three hook points execute (verifiable via debug log or StepTrace)
- Given PassthroughSafeguard, Then total safeguard overhead <5ms (measured via StepTrace)
- Given SafeguardHook.pre_query(), Then it executes before SSE stream starts

**Traceability**: REQ-044, ARCH-029
**Phase**: 1

---

### SC-D02: Out-of-scope topic detection (Phase 2)

**Actor**: Downstream application
**Trigger**: User asks a question outside the configured topic scope
**Preconditions**: SafeguardHook configured with topic boundaries (Phase 2)

**Flow**:
1. Client calls POST /query with off-topic question
2. SafeguardHook.pre_query() classifies query as out-of-scope
3. SafeguardResult(allowed=False, reason="Query outside configured topic scope")
4. System returns response indicating the question is outside scope

**Expected outcome**: Off-topic queries blocked before retrieval and LLM call.

**Acceptance criteria**:
- Given topic-scoped safeguard, When off-topic query submitted, Then blocked with reason
- Given blocking, Then no LLM call is made (cost savings)

**Traceability**: REQ-044, ARCH-049
**Phase**: 2

---

### SC-D03: PII in query detection (Phase 2)

**Actor**: Downstream application
**Trigger**: User submits query containing personal data
**Preconditions**: Presidio-based SafeguardHook configured (Phase 2)

**Flow**:
1. Client calls POST /query with "What grade did Mario Rossi get?"
2. SafeguardHook.pre_query() runs Presidio detection
3. System detects PERSON entity "Mario Rossi"
4. SafeguardResult.modified_content contains anonymized query: "What grade did `<PERSON>` get?"
5. Pipeline continues with anonymized query

**Expected outcome**: PII anonymized before reaching LLM. Annotations record entity types.

**Acceptance criteria**:
- Given query with PII, When pre_query runs Presidio, Then modified_content has anonymized text
- Given anonymization, Then annotations record entity type and position
- Given modified_content, Then pipeline uses it instead of original query text

**Traceability**: REQ-044, ARCH-049
**Phase**: 2

---

### SC-D04: PII in response with anonymization (Phase 2)

**Actor**: Downstream application
**Trigger**: LLM response contains personal data from indexed documents
**Preconditions**: Presidio-based SafeguardHook configured (Phase 2)

**Flow**:
1. POST /query returns LLM response containing "Mario Rossi scored 28/30"
2. SafeguardHook.pre_response() runs Presidio on response text
3. Detects PERSON entity
4. SafeguardResult.modified_content: "`<PERSON>` scored 28/30"
5. Client receives anonymized response

**Expected outcome**: PII in responses anonymized before delivery to client.

**Acceptance criteria**:
- Given LLM response with PII, When pre_response runs, Then modified_content has anonymized text
- Given anonymization, Then client never sees original PII
- Given modification, Then details recorded in QueryTrace via SafeguardResult.annotations

**Traceability**: REQ-044, ARCH-049
**Phase**: 2

---

## E. Security and access control

### SC-E01: Bootstrap key lifecycle

**Actor**: Platform Operator (first-time setup)
**Trigger**: Operator uses bootstrap key to create first admin API key
**Preconditions**: Fresh deployment with VEKTRA_ADMIN_BOOTSTRAP_KEY set

**Flow**:
1. Operator calls POST /api-keys with Bootstrap key as Bearer token
2. System creates new API key with admin scope
3. System returns plaintext key (shown exactly once)
4. System marks bootstrap key as consumed
5. Audit log records "bootstrap_key_consumed"
6. Operator attempts POST /api-keys with bootstrap key again
7. System returns 401: "Bootstrap key already used. Create additional keys using admin API key."

**Expected outcome**: Bootstrap key is single-use. After consumption, only admin API keys can create more keys.

**Acceptance criteria**:
- Given fresh deploy, When POST /api-keys with bootstrap key, Then new admin key returned
- Given bootstrap consumed, When POST /api-keys with same bootstrap key, Then 401
- Given bootstrap key, Then it can only create keys (cannot list or revoke)
- Given lost admin key, Then recovery requires container redeploy with new bootstrap key

**Traceability**: REQ-021, REQ-036
**Phase**: 1

---

### SC-E02: API key scope enforcement

**Actor**: Platform Operator
**Trigger**: Operator tests scope-based access control
**Preconditions**: API keys with different scopes created

**Flow**:
1. Operator creates key with admin scope
2. Operator creates key with ingest scope
3. Operator creates key with query scope
4. Admin key accesses all endpoints (succeeds)
5. Phase 1: ingest and query keys also access all endpoints (enforcement deferred)
6. Unknown scope value rejected with 403 and ERR-AUTH-003

**Expected outcome**: Phase 1: all valid scopes permit all operations. Unknown scopes rejected.

**Acceptance criteria**:
- Given admin scope key, When any endpoint called, Then access granted
- Given ingest or query scope key in Phase 1, When any endpoint called, Then access granted (enforcement deferred)
- Given unknown scope value, When any endpoint called, Then 403 with ERR-AUTH-003

**Traceability**: REQ-024, REQ-031, REQ-041
**Phase**: 1 (enforcement Phase 2)

---

### SC-E03: Namespace isolation verification

**Actor**: Platform Operator
**Trigger**: Operator verifies documents in one namespace are invisible from another
**Preconditions**: Two namespaces with different documents

**Flow**:
1. Operator creates namespace "project-a"
2. Operator ingests "secret.pdf" into namespace "project-a"
3. Operator creates namespace "project-b"
4. Operator ingests "public.pdf" into namespace "project-b"
5. Client queries namespace "project-b" about content from "secret.pdf"
6. No results from "project-a" appear
7. Client queries namespace "project-a" about content from "public.pdf"
8. No results from "project-b" appear

**Expected outcome**: Complete namespace isolation. No cross-namespace data leakage.

**Acceptance criteria**:
- Given documents in namespace A, When query in namespace B, Then no results from A
- Given namespace isolation, Then WHERE namespace_id filter applied at database level

**Traceability**: REQ-048, ARCH-007, ARCH-047
**Phase**: 1

---

### SC-E04: Authentication error scenarios

**Actor**: Downstream application
**Trigger**: Client sends requests with invalid authentication
**Preconditions**: Vektra stack running

**Flow**:
1. Client calls POST /query without Authorization header -> 401 ERR-AUTH-001
2. Client calls POST /query with malformed token -> 401 ERR-AUTH-001
3. Client calls POST /query with valid format but unknown token -> 401 ERR-AUTH-001
4. Client calls POST /query with revoked API key -> 401 ERR-AUTH-001
5. All error responses include human-readable message and remediation

**Expected outcome**: Clear, actionable error responses for each authentication failure mode.

**Acceptance criteria**:
- Given missing Authorization header, Then 401 with ERR-AUTH-001 and remediation
- Given malformed token, Then 401 with ERR-AUTH-001
- Given revoked key, Then 401 within 60s of revocation
- Given all auth errors, Then error response includes message + remediation (never empty)
- Given failed auth, Then audit log records attempt with reason

**Traceability**: REQ-030, REQ-041, REQ-009
**Phase**: 1

---

### SC-E05: Audit trail completeness

**Actor**: Security Officer (via Platform Operator)
**Trigger**: Verification that all authenticated requests are logged
**Preconditions**: API keys created, various operations performed

**Flow**:
1. Operator makes N authenticated requests (mix of ingest, query, admin)
2. Operator checks structured log output
3. All N requests have corresponding audit entries
4. Each entry includes: timestamp, key_id, endpoint, method, status_code, request_id
5. No query text or response content in audit logs

**Expected outcome**: 100% audit coverage. GDPR-compliant (no content in audit).

**Acceptance criteria**:
- Given N authenticated requests, Then exactly N audit log entries exist
- Given audit entry, Then includes timestamp, key_id, endpoint, method, status_code, request_id
- Given audit entry, Then never contains query text or response content
- Given failed auth attempts, Then also logged with reason

**Traceability**: NFR-007, REQ-022, REQ-051
**Phase**: 1

---

### SC-E06: API key CRUD lifecycle with revocation timing

**Actor**: Platform Operator
**Trigger**: Operator creates, uses, and revokes an API key
**Preconditions**: Admin API key available

**Flow**:
1. Operator calls POST /api-keys with label="ingest-worker" and scopes=["ingest"]
2. System returns ApiKeyCreateResponse with plaintext key (shown once) and id
3. Operator calls GET /api-keys
4. Response lists the key with key_preview (last 4 chars only), scopes, created_at, revoked_at=null
5. Plaintext key absent from listing
6. Operator uses new key to call POST /ingest (succeeds)
7. Operator calls DELETE /api-keys/{id} to revoke
8. System returns {id, revoked_at}
9. Within 60 seconds: operator uses revoked key to call POST /ingest
10. System returns 401 with ERR-AUTH-001

**Expected outcome**: Full key lifecycle from creation to rejection after revocation. Plaintext only on creation. Revoked keys rejected within 60s. Revoked keys remain visible in GET /api-keys listing.

**Acceptance criteria**:
- Given POST /api-keys, Then 201 with plaintext key (only returned once), id, label, scopes, created_at
- Given GET /api-keys, Then response includes revoked keys (revoked_at non-null) - visibility required for history
- Given GET /api-keys, Then key_preview contains last 4 characters only (never plaintext)
- Given DELETE /api-keys/{id}, Then 200 with {id, revoked_at}
- Given revoked key used within 60s of revocation, Then 401 with ERR-AUTH-001
- Given key creation and revocation, Then audit log records both events with key_id and operator key_id

**Traceability**: REQ-020, REQ-030, REQ-032
**Phase**: 1

---

## F. Operational

### SC-F01: Startup validation sequence

**Actor**: Platform Operator
**Trigger**: Operator starts Vektra container
**Preconditions**: Docker compose environment

**Flow**:
1. Container starts, begins 8-step validation
2. Step 1: Pydantic validates all VEKTRA_* env vars
3. Step 2: asyncpg connects to PostgreSQL
4. Step 3: Alembic checks/applies migrations
5. Step 4: pgvector extension verified
6. Step 5: ProviderRegistry populated from config
7. Step 6: Embedding model loaded (first inference with test text)
8. Step 7: LLM connectivity checked (warning-only)
9. Step 8: Jinja2 templates loaded
10. FastAPI begins accepting requests

**Expected outcome**: All 8 steps complete within 60s. Clear error on any failure.

**Acceptance criteria**:
- Given valid config, Then all 8 steps pass and /health returns 200 within 60s
- Given invalid VEKTRA_LLM_PROVIDER value, Then step 5 fails: "Provider 'xyz' not found for llm. Available: [litellm]"
- Given PostgreSQL not running, Then step 2 fails: "PostgreSQL unreachable at {host}:{port}..."
- Given missing template, Then step 8 fails: "Template 'system.j2' not found..."
- Given LLM unreachable, Then step 7 logs warning (not fatal), container starts

**Traceability**: ARCH-057, NFR-004, NFR-009, REQ-005
**Phase**: 1

---

### SC-F02: LLM failure with graceful degradation

**Actor**: Downstream application
**Trigger**: LLM provider becomes unavailable during query
**Preconditions**: Documents indexed, primary LLM configured with fallback

**Flow**:
1. Client calls POST /query
2. Pipeline reaches LLM step
3. Primary model times out (30s default)
4. System logs warning, switches to fallback_model
5. Fallback model also fails
6. System checks context_only_enabled (default true)
7. System returns QueryResponse with context_only=True, answer=null, sources populated
8. QueryTrace records fallback_attempted=true, context_only=true

**Expected outcome**: Client always receives value (relevant sources at minimum).

**Acceptance criteria**:
- Given primary LLM timeout, When fallback configured, Then fallback attempted
- Given both LLM fail and context_only_enabled, Then response has context_only=True, answer=null, sources populated
- Given degradation, Then QueryTrace records fallback and context-only events
- Given no fallback_model configured, Then context-only directly after primary failure

**Traceability**: REQ-059, ARCH-043, ARCH-036
**Phase**: 1

---

### SC-F03: Container restart with zero data loss

**Actor**: Platform Operator
**Trigger**: Operator restarts Vektra container
**Preconditions**: Documents indexed, at least one query completed

**Flow**:
1. Operator ingests document, verifies queryable
2. Operator runs docker-compose restart vektra
3. Container restarts, startup validation runs
4. Operator calls POST /query about previously indexed document
5. Document is still queryable with same results

**Expected outcome**: Zero data loss for documents in INDEXED state.

**Acceptance criteria**:
- Given document in INDEXED state, When container restarted, Then document queryable after restart
- Given arq job in PROCESSING state, When container restarted, Then job resumes (arq PostgreSQL persistence)
- Given in-memory conversation state, When container restarted, Then conversations lost (accepted Phase 1 tech debt TD-01)

**Traceability**: NFR-005, ARCH-005
**Phase**: 1

---

### SC-F04: Health check endpoints

**Actor**: Platform Operator / Load balancer
**Trigger**: Health check request
**Preconditions**: Vektra stack running

**Flow**:
1. Load balancer calls GET /health (no auth) -> {status: "healthy", timestamp: "..."}
2. Operator calls GET /health?detail=full with Bearer token
3. System checks each component: database, vector store, LLM, embedding
4. Returns component array with name, status, latency_ms
5. Non-ok components include remediation hint
6. Operator calls GET /health?detail=full without token -> 401

**Expected outcome**: Two-tier health model. Shallow for infrastructure probes, deep for operator diagnostics.

**Acceptance criteria**:
- Given no auth, When GET /health, Then {status, timestamp} only, no component names or versions
- Given healthy system, When GET /health, Then 200
- Given degraded component, When GET /health, Then 200 (status: "degraded")
- Given all components down, When GET /health, Then 503 (status: "unhealthy")
- Given valid token, When GET /health?detail=full, Then component array with name, status, latency_ms
- Given no token, When GET /health?detail=full, Then 401

**Traceability**: REQ-025, REQ-004, ARCH-022
**Phase**: 1

---

### SC-F05: Configuration change (env var, template)

**Actor**: Platform Operator
**Trigger**: Operator changes configuration
**Preconditions**: Vektra stack running

**Flow**:
1. Operator changes VEKTRA_MIN_RELEVANCE_SCORE from 0.3 to 0.5
2. Operator restarts container (env vars read at startup)
3. Startup validation re-runs all 8 steps
4. Queries now use updated threshold
5. Operator modifies prompt template file
6. Operator restarts container
7. Template step loads new template, prompt_version changes

**Expected outcome**: Configuration changes take effect after restart. No hot-reload (Phase 1 simplicity).

**Acceptance criteria**:
- Given changed VEKTRA_MIN_RELEVANCE_SCORE, When container restarted, Then new threshold active
- Given changed template, When container restarted, Then prompt_version changes in subsequent QueryTrace
- Given invalid config value, When container restarted, Then startup validation fails with clear error

**Traceability**: ARCH-057, ARCH-054, ARCH-056
**Phase**: 1

---

### SC-F06: Monitoring endpoints

**Actor**: Platform Operator / Prometheus
**Trigger**: Monitoring system scrapes metrics
**Preconditions**: Vektra stack running with starlette-prometheus

**Flow**:
1. Prometheus scrapes GET /metrics
2. Metrics include HTTP request histograms aligned with latency NFRs
3. Operator calls GET /health/memory
4. System returns per-component memory usage (advisory)

**Expected outcome**: Prometheus-compatible metrics and memory observability available.

**Acceptance criteria**:
- Given GET /metrics, Then Prometheus-format metrics returned
- Given metrics, Then histogram buckets aligned with NFR-001/002/003 latency targets
- Given GET /health/memory, Then per-component memory usage returned

**Traceability**: ARCH-014, ARCH-027
**Phase**: 1

---

### SC-F07: Vector store write failure during ingestion

**Actor**: (system behavior under failure)
**Trigger**: VectorStoreProvider.upsert() fails while storing chunks
**Preconditions**: Document extraction and embedding completed successfully; vector store becomes unavailable

**Flow**:
1. Operator calls POST /ingest with valid PDF (sync path, <=10MB)
2. System extracts, chunks, and embeds successfully
3. VectorStoreProvider.upsert() throws (connection lost, disk full, timeout)
4. System aborts the write transaction
5. No partial record left in source_documents or document_chunks
6. System returns 503 with ERR-INGEST-004

**Expected outcome**: Atomic ingest failure. No orphaned records. Operator can retry safely.

**Acceptance criteria**:
- Given VectorStoreProvider.upsert() fails, When POST /ingest, Then 503 with ERR-INGEST-004
- Given ERR-INGEST-004, Then category="TRANSIENT", retry_after set
- Given ERR-INGEST-004, Then remediation instructs operator to retry POST /ingest
- Given failure, Then source_documents contains no partial record for this ingest
- Given failure, Then document_chunks contains no orphaned rows for this document_id
- Given async ingest (>10MB), When VectorStoreProvider.upsert() fails, Then job status transitions to "failed" with error_code="ERR-INGEST-004"

**Traceability**: ARCH-043, ARCH-052
**Phase**: 1

---

### SC-F08: Vector store read failure during query

**Actor**: Downstream application
**Trigger**: VectorStoreProvider.search() fails during query execution
**Preconditions**: Documents indexed; vector store becomes unavailable after indexing

**Flow**:
1. Client calls POST /query
2. Pipeline reaches vector_search step
3. VectorStoreProvider.search() throws (connection lost, timeout)
4. Pipeline stops at vector_search step (cannot proceed without chunks)
5. System returns 503 with ERR-QUERY-004

**Expected outcome**: Pipeline aborts at search step. No LLM call attempted. TRANSIENT error with retry guidance.

**Acceptance criteria**:
- Given VectorStoreProvider.search() fails, When POST /query, Then 503 with ERR-QUERY-004
- Given ERR-QUERY-004, Then category="TRANSIENT", retry_after set
- Given failure at vector_search step, Then no LLM call made (pipeline stops)
- Given ERR-QUERY-004, Then remediation instructs operator to retry or check vector store health
- Given POST /search (not /query), When VectorStoreProvider.search() fails, Then same ERR-QUERY-004 returned

**Traceability**: ARCH-043
**Phase**: 1

---

### SC-F09: TLS enforcement in production mode

**Actor**: Platform Operator
**Trigger**: Operator deploys Vektra with VEKTRA_ENV=production behind a TLS-terminating reverse proxy
**Preconditions**: Reverse proxy configured, VEKTRA_ENV=production set

**Flow**:
1. Operator sets VEKTRA_ENV=production
2. Container starts, startup log includes TLS enforcement note
3. External client attempts HTTP (non-TLS) connection to the reverse proxy
4. Reverse proxy rejects or redirects the non-TLS request
5. Vektra container receives only HTTPS-originated traffic from the proxy
6. Infrastructure probe calls GET /health on Vektra's container port directly (internal, no proxy)
7. GET /health returns 200 (health endpoint exempt from TLS enforcement)
8. All data API endpoints reachable only via HTTPS through the proxy

**Expected outcome**: No plaintext API traffic in production. Health endpoint available without TLS for infrastructure probes. Enforcement at reverse proxy layer (NFR-012: TLS termination is a proxy concern).

**Acceptance criteria**:
- Given VEKTRA_ENV=production, Then startup log includes message confirming production mode active
- Given production mode, When API endpoint called over HTTP (bypassing proxy), Then connection refused or redirected by proxy
- Given production mode, Then GET /health accessible without TLS (probe exemption)
- Given VEKTRA_ENV=development, Then HTTP connections permitted with no enforcement
- Given QS-09, Then TLS 1.2+ minimum version enforced at proxy layer (Vektra does not terminate TLS itself)

**Traceability**: NFR-012, QS-09
**Phase**: 1

---

### SC-F10: LLM provider switch via configuration

**Actor**: Platform Operator
**Trigger**: Operator switches LLM provider from Ollama to OpenAI
**Preconditions**: Vektra running with Ollama as primary LLM provider

**Flow**:
1. Operator calls POST /query - response comes from Ollama
2. GET /providers shows ollama as primary with status "ok"
3. Operator changes VEKTRA_LLM_PROVIDER to "openai/gpt-4o" and sets OPENAI_API_KEY
4. Operator restarts container
5. Startup validation step 7 verifies new LLM connectivity (warning-only, not fatal)
6. GET /providers shows openai as primary
7. Operator calls POST /query - response comes from OpenAI
8. QueryTrace llm_call StepTrace metadata records new model identifier

**Expected outcome**: Provider switch requires only config change + restart. No code change, no data migration. litellm handles provider-specific API format differences transparently.

**Acceptance criteria**:
- Given VEKTRA_LLM_PROVIDER changed and container restarted, Then GET /providers reflects new provider name
- Given valid OpenAI API key, When POST /query after switch, Then response generated by OpenAI model (verifiable via model field in trace)
- Given QueryTrace, Then llm_call StepTrace metadata includes model identifier matching VEKTRA_LLM_PROVIDER
- Given invalid API key for new provider, Then startup step 7 logs warning (not fatal), container starts with provider status "unavailable"
- Given Anthropic as provider, Then ANTHROPIC_API_KEY read by litellm directly (no VEKTRA_ prefix required)
- Given Ollama provider, Then no API key required, VEKTRA_LLM_API_KEY ignored

**Traceability**: REQ-047, REQ-013, ADR-0008
**Phase**: 1

---

### SC-F11: Zero-downtime reindex via index version rotation

**Actor**: Platform Operator
**Trigger**: Operator reindexes documents after updating the embedding model
**Preconditions**: Documents indexed with index_version=1; new embedding model deployed

**Flow**:
1. GET /stats shows index_versions=[1], document_count=10
2. Operator re-submits chunk embeddings via POST /documents/{id}/chunks for all documents with index_version=2
3. GET /stats shows both versions present
4. Operator changes VEKTRA_ACTIVE_INDEX_VERSION=2 and restarts container
5. VectorStoreProvider.search() filters WHERE index_version=2
6. POST /query returns only version-2 sourced chunks
7. Version-1 chunks remain in database (manual cleanup optional)

**Expected outcome**: New embedding model activated without downtime. Version rotation is a config change. Old chunks available for rollback by reverting VEKTRA_ACTIVE_INDEX_VERSION.

**Acceptance criteria**:
- Given chunks stored with index_version=2, When VEKTRA_ACTIVE_INDEX_VERSION=2, Then only version-2 chunks returned in POST /search and POST /query
- Given VEKTRA_ACTIVE_INDEX_VERSION=1, Then GET /stats index_versions reflects [1] as active
- Given chunks from two versions coexisting, When VEKTRA_ACTIVE_INDEX_VERSION=2, Then citations reference only version-2 document_chunk rows
- Given index version change, Then no re-extraction required (text in source_documents unchanged)
- Given VEKTRA_ACTIVE_INDEX_VERSION=1 restored, Then version-1 chunks resume serving queries

**Traceability**: REQ-064, ARCH-045
**Phase**: 1

---

## G. Compliance

### SC-G01: Soft delete with retention verification

**Actor**: Platform Operator
**Trigger**: Operator verifies soft delete behavior
**Preconditions**: Documents indexed

**Flow**:
1. Operator deletes document via DELETE /documents/{id}
2. System sets deleted_at and deletion_reason
3. Operator verifies document excluded from search
4. Operator verifies document excluded from dedup checks
5. Database: record still exists with deleted_at populated
6. Phase 2: arq cleanup job hard-deletes after VEKTRA_RETENTION_DAYS

**Expected outcome**: Soft delete preserves data for compliance. Retention configurable.

**Acceptance criteria**:
- Given DELETE /documents/{id}, Then deleted_at and deletion_reason set (not hard deleted)
- Given soft-deleted document, Then excluded from all API endpoints
- Given soft-deleted document, Then database record retained

**Traceability**: REQ-057, ARCH-040
**Phase**: 1

---

### SC-G02: Audit log retention

**Actor**: Platform Operator
**Trigger**: Operator configures audit log retention
**Preconditions**: VEKTRA_AUDIT_RETENTION_DAYS set

**Flow**:
1. Operator sets VEKTRA_AUDIT_RETENTION_DAYS=90
2. Startup log confirms configured retention period
3. Phase 1: retention is operator responsibility (log rotation)
4. Phase 2: automated cleanup of logs older than retention period

**Expected outcome**: Retention policy configurable and visible.

**Acceptance criteria**:
- Given VEKTRA_AUDIT_RETENTION_DAYS=90, Then startup log shows configured retention
- Given Phase 1, Then operator manages log rotation manually

**Traceability**: NFR-008
**Phase**: 1 (configuration only, enforcement Phase 2)

---

### SC-G03: Conversation privacy (admin cannot see content)

**Actor**: Platform Operator with admin scope
**Trigger**: Admin attempts to access conversation content
**Preconditions**: Conversations exist in encrypted storage

**Flow**:
1. Users have had conversations via POST /query
2. Admin calls various API endpoints
3. No endpoint exposes conversation content (query text or response text)
4. Audit logs contain only metadata (timestamp, key_id, endpoint, method, status_code)
5. Admin dashboard shows only aggregate metrics

**Expected outcome**: Admin scope cannot access conversation content via any path.

**Acceptance criteria**:
- Given admin scope, Then no API endpoint returns conversation text or response text
- Given audit logs, Then never contain query text or response content
- Given admin dashboard, Then shows only aggregate metrics, never individual conversations

**Traceability**: REQ-051, ARCH-031, ARCH-041
**Phase**: 1

---

### SC-G04: PII redaction in logs

**Actor**: (internal system behavior)
**Trigger**: System logs contain potentially sensitive information
**Preconditions**: structlog configured with PII redaction processors

**Flow**:
1. Query executed with user data
2. structlog processors run on all log events
3. PII patterns redacted before log output
4. Structured JSON log written with redacted fields

**Expected outcome**: No PII in application logs. structlog processors enforce redaction.

**Acceptance criteria**:
- Given structlog configuration, Then PII redaction processors active
- Given log output, Then no user query text or response content in logs
- Given all log events, Then output in structured JSON format

**Traceability**: ARCH-013, REQ-051
**Phase**: 1

---

## H. MVP validation (onboarding)

### SC-H01: 30-minute git clone to working query

**Actor**: Platform Operator (first-time setup)
**Trigger**: Operator clones repository and follows quickstart
**Preconditions**: Docker and docker-compose installed, internet access for image pull

**Flow**:
1. T+0:00 - git clone vektra-stack && cd vektra-stack
2. T+0:30 - docker-compose up -d completes without error
3. T+2:00 - GET /health returns 200
4. T+5:00 - make ingest FILE=samples/sample.pdf completes
5. T+6:00 - make query Q="What is this document about?" returns relevant response
6. Total: under 10 minutes (checkpoints from REQ-027, excluding image pull time)

**Expected outcome**: Complete MVP workflow in under 10 minutes active time. No manual configuration required.

**Acceptance criteria**:
- Given fresh clone, Then docker-compose up requires no manual configuration
- Given compose-up, Then health endpoint returns 200 within 2 minutes
- Given sample PDF, Then ingestion completes within 5 minutes
- Given ingested document, Then query returns relevant response with citations within 6 minutes
- Given repository, Then sample PDF and test scripts included

**Traceability**: REQ-005, REQ-027, REQ-007, REQ-018
**Phase**: 1

---

### SC-H02: Full integration test (bootstrap to query)

**Actor**: CI/CD pipeline (or Platform Operator)
**Trigger**: Automated integration test execution
**Preconditions**: Fresh deployment with VEKTRA_ADMIN_BOOTSTRAP_KEY set

**Flow**:
1. Use bootstrap key to create API key with scopes [ingest, query]
2. Receive new API key in response
3. Use new key to POST /ingest with sample document
4. Receive 200 or 202 with document/job ID
5. Poll until queryable (max 60s)
6. Use new key to POST /query with question about document
7. Receive response with answer and source attribution
8. Verify response_id (UUID), sources non-empty, citation_ids present

**Expected outcome**: Complete flow executes in single automated test script.

**Acceptance criteria**:
- Given fresh deployment, Then complete flow (bootstrap -> key -> ingest -> query -> verify) passes
- Given flow, Then document queryable within 60s of ingestion
- Given response, Then includes response_id, non-empty sources, citation_ids
- Given flow, Then executable as single automated test script

**Traceability**: REQ-032, REQ-036, REQ-055
**Phase**: 1

---

## I. vektra-learn (high-level, data model validation only)

> **Note**: these scenarios are defined at high level to validate that the Phase 1 data model supports vektra-learn requirements. Detailed acceptance criteria deferred to Phase 2 design. vektra-learn is LMS-agnostic.

### SC-I01: Instructor uploads course materials

**Actor**: Instructor (via vektra-learn API)
**Trigger**: Instructor triggers content ingestion for a course
**Preconditions**: vektra-learn deployed (Phase 2), course namespace exists

**Flow**:
1. vektra-learn creates namespace "corso-ml-2026" with metadata
2. vektra-learn calls POST /ingest with course materials, namespace="corso-ml-2026"
3. Documents indexed with metadata {content_type: "lecture", language: "it", course_id: "corso-ml-2026"}
4. course_id stored as arbitrary JSONB key (not hardcoded Phase 1 field)

**Data model validation**: Phase 1 JSONB metadata supports arbitrary keys including course_id without schema changes.

**Traceability**: REQ-048, REQ-063, ARCH-044, ARCH-047
**Phase**: 2 (data model validated in Phase 1)

---

### SC-I02: Student queries course content

**Actor**: Student (via chatbot widget)
**Trigger**: Student asks a question about course material
**Preconditions**: Course materials indexed in course namespace

**Flow**:
1. Chatbot widget calls POST /query with namespace="corso-ml-2026"
2. Query scoped to course namespace
3. Response includes citations from course materials
4. Student sees answer with source references

**Data model validation**: Phase 1 namespace isolation ensures cross-course query boundaries.

**Traceability**: REQ-003, REQ-048, ARCH-007
**Phase**: 2 (data model validated in Phase 1)

---

### SC-I03: Course isolation via namespace

**Actor**: Platform Operator (vektra-learn)
**Trigger**: Verification that one course's materials are invisible to another
**Preconditions**: Two course namespaces with different materials

**Flow**:
1. Namespace "corso-ml-2026" has machine learning materials
2. Namespace "corso-db-2026" has database materials
3. Student in corso-db queries about ML topics
4. No results from corso-ml appear

**Data model validation**: Phase 1 namespace isolation (SC-E03) directly supports course isolation.

**Traceability**: REQ-048, ARCH-007, ARCH-047
**Phase**: 2 (data model validated in Phase 1)

---

### SC-I04: Academic year filtering via metadata

**Actor**: Platform Operator (vektra-learn)
**Trigger**: Query filtered by academic year
**Preconditions**: Documents indexed with academic_year metadata key

**Flow**:
1. Documents ingested with metadata {academic_year: "2025-2026"}
2. New documents ingested with metadata {academic_year: "2026-2027"}
3. Query with filters={academic_year: "2025-2026"} returns only 2025-2026 content

**Data model validation**: Phase 1 arbitrary JSONB keys + GIN index support year filtering without schema changes.

**Traceability**: REQ-063, ARCH-044
**Phase**: 2 (data model validated in Phase 1)

---

### SC-I05: Enrollment-based access control

**Actor**: Student (via vektra-learn)
**Trigger**: Student attempts to query a course they are not enrolled in
**Preconditions**: vektra-learn manages enrollment (Phase 2)

**Flow**:
1. Student authenticates via vektra-learn
2. vektra-learn checks enrollment for target course
3. If not enrolled, request rejected before reaching vektra-core
4. If enrolled, vektra-learn proxies query with course namespace

**Data model validation**: Phase 1 API key scopes (REQ-024) and namespace isolation provide the building blocks. Enrollment logic is vektra-learn's responsibility.

**Traceability**: REQ-024, REQ-048
**Phase**: 2

---

### SC-I06: Chatbot widget with course context

**Actor**: Student (via chatbot widget)
**Trigger**: Student opens chatbot from LMS interface
**Preconditions**: vektra-learn deployed, chatbot widget configured

**Flow**:
1. Student opens chatbot in LMS (e.g., Moodle via vektra-moodle)
2. Widget receives course context (namespace, conversation_id)
3. Student asks question
4. Widget calls POST /query with namespace and conversation_id
5. Multi-turn conversation maintained within course context

**Data model validation**: Phase 1 conversation_id (REQ-049) and namespace (REQ-048) support this pattern.

**Traceability**: REQ-049, REQ-048
**Phase**: 2

---

## Requirements coverage analysis

### Gaps identified during scenario drafting

| Gap | Severity | Description | Recommendation |
|-----|----------|-------------|----------------|
| No incremental indexing marker | Low | No way to mark which documents were ingested in which batch. n8n tracks externally | Acceptable for Phase 1. n8n maintains batch state |
| No deletion verification endpoint | Low | After DELETE, operator must query to verify absence | Acceptable. DELETE response + subsequent query sufficient |
| Safeguards Phase 1 = pass-through | By design | No configurable rules in Phase 1 | TD-09 accepted. Hook points verified (SC-D01) |
| In-memory conversation loss | By design | Conversations lost on restart | TD-01 accepted. Phase 2 persistent storage |
| PostgreSQL/pgvector failure not covered | Medium | ARCH-043 covers LLM and retrieval degradation, not database failure | Health check (SC-F04) detects. Application cannot function without database |
| Namespace access control absent | By design | All admin keys can query all namespaces | Phase 1 single-operator model. Phase 2 adds per-key namespace restrictions |
| Scoped API key enforcement deferred | By design | Scope stored but not enforced | Phase 1: REQ-024 explicit deferral. Phase 2: full enforcement |
| Batch document processing absent | By design | Single-document only | EX-005 explicit exclusion |
| RAG quality evaluation absent | By design | QueryTrace only, no automated evaluation | ARCH-050 defines Phase 2 three-tier strategy |
| No explicit REQ for no_relevant_context | Low | ARCH-056 defines behavior, no corresponding REQ | Consider adding REQ in next revision |

---

*Generated: 2026-02-09. Updated: 2026-02-17 (v0.3 - added 10 coverage gap scenarios, reorganized: SC-E07->SC-F10, SC-I07->SC-F11, SC-I08->SC-A09)*
*Pending: roundtable refinement with QA Lead and Business Analyst*
