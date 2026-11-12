# vektra-assess

Backend of the assessment discussion chatbot: a per-attempt, time-boxed discussion between a
student and an LLM about the student's own submitted and graded work, launched from the LMS by
its discussion plugin. The vertical reads the LMS only through the `LmsProvider` Protocol
(`vektra_shared.lms`, ADR-0033); the provider is a separate distribution loaded by name
(`VEKTRA_ASSESS_LMS_PROVIDER`), never imported; the Moodle one is released from
vektra-moodle-discuss (`provider/`) and installed by the `lms-moodle` image variant.

Dormant by default: with `VEKTRA_ASSESS_ENABLED=false` no `/api/v1/assess` route is mounted and
the `assess-worker` / `assess-parser` entrypoints refuse to run.

## Layout

| Sub-package | Role |
|---|---|
| `api/` | HTTP routes (session, teacher, privacy), mounted by `vektra_app` |
| `worker/` | Job loop; the only holder of the KEK, the LMS provider's credentials and SMTP credentials |
| `setup/` | REQ-079 setup-check runner and Vektra's own items (worker only) |
| `worker/parser_client.py` | Client for the network-less parser sidecar (worker only) |
| `parser_sidecar/` | The sidecar itself: server, supervised child, extractors (imports only `parser_protocol`) |
| `parser_protocol.py` | Wire framing, `FileStatus` vocabulary and labels, limits (stdlib only, both sides) |
| `context/` | Snapshot capture and `ContextBundle` assembly |
| `llm/` | Discussion turn runner and post-check |
| `transcript/` | Transcript rendering |
| `delivery/` | Outbox and download tokens |
| `crypto/` | Envelope encryption (AES-256-GCM, X25519 hand-off) |
| `db/` | ORM models and SECURITY DEFINER wrappers |

Boundaries are enforced by import-linter contracts in the root `pyproject.toml` and by
`tests/test_boundaries.py`. Design: `.s2s/architecture.md` Appendix B, ADR-0027..0032.
