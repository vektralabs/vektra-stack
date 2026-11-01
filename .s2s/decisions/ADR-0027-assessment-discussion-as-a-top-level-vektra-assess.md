# ADR-0027: Assessment discussion as a top-level vektra_assess module with a Vektra-authoritative split and a thin Moodle local plugin

**Status**: accepted
**Date**: 2026-11-01
**Context**: design session `20261101-143802-design-assessment-discussion-chatbot` (assessment discussion chatbot); covers ARCH-065, ARCH-073

## ARCH-065: Vektra-authoritative split with a thin, non-authoritative Moodle local_ plugin

### Context

REQ-067 says nothing rendered by Moodle is authoritative. BR-006(d) needs cohort-wide override
reads, which are invisible in the student's own session. REQ-070 and REQ-086 need
client-less, sub-minute scheduling, which Moodle cron cannot guarantee. REQ-075 requires
Vektra's own SMTP, and REQ-079 a dedicated service account.

### Decision

VEKTRA owns: the session state machine; START and IN-SESSION gates; attempt selection
(BR-012); the WS client and setup check; the snapshot and context assembly; the LLM path with
the post-check and safeguards; timer, outage and sweeper; transcript rendering and hash
verification; outbox and SMTP delivery; audit; purge.
The new local_ plugin (local_vektradiscuss, which depends on block_vektra's client) does only
the following:
- injects the button and pre-session screen through page hooks, rendered from a Vektra state
  endpoint (advisory; gates are re-evaluated at Start);
- requests tokens server-side (sesskey, require_login, login-as check);
- provides the settings section (coursemodule_standard_elements / edit_post_actions), which
  forwards config to Vektra (BR-013 is validated in Vektra);
- provides teacher pages that call Vektra APIs;
- provides the transcript download path (headers per BR-016);
- implements the Privacy API provider, which pulls server-to-server.
The plugin holds no session state, caches no eligibility, and renders nothing or a generic
unavailable notice on any non-2xx response. Moodle ids sent by the plugin are always
re-resolved by Vektra (REQ-095).

### Rationale

Converged 4/4 in the opening statements. All three P1 alternatives contradict accepted requirements.

### Options considered

- **Vektra-authoritative, thin plugin** — pros: gates and state implemented once in Python, constant across 4.3/5.2; DB-level invariants; client-less scheduling; cons: heavy Moodle WS consumer; a WS outage blocks Starts (fail closed, user-visible); login-as remains plugin-asserted
- **Moodle-side gate evaluation / state** — pros: no WS dependency for gate inputs; cons: violates REQ-067; cannot read other students' overrides in the student context; duplicate PHP logic across two Moodle versions; Moodle cron cannot meet REQ-070/REQ-086

### Consequences

- (+) single decision point and test surface
- (+) fail-closed defaults where OQ-025 leaves inputs unreadable
- (−) Vektra depends on Moodle WS availability
- (−) the plugin still needs a two-version hook matrix
- (−) most expensive decision to reverse (SA)

## ARCH-073: Top-level vektra_assess module with stable external names and import-linter contracts

### Context

CONF-001. After ARCH-067, the sub-package option reuses nothing it may use. The costly-to-reverse items are the names: a route baked into the deployed plugin, table names under encrypted, hash-anchored data, settings and error codes.

### Decision

- A new top-level package vektra_assess, wired only by vektra_app.
- Names: route /api/v1/assess, tables assess_*, owned revision files in the single root Alembic
  chain (0009+), settings VEKTRA_ASSESS_*, error codes ERR-ASSESS-*, its own test package.
- import-linter contracts in CI:
  (a) vektra_learn and vektra_assess are independent;
  (b) vektra_assess may import only vektra_shared, the vektra_core public API (LLMProvider,
      SafeguardHook) and the vektra_analytics public write API;
  (c) only vektra_assess.worker.* may import vektra_assess.moodle (WS client) and the parser
      client;
  (d) only vektra_assess.context may construct ContextBundle.
- The worker entrypoint must not import sentence_transformers, torch, qdrant_client, vektra_index
  or vektra_ingest. This is checked by a sys.modules startup assertion plus a CI idle-RSS
  ceiling test (about 300 MB).

### Rationale

Resolved 4/4 (SA and DevOps shifted on the evidence; SEC prefers it).

### Options considered

- **Top-level vektra_assess** — pros: stable non-'learn' names from the pilot on; a minimal worker import graph is easier to keep honest; widget shortcuts become import errors; cons: 1-2 days of scaffolding; JWT helpers moved to vektra_shared
- **vektra_learn.discussion sub-package** — pros: no new top-level package; cons: names embed 'learn'; risk of the learn __init__ pulling the ML stack into the worker

### Consequences

- (+) extraction or rename never needed after the pilot
- (−) a small amount of duplicated plumbing

