# Vektra

@../.s2s/CONTEXT.md

## API interaction

Before making **any** API call (curl, httpie, scripts), consult `docs/reference/api.md` or the live OpenAPI spec at `/openapi.json` to verify:
- Authentication method and header format
- Parameter names, types, and whether they are query, path, or body params
- Request body field names (e.g. `question` vs `query`)
- Correct endpoint for the task (e.g. `/api/v1/query` for RAG, `/api/v1/search` for raw vector search)

Do not construct API calls from memory or guesswork.

## Database investigation

When querying Postgres directly (psql, DB inspection):
- **Always run `\d table_name` first** to check column names and types. Common pitfalls: `namespace_id` (not `namespace`), `bytea` columns that need decryption, columns that don't exist.
- **Conversation turns are encrypted**: `question` and `answer` are `pgp_sym_encrypt()`'d. To read them: `SELECT pgp_sym_decrypt(question, '<key>') FROM conversation_turns WHERE ...` using `VEKTRA_CONVERSATION_KEY` from `.env`.
- **Where chunk text lives depends on the vector store provider**: `document_chunks` (Postgres) is written *only* by the pgvector provider (`providers/pgvector.py`). With `VEKTRA_VECTOR_STORE_PROVIDER=qdrant` that table stays **empty**: the Qdrant provider stores chunk text and metadata in the Qdrant payload (`text`, `namespace_id`, `document_id`, `parent_id`, `metadata.chunk_level`), and Qdrant is the only source of truth for per-chunk content. Postgres still holds `source_documents` (one row per document, with `chunk_count`). So in Qdrant mode, inspect chunks via the Qdrant REST API, not SQL. The Qdrant payload uses `namespace_id` as the namespace filter field.

## Chunks belong to the vector store, not to Postgres (ADR-0026)

`document_chunks` is an **implementation detail private to the pgvector provider**. No other module may read or write it. Every path that touches chunk text, chunk counts or chunk deletion goes through the `VectorStoreProvider` Protocol: `store()`, `search()`, `retrieve()`, `list_chunks()`, `count_chunks()`, `delete()`, `delete_index_version()`.

This is not a style preference. Before ADR-0026, several modules queried that table directly, which in Qdrant mode meant querying an **empty** table — and they did not fail, they **reported success**: reindex re-embedded nothing and marked the job `completed`, `GET /stats` reported 0 chunks for populated namespaces, and `DELETE /documents/{id}` returned 200 while the deleted document went on answering queries. Any new SQL against `document_chunks` outside `providers/pgvector.py` reintroduces that class of bug.

A new vector store provider must implement the whole Protocol, including `delete_index_version()`, and that method **must refuse to delete the provider's own active version** (raise `ActiveIndexVersionError`, surfaced as `ERR-INDEX-001`, HTTP 409). The failure mode it guards is not "an old index version survives", it is "the live index is emptied".

## A dormant capability is not harmless, it is only not yet dangerous

When a fix turns a path that was inert into one that actually runs, ask **immediately** what that path can now destroy. Do not wait to find out on the next pass. This has already happened three times here, each time to code that had been sitting harmlessly for months:

| Latent gap | Harmless while… | Armed by |
|---|---|---|
| `DELETE /documents/{id}` had no namespace binding | the delete deleted nothing from the active store | its own fix (BUG-023): it became a cross-namespace deletion |
| the `vektra-app` tests had never worked at all | nobody ran them, so nobody found out | wiring them into CI (DEBT-030) |
| no cleanup existed for an old index version | reindex wrote nothing, so no old version ever accumulated | its own fix: every reindex now doubles a namespace's storage (DEBT-032) |

The rule that comes out of it: **verify empirically, do not trust the reports.** The bugs in this family do not fail loudly, they return `200 OK` and a reassuring status field. A green status and a `completed` job are evidence of nothing until you have counted the rows or the points yourself.

## Query pipeline vs search endpoint

These are fundamentally different:

| | `/api/v1/search` | `/api/v1/query` |
|--|--|--|
| What it does | Raw vector search (Qdrant only) | Full RAG pipeline |
| Reranker | No | Yes |
| Threshold filter | No | Yes |
| LLM call | No | Yes |
| Field for question | `query` | `question` |
| Response field | `results[].text_snippet` | `sources[].snippet` |

When investigating **end-to-end answer quality** (retrieval + reranking + LLM), use `/api/v1/query`. When investigating **raw retrieval quality** (vector similarity only, no reranker), use `/api/v1/search`.

## Spec2Ship Commands

- `/s2s:specs` - Define requirements via roundtable
- `/s2s:design` - Design architecture via roundtable
- `/s2s:plan --new` - Create implementation plan
- `/s2s:brainstorm` - Creative ideation session
