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
- **Postgres holds metadata/text, Qdrant holds vectors**: chunk text is in `document_chunks` (Postgres), but vector search runs against Qdrant (check `VEKTRA_QDRANT_URL` and `VEKTRA_QDRANT_COLLECTION` in config for host/port/collection). To inspect vectors or search results, query Qdrant REST API directly. The Qdrant payload uses `namespace_id` as the namespace filter field.

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
