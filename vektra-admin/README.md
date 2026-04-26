# vektra-admin

System administration interface for the Vektra platform.

Phase 1: minimal admin endpoints included in vektra-core (namespace management, API key management, system health). Phase 2: full admin SPA with separate deployment.

## Endpoints

See [API reference](../docs/reference/api.md) for full request/response shapes:

- `POST/GET/DELETE /api/v1/api-keys` — API key lifecycle (`admin` scope)
- `GET /api/v1/admin/namespaces/{id}/config` — read namespace behavioral config (stored JSONB + resolved view)
- `PATCH /api/v1/admin/namespaces/{id}/config` — partial update of namespace behavioral config (current whitelist: `grounding_mode`, `show_sources`)

The PATCH endpoint backs upstream LMS plugins (e.g. the Moodle block edit form) so professors can toggle per-course behavior — strict/hybrid RAG, citation visibility — without touching environment variables or invoking platform admins.

See [architecture.md](../.s2s/architecture.md) for the component specification.
