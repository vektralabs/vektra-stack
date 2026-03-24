# Vektra

@../.s2s/CONTEXT.md

## API interaction

Before making **any** API call (curl, httpie, scripts), consult `docs/reference/api.md` or the live OpenAPI spec at `/openapi.json` to verify:
- Authentication method and header format
- Parameter names, types, and whether they are query, path, or body params
- Request body field names (e.g. `question` vs `query`)
- Correct endpoint for the task (e.g. `/api/v1/query` for RAG, `/api/v1/search` for raw vector search)

Do not construct API calls from memory or guesswork.

## Spec2Ship Commands

- `/s2s:specs` - Define requirements via roundtable
- `/s2s:design` - Design architecture via roundtable
- `/s2s:plan --new` - Create implementation plan
- `/s2s:brainstorm` - Creative ideation session
