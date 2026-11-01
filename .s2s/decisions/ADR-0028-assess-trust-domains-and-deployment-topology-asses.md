# ADR-0028: Assess trust domains and deployment topology: 'assess' Compose profile, worker as sole WS/SMTP holder with no listener, network-less parser sidecar, Postgres-only API to worker channel (addendum to ADR-0004)

**Status**: accepted
**Date**: 2026-11-01
**Context**: design session `20261101-143802-design-assessment-discussion-chatbot` (assessment discussion chatbot); covers ARCH-066, ARCH-075, INT-002

## ARCH-066: Process and secret isolation: WS credential only in the assess worker; all untrusted parsing in a network-less, secret-less sidecar

### Context

The service-account WS token can read every attempt, override and correct answer on the site.
Student files (PDF, DOCX, ODT) are untrusted input (REQ-101). In P, both sat in processes
shared with public widget endpoints and ingest parsers. All four speakers flagged this as a
dormant capability.

### Decision

- The public API process never holds the WS credential, its decryption key or the SMTP
  credentials. A startup check in the API FAILS if a WS key file is present in its container
  (dormant-capability guard).
- Secrets are per-service Docker secret files (/run/secrets/...), never the shared .env and
  never the DB.
- The WS-holding process (vektra-assess-worker) exposes NO listening socket. API-to-worker
  traffic goes only through Postgres. Egress is allowlisted to the registered Moodle host(s),
  the SMTP relay, the LLM endpoint and Postgres.
- Postgres role separation: the API role and the worker role each have least-privilege
  grants. The exact grants depend on CONF-007.
- The parser sidecar (vektra-assess-parser) has network_mode none, a read-only root, runs as
  non-root, uses tmpfs with a size cap, has mem/pids/CPU limits and NO secrets. It runs one
  process per item under rlimits and is killed on timeout. It is reached over a unix socket on
  a dedicated volume mounted ONLY into the worker (never the API, never the secrets volume).
- The sidecar parses ALL untrusted input: submission files AND review-HTML-to-text
  sanitization (TeX/MathML/code/tables preserved, no media fetched).
- Limits are checked before parsing. XML is parsed with defusedxml, with zip-bomb guards,
  using pure-Python libraries only.
- NO degraded in-process parsing mode. A running sidecar is a REQ-079 setup-check
  prerequisite, and the feature is unavailable without it.
- The undeployed CMD_TARGET=worker entrypoint (ingest + analytics) is never reused for assess.

### Rationale

Converged 4/4 after the rebuttal. SEC's objection to the degraded mode was adopted: an env={} child still shares the filesystem with /run/secrets. Refusing the feature instead of introducing a file status outside the REQ-101 vocabulary is a facilitator decision.

### Options considered

- **Separate process holds WS credential + parser sidecar (network none)** — pros: a parser RCE or API-side SSRF cannot reach the grade-book credential; no CAP_SYS_ADMIN needed; same image; cons: one or two extra Compose services; an extra hop in pre-flight latency
- **P as written (in-process subprocess with unshare, shared worker)** — pros: no extra services; cons: unshare blocked by Docker's default seccomp; subprocess inherits secrets; co-resident with ingest parsers

### Consequences

- (+) contains the blast radius of the highest-value secret
- (+) makes REQ-101 isolation deployable
- (−) more services on an 8 GB / 4 CPU VM; memory budget needed before K is fixed (OQ-026)

## ARCH-075: Deployment topology: 'assess' Compose profile adding vektra-assess-worker and vektra-assess-parser, no Redis

### Context

OQ-028. The deployed stack is postgres + vektra only, with arq in-process (ADR-0006) and no Redis.

### Decision

- Base stack unchanged (ADR-0004 shape, MVP 30-minute criterion unaffected).
- The 'assess' profile adds two services, built from the SAME image:
  * vektra-assess-worker: CMD_TARGET=assess-worker; one asyncio process (job loop, ticks, WS
    client, pre-flight, transcript, delivery, purge, setup check); no ports; limit about
    512M; secrets per ARCH-066.
  * vektra-assess-parser: per ARCH-066; limit about 512-768M; a pool of about 3 with
    per-process RLIMIT_AS.
- Pre-flight concurrency is capped at K by a per-kind semaphore.
- Record this in ADR-0027 (an ADR-0004 addendum for the profile) and in a new ADR for the
  Postgres-native assess job loop, stating that ingest stays on ADR-0006 in-process arq and
  that the two converge if Phase-2 Redis lands.

### Rationale

Resolved 4/4.

### Consequences

- (+) only sites enabling the feature pay for it
- (+) no new technology
- (−) about 1-1.3 GB of added limits; co-locating Ollama overcommits 8 GB (new OQ)

See INT-002 in architecture.md Appendix B.

