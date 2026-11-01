# vektra-assess

Backend of the assessment discussion chatbot: a per-attempt, time-boxed discussion between a
student and an LLM about the student's own submitted and graded work, launched from Moodle by the
`local_vektradiscuss` plugin (repository `vektra-moodle-discuss`).

Dormant by default: with `VEKTRA_ASSESS_ENABLED=false` no `/api/v1/assess` route is mounted and
the `assess-worker` / `assess-parser` entrypoints refuse to run.

## Layout

| Sub-package | Role |
|---|---|
| `api/` | HTTP routes (session, teacher, privacy), mounted by `vektra_app` |
| `worker/` | Job loop; the only holder of the KEK, WS tokens and SMTP credentials |
| `moodle/` | Typed Moodle web service readers (worker only) |
| `parser_client/` | Client for the network-less parser sidecar (worker only) |
| `context/` | Snapshot capture and `ContextBundle` assembly |
| `llm/` | Discussion turn runner and post-check |
| `transcript/` | Transcript rendering |
| `delivery/` | Outbox and download tokens |
| `crypto/` | Envelope encryption (AES-256-GCM, X25519 hand-off) |
| `db/` | ORM models and SECURITY DEFINER wrappers |

Boundaries are enforced by import-linter contracts in the root `pyproject.toml` and by
`tests/test_boundaries.py`. Design: `.s2s/architecture.md` Appendix B, ADR-0027..0032.
