# First query tutorial

This tutorial walks through ingesting a custom PDF and querying it, with a detailed look at the response structure.

**Prerequisites**: a running Vektra stack with `VEKTRA_API_KEY` set. See [quick start](index.md) if you haven't set up yet.

## 1. Ingest your PDF

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -F "file=@/path/to/your-document.pdf;type=application/pdf" \
  "http://localhost:8000/api/v1/ingest?namespace=default" | python3 -m json.tool
```

Supported formats: PDF, DOCX, PPTX.

### Sync response (files <= 10 MB)

```json
{
    "document_id": "a1b2c3d4-...",
    "chunk_count": 24,
    "status": "indexed"
}
```

The document is immediately available for queries.

### Async response (files > 10 MB)

```json
{
    "job_id": "e5f6a7b8-...",
    "status": "processing"
}
```

Poll the job status until it completes:

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/api/v1/ingest/jobs/e5f6a7b8-.../status | python3 -m json.tool
```

The `status` field transitions: `processing` -> `indexed` (or `failed`).

## 2. Query the document

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "Summarize the main findings",
    "namespace": "default",
    "top_k": 5
  }' \
  http://localhost:8000/api/v1/query | python3 -m json.tool
```

Or using the shell script:

```bash
scripts/query.sh "Summarize the main findings"
```

## 3. Understanding the response

```json
{
    "response_id": "9f8e7d6c-...",
    "answer": "The document describes three main findings: ...",
    "sources": [
        {
            "doc_id": "a1b2c3d4-...",
            "chunk_id": "c1a2b3...",
            "score": 0.912,
            "snippet": "Finding 1: The study reveals that...",
            "citation_id": "e5f6a7-...",
            "document_version": 1
        },
        {
            "doc_id": "a1b2c3d4-...",
            "chunk_id": "d4e5f6...",
            "score": 0.847,
            "snippet": "Finding 2: In contrast to previous work...",
            "citation_id": "b8c9d0-...",
            "document_version": 1
        }
    ],
    "conversation_id": null,
    "context_only": false,
    "no_relevant_context": false
}
```

| Field | Description |
|-------|-------------|
| `response_id` | Unique identifier for this response (for debugging and audit) |
| `answer` | LLM-generated answer grounded in the retrieved sources |
| `sources` | Ranked list of document chunks used to build the answer |
| `sources[].doc_id` | Document that contains this chunk |
| `sources[].chunk_id` | Unique identifier of the retrieved chunk |
| `sources[].citation_id` | UUID citation reference for traceability |
| `sources[].document_version` | Index version of the chunk |
| `sources[].score` | Cosine similarity score (0.0 - 1.0, higher is more relevant) |
| `sources[].snippet` | Text excerpt from the chunk |
| `conversation_id` | Echoed back if provided in the request (see below) |
| `context_only` | `true` if both LLM providers failed and only raw sources are returned |
| `no_relevant_context` | `true` if no chunks exceeded the minimum relevance threshold |

## 4. Multi-turn conversations

Provide a `conversation_id` to maintain context across queries:

```bash
# First query - include a conversation_id (must be a valid UUID)
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What methodology was used?",
    "namespace": "default",
    "conversation_id": "550e8400-e29b-41d4-a716-446655440001"
  }' \
  http://localhost:8000/api/v1/query | python3 -m json.tool

# Follow-up query - same conversation_id
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "How does that compare to the control group?",
    "namespace": "default",
    "conversation_id": "550e8400-e29b-41d4-a716-446655440001"
  }' \
  http://localhost:8000/api/v1/query | python3 -m json.tool
```

The server retains the last 10 conversation turns (configurable via `VEKTRA_MAX_CONVERSATION_TURNS`). The conversation ID is client-managed: provide any valid UUID.

Using the shell script, a UUID is auto-generated for new conversations:

```bash
scripts/query.sh "What methodology was used?"
# Output includes: conversation_id: 550e8400-...
# Use it for follow-up:
scripts/query.sh "How does that compare?" 550e8400-...
```

## 5. Streaming responses

For real-time answer delivery, request Server-Sent Events:

```bash
curl -s -N \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"question":"Summarize the findings","namespace":"default","stream":true}' \
  http://localhost:8000/api/v1/query
```

The server sends SSE events as the answer is generated, followed by a final event with sources.

## 6. Check index stats

Verify what's indexed in a namespace:

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/stats?namespace=default" | python3 -m json.tool
```

```json
{
    "document_count": 1,
    "chunk_count": 24,
    "namespace": "default"
}
```

## Next steps

- [Configuration reference](../reference/configuration.md) - tune chunking, relevance thresholds, and LLM settings
- [API reference](../reference/api.md) - full endpoint documentation with all parameters
- [Error codes](../reference/error-codes.md) - troubleshoot specific error responses
