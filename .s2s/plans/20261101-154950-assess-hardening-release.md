# Implementation Plan: Assess hardening, NFR verification campaign and release

**ID**: 20261101-154950-assess-hardening-release
**Status**: in_progress
**Branch**: feat/assess-hardening-release (13a)
**Created**: 2026-11-01T14:49:50Z
**Updated**: 2026-11-10T16:00:00Z
**Approved**: 2026-11-01 (coordinator)

## Traceability

**Source**: ARCH-068 (chaos tests verified by row counts), ARCH-075 (deployment topology), ARCH-079 (key rotation runbooks, offline backup and restore drill), B.1 plan-phase priority 3
**Source Type**: architecture

## References

### Requirements
- REQ-067: Server-authoritative discussion lifecycle (all six close reasons, exactly-one) @.s2s/requirements.md
- REQ-071: Start pre-flight, commit and rollback @.s2s/requirements.md
- REQ-075: Durable transcript delivery by Vektra SMTP @.s2s/requirements.md
- REQ-079: Site admin setup check for the dedicated service account @.s2s/requirements.md
- REQ-080: Operator configuration and metadata-only monitoring @.s2s/requirements.md
- NFR-014: Attempt isolation @.s2s/requirements.md
- NFR-015: Anti-leak exclusion of solution and hidden material @.s2s/requirements.md
- NFR-016: Accessibility of the discussion UI and transcript @.s2s/requirements.md
- NFR-017: Context-assembly exclusion verification @.s2s/requirements.md
- NFR-018: Discussion latency (should) @.s2s/requirements.md
- NFR-019: Availability in exam windows (should) @.s2s/requirements.md
- NFR-020: Admission limit and expected peak @.s2s/requirements.md
- NFR-021: Transcript availability and delivery timing @.s2s/requirements.md

### Architecture
- ARCH-068: DB-enforced lifecycle, outbox, leased job loop; chaos tests @.s2s/architecture.md
- ARCH-075: 'assess' Compose profile (vektra-assess-worker, vektra-assess-parser) @.s2s/architecture.md
- ARCH-076: Cross-origin iframe, per-site frame-ancestors @.s2s/architecture.md
- ARCH-079: Envelope encryption; KEK and sealing-key rotation; offline backup and restore drill @.s2s/architecture.md
- ARCH-071: Key matrix and backups @.s2s/architecture.md
- INT-001: Negative IDOR tests across site/course/user @.s2s/architecture.md

### Decisions
- ADR-0006: Background tasks with arq (drift: never deployed) @.s2s/decisions/ADR-0006-background-tasks-arq.md
- ADR-0028: Assess trust domains and deployment topology @.s2s/decisions/ADR-0028-assess-trust-domains-and-deployment-topology-asses.md
- ADR-0029: Postgres-native leased job loop @.s2s/decisions/ADR-0029-postgres-native-leased-job-loop-for-assess-with-db.md
- ADR-0031: Envelope encryption with live-session key hand-off @.s2s/decisions/ADR-0031-envelope-encryption-with-live-session-key-hand-off.md

### Dependencies
- Backend: 20261101-154950-assess-foundations, 20261101-154950-assess-data-crypto, 20261101-154950-assess-job-loop, 20261101-154950-assess-moodle-ws, 20261101-154950-assess-parser, 20261101-154950-assess-context, 20261101-154950-assess-llm-runner, 20261101-154950-assess-session-lifecycle, 20261101-154950-assess-discussion-ui, 20261101-154950-assess-transcript-delivery, 20261101-154950-assess-teacher-ops, 20261101-154950-assess-privacy-retention
- Plugin (vektralabs/vektra-moodle-discuss): 20261101-154950-discuss-foundation, 20261101-154950-discuss-student, 20261101-154950-discuss-teacher-privacy

## Overview

Each earlier plan proves its own slice. This plan proves the whole system on the target hardware
before a pilot exam: one consolidated chaos suite that kills every process at every dangerous
moment, an NFR campaign whose numbers come from REQ-085 event timestamps and row counts, the
isolation and anti-leak suites re-run on the target model and Moodle versions, the operator
runbooks (secrets, key rotation, offline backup and restore drill, framing configuration, Moodle
service account), the docs, and the release itself. It ends with written pilot go/no-go criteria.

The project rule governs every line: a green status or a `completed` job proves nothing until
rows, points, messages on the mail sink or bytes on disk have been counted.

## Design Notes

- **Chaos suite location.** `tests/chaos/assess/` (root tests, `@pytest.mark.chaos`), driving the
  compose stack with the `assess` profile through `docker compose kill -s KILL`,
  `docker compose restart postgres`, and a fault-injecting LLM endpoint and mail sink. Each
  scenario ends with `tests/chaos/assess/invariants.py`, a single SQL invariant checker run with a
  read-only superuser connection:
  - at most one non-CLOSED session per (site, cmid, student);
  - `consumed ⇔ persisted opening bot turn` for every record;
  - 0 `assess_session_handoff` rows for sessions not ACTIVE/OUTAGE;
  - transcripts count = CLOSED count (after a settle window ≤ 60 s);
  - per (transcript, recipient): exactly one SENT row and the mail sink holds ≥ 1 and ≤ 2 messages
    with that Message-ID (2 only with an audited `delivery_possible_duplicate`);
  - no job leased past `lease_until` + grace after recovery; STARTING never older than the reaper
    bound; every rollback left the record ELIGIBLE and unconsumed;
  - `kek_wrapped` present for every retained session (no accidental shredding).
- **Scenario matrix.** Six close reasons (finished, timed_out, service_failure, budget_exhausted,
  terminated_by_teacher, access_revoked) + start rollback + API lazy close, each crossed with:
  kill -9 worker mid-pre-flight, mid-generation, mid-SMTP (between DATA 250 and commit);
  kill -9 API mid-turn stream (expect the "risposta interrotta" reconciliation turn); Postgres
  restart during Start burst and during delivery; parser kill mid-extraction (expect rollback).
  Plans 3, 8 and 10 contain individual cases; this plan consolidates and runs the full matrix
  nightly and before release.
- **NFR campaign evidence** is computed only from REQ-085 audit timestamps and DB rows by
  `scripts/assess_nfr_report.py`, which emits a dated markdown + JSON report; numbers typed by
  hand are not evidence.
- **K and hardware (OQ-032, NFR-020).** K is not chosen in code. The campaign runs the burst
  (20, 30, 40 concurrent Starts with 10-file submissions) at K = 5, 10, 15, 20 on the candidate
  configurations (co-located Ollama on the 8 GB / 4 CPU VM; off-VM or dedicated LLM; larger VM)
  and records the largest K for which NFR-018 holds. The output is a decision input for ops and
  product, not a decision.
- **Crypto-shredding vs backups.** Plan 12 found that ARCH-079's backup claim is conditional. The
  KEK rotation runbook therefore fixes the cadence: rotate the KEK every
  `retention_days` interval, re-wrap live DEKs, and destroy the retired KEK once the oldest DB
  backup taken under it has expired; DB backup retention is documented next to it.
- **ADR-0006 drift.** arq was never started in any deployment (no Redis, `arq_pool` never
  registered, ingest falls back to `BackgroundTasks`, retention crons never run). Assess uses the
  Postgres loop of ADR-0029. This plan adds a dated "Status note" to ADR-0006 recording the
  actual state and pointing to ADR-0029, without rewriting the original decision.

## Tasks

### Chaos suite consolidation
- [ ] Create `tests/chaos/assess/` with the compose driver, fault-injecting LLM stub (latency, 5xx, stall) and mail-sink toggles reused from plan 10; `invariants.py` checker
- [ ] Implement the scenario matrix (close reasons × kill points × Postgres restart); each test asserts invariants and the exact expected row deltas
- [ ] Key-lifecycle chaos: API restart with live sessions (resume from handoff row without worker), worker down for 10 min (turns continue, closes deferred, then reconciled), epoch bump mid-stream
- [x] `make test-chaos-assess` and a nightly `chaos-assess` job in `integration.yml` (also required on release tags); results artifact = invariant report

### NFR verification campaign
- [x] `scripts/assess_nfr_report.py`: reads audit events + rows, computes p50/p95/p99 per NFR, commit rate, service_failure rate, capacity refusals, concurrency distribution
- [ ] NFR-020 / OQ-032: burst runs (20/30/40 Starts, 10-file submissions) at K = 5/10/15/20 on each candidate hardware configuration; write `docs/guides/operators/assess-sizing.md` with the evidence table and the recommended K and topology (decision input)
- [ ] NFR-018: latency at the chosen K on the reference model (OQ-026) — Inizia→commit, first visible output, full ~150-word reply
- [ ] NFR-019: 1-min probe over a 24 h soak with synthetic sessions; commit rate and service_failure rate excluding student-caused and capacity refusals
- [ ] NFR-021: re-run plan 10's timing benchmark on the target VM with the production-like SMTP relay (or the university relay in a test tenant)
- [ ] NFR-014 / INT-001: consolidated IDOR and token pen test matrix on Moodle 4.3 and 5.2 — token for A with B's attempt, other course/activity, expired, replay after CLOSED, unenrolled, cross-site key, login-as, query-string token, download-token reuse, teacher of other course, admin key on narrow scopes; 0 cross-student reads, each rejection audited
- [ ] NFR-015 / NFR-017: re-run the capture-time canary suite (plan 6/7) on the target model and provider against the exact LLM request bytes, including RAG OFF → 0 retrieval calls; record the run id
- [ ] NFR-016: final axe + keyboard + manual NVDA/VoiceOver pass on UI and transcript (plans 9, 10), on the plugin pages embedding the iframe on both Moodle versions
- [ ] REQ-080 canary grep on the target deployment logs, mail logs and `/metrics` (plan 12 script)

### Runbooks and operator docs
- [ ] `docs/guides/operators/assess-deployment.md`: compose `assess` profile, roles and DSNs, connection budget, `log_statement=none`, worker/parser limits, healthchecks, TLS UI origin, setup check walkthrough
- [ ] `docs/guides/operators/assess-secrets.md`: per-service secret files and the ARCH-071 key matrix (who must and must not hold what), generation commands, startup refusal messages
- [ ] Key rotation runbooks: KEK (re-wrap job, retire/destroy cadence tied to backup retention), API X25519 hand-off key (re-seal ≤ K live hand-off rows), transcript key (re-wrap per-transcript DEKs with `crypto.rotation.rewrap_transcript_deks` AND the teachers' reset / termination reasons with `rewrap_teacher_reasons`, plan teacher-ops 11b, before the old kid is retired), teacher-config key (re-seal blobs), token signing key (kid overlap), subject-key secret (not rotatable without full rekey — documented)
- [ ] Offline key backup + restore drill: back up keys separately from DB dumps; drill = restore DB dump + keys into a clean host, run `python -m vektra_assess.keys verify`, download a retained transcript and compare its sha256 to the pre-backup value, confirm purged sessions stay unrecoverable; record drill date and result
- [ ] Framing runbook: per-site `moodle_origin`, nginx/traefik exemption of `/api/v1/assess/ui` from `X-Frame-Options` (plan 9), distinct-origin requirement, verifying with the setup check
- [ ] Moodle service account runbook: dedicated account and role, required read functions, no grade-write, interactive login disabled, not site admin, not the ingestion account, token creation and rotation, assess:* API keys for the plugin (mint, teacher, privacy-export, privacy-erase) — aligned with plan 4 and P1
- [ ] `docs/reference/api.md`: complete Assess section (all INT-001 endpoints, scopes, error codes, SSE wire format, postMessage protocol); `docs/reference/error-codes.md` all `ERR-ASSESS-*`; `docs/reference/configuration.md` all `VEKTRA_ASSESS_*`; `.env.example` reviewed end to end
- [ ] ADR-0006 status note (drift record) pointing to ADR-0029; update `.s2s/CONTEXT.md` components table with vektra-assess (only if not already done by plan 1)

### Release
- [ ] Release checklist `docs/guides/contributors/release-assess.md` (or the existing release section): version bump in every workspace `pyproject.toml` (currently 0.7.1) to the next minor, `uv lock`, CHANGELOG `[Unreleased]` → `[X.Y.0] - date` + fresh `[Unreleased]`, BUG-027 qemu baseline green with the assess modules, image built by `publish.yml` on tag and smoke-tested (`/health`, `/health/assess`, setup check), minimum plugin_version enforced at mint matches the released plugin
- [x] Image content boundary: `test_image_content_boundary.py` asserts assess UI assets present and no test fixtures, mail-sink, canary datasets or `.s2s/` in the image
- [ ] Pilot go/no-go document `docs/guides/operators/assess-pilot-go-no-go.md` with the criteria below, signed off per item with evidence links

## State & Data Lifecycle

| State element | Created by | Updated by | Invalidated/Deleted by |
|---------------|-----------|------------|------------------------|
| Chaos/NFR test databases and compose volumes | chaos harness setup | scenarios | `docker compose down -v` at teardown (asserted) |
| NFR/chaos reports (artifacts) | `scripts/assess_nfr_report.py`, invariant checker | each run | CI artifact retention; release evidence copied into the go/no-go doc |
| Retired KEK / keys (operational) | rotation runbook | — | destroyed per cadence after the last backup under it expires (runbook) |
| Offline key backups | backup runbook | each rotation | superseded copies destroyed per runbook |

## Acceptance Criteria

- [ ] Full chaos matrix passes with 0 invariant violations on two consecutive nightly runs and on the release tag (REQ-067, REQ-071, ARCH-068)
- [ ] SMTP outage and mid-SMTP kill scenarios: exactly one SENT row per recipient, mail sink ≤ 1 audited duplicate (REQ-075)
- [ ] K is documented with burst evidence and the hardware recommendation before the pilot (NFR-020, OQ-032)
- [ ] NFR-018 and NFR-019 measured and reported per provider and model at the chosen K; breaches listed, not hidden (NFR-018, NFR-019)
- [ ] NFR-021 met on the target VM: generation p99 ≤ 60 s, SENT p95 ≤ 5 min (NFR-021)
- [ ] 0 cross-student reads across the NFR-014 matrix on 4.3 and 5.2, each rejection with an audit row (NFR-014)
- [ ] Canary suites re-run on the target model/provider: 0 solution/hidden canaries in captured request bytes and outputs; RAG OFF → 0 retrieval calls (NFR-015, NFR-017)
- [ ] Accessibility pass recorded for UI, plugin embedding and transcript (NFR-016)
- [ ] Canary grep of target logs, mail logs and metrics returns 0 (REQ-080)
- [ ] Restore drill completed: transcript sha256 equal after restore, purged data unrecoverable, `keys verify` exit 0 (ARCH-079)
- [ ] Setup check on the pilot site is all OK, including `ui_origin_distinct` and `privacy_config` (REQ-079)

## Testing Approach

Black-box against the deployed stack. The chaos harness and the NFR report never consult
application status endpoints for their verdicts; they read rows (read-only superuser), the mail
sink and audit timestamps. Pen tests are scripted (pytest + httpx) so they are repeatable on
every release, with a manual exploratory session on top.

### Test Infrastructure

- **Required**: compose stack with the `assess` profile; fault-injecting LLM stub and mail sink; Moodle 4.3 and 5.2 dev envs with seeded fixtures; target VM(s) for the campaign; reference LLM (OQ-026); screen readers for the manual pass
- **Provided by**: this plan (chaos harness, invariant checker, NFR report script); dev envs and seeding from 20261101-154950-assess-moodle-ws; mail sink from 20261101-154950-assess-transcript-delivery; canary suites from 20261101-154950-assess-context and 20261101-154950-assess-llm-runner; target hardware from ops (OQ-032)
- **False-pass guard**: the invariant checker fails when a scenario produced fewer sessions than declared; NFR report fails on sample size below the declared minimum; any campaign item without target hardware or reference model is reported BLOCKED in the go/no-go document, never PASS

### NFR Verification

| NFR | Dataset/Load | Tool | Environment | Pass criterion |
|-----|--------------|------|-------------|----------------|
| NFR-014 | negative matrix (≥ 12 case types × 2 Moodle versions × quiz/assign) | `tests/pen/assess/` pytest + httpx | staging with 4.3 and 5.2 | 0 cross-student reads; every case rejected and audited |
| NFR-015 | ≥ 50 adversarial queries per fixture, canaries in hidden module/solution | canary suite on captured LLM request bytes + QueryTrace | target model/provider | 0 canaries; RAG OFF → 0 retrieval calls |
| NFR-016 | all UI states + transcripts, 1 quiz + 1 assignment | axe/Playwright + manual NVDA/VoiceOver | staging, both Moodle versions | 0 AA violations; full keyboard flow; 1 announcement per reply/warning |
| NFR-017 | adversarial suite, toggle OFF/ON, HTML filtering disabled, hidden rubric descriptor | canary suite on captured context | target model | 0 solution/hidden/descriptor canaries with toggle OFF; present with ON |
| NFR-018 | bursts of 20/30/40 Starts with 10-file submissions at chosen K, 150-word replies | `scripts/assess_nfr_report.py` on REQ-085 timestamps | target VM + reference LLM | commit p50 ≤ 10 s, p95 ≤ 30 s, max < 60 s; first output p95 ≤ 8 s; full reply p95 ≤ 30 s |
| NFR-019 | 24 h soak + 1-min probe | probe + report script | target VM | ≥ 99% admitted Starts commit; service_failure ≤ 2%; probe ≥ 99% |
| NFR-020 | K sweep 5/10/15/20 × bursts | report script | each candidate hardware config | K = largest value meeting NFR-018, documented with evidence |
| NFR-021 | 50 concurrent closes, 2 recipients each | plan 10 benchmark | target VM + relay | generation p99 ≤ 60 s; SENT p95 ≤ 5 min; ≥ 99% SENT or downloadable ≤ 15 min |

## Integration Notes

- Requires the plugin plans released in a version whose `plugin_version` meets the minimum
  enforced at mint; the go/no-go lists the exact plugin and Vektra versions tested together.
- The chaos suite is the consolidation point: plans 3, 8, 10, 12 keep their focused tests; a
  failure here is fixed in the owning plan's module, not patched in the harness.
- Pilot go/no-go criteria (all required): chaos matrix green; setup check green on the pilot
  site; DPO values set (OQ-020) and erasure mode chosen (OQ-033); OQ-029 reading confirmed by
  product/QA; K and hardware decided (OQ-032) with NFR-018 evidence; NFR-014/015/017 suites green
  on the target model; NFR-016 manual pass recorded; restore drill done within 30 days; alerts
  wired to an on-call recipient; quality gates of NFR-022/NFR-023 reported by their owner
  (OQ-027); RAG for discussions confirmed OFF.

## Notes

- **OQ-032 resolved 2026-11-01 (coordinator):** the assess bot uses the normal multi-provider LLM
  config; the choice is the provider per deployment. Sizing work below applies only when the
  provider is a local model that cannot sustain ~200 parallel requests: cap K/admission or size
  hardware. A remote provider carries the load, but data egress is gated by the DPO (OQ-020).

- OQ-032 is the largest open risk: the co-located on-prem LLM overcommits 8 GB / 4 CPU; the
  campaign may conclude that the pilot needs an off-VM LLM or a larger VM.
- OQ-026 (reference model) and OQ-027 (eval-suite owner) block NFR-018 and NFR-022/023 evidence;
  report BLOCKED until resolved.
- OQ-031 outcome (login-as cross-check) changes one pen-test case from "verified" to "accepted
  residual risk, trust-the-plugin" — record which.
- Drift recorded: ADR-0006 vs reality (no arq worker ever ran); ARCH-079 backup claim conditional
  on KEK rotation (plan 12); `deploy/nginx/vektra.conf.example` XFO DENY (plan 9).
- Per-key RPM rate limiting outside assess remains dormant (`app.state.rate_limiter` never set);
  confirm the release did not arm it as a side effect, or record that it did.
- **Worker DB pool vs. row fan-out (deferred from CodeRabbit 4513956359 on PR #190, 2026-11-09).**
  The delivery pool runs 2 jobs at once, and both the transcript handler (`delivery/handler.py`
  `_all`) and the notification handler (`delivery/notify.py`) gather their rows: up to 3 each, so
  up to 6 rows may want a DB connection at the same moment, against `VEKTRA_ASSESS_WORKER_POOL_SIZE`
  = 5 (`max_overflow=0`) shared with heartbeats and the other pools. A row holds a connection
  only for its claim and its settle, never across the SMTP send, so the pool waits rather than
  fails; but a slow DB can push a row past its job budget, and heartbeats queue behind it. The
  fix is one coordinator shared by every handler (a semaphore sized from the pool, minus a
  reserve for heartbeats), not a notification-only limit; it belongs to the NFR-020 sizing work
  here, with a load test (2 delivery jobs x 3 rows, DB latency injected) proving heartbeats stay
  fresh.

- **Split 13a / 13b (coordinator decision, 2026-11-10).** Plan 13 depends on every plan, the plugin
  P3 included (run by the plugin session); it proceeds with the tasks that do not need P3.
  - **13a** (branch `feat/assess-hardening-release`): "Chaos suite consolidation" (all four tasks),
    `scripts/assess_nfr_report.py`, the image content boundary test, and the items earlier PRs
    deferred here: the claim/settle duplication between the notify and transcript handlers
    (CodeRabbit on #190), the DB-connection budget shared by every handler (CodeRabbit 4513956359),
    the REQ-080 compose-stack canary grep left BLOCKED by 12b, and the key-rotation wiring where
    it is code (the runbook text is 13b).
  - **13b**: runbooks, operator docs, the reference-docs completion, the release checklist and the
    pilot go/no-go document.
  - NFR campaign items that need the target VM, HPC, the reference model (OQ-026), the production
    relay, live Moodle with login-as or a manual screen reader are BLOCKED with the missing
    environment and the command to run; never reported as PASS.
- **Chaos runner (13a).** `make test-chaos-assess` runs the suite with `CHAOS_REPORT_DIR` and then
  `tests/chaos/assess/gate.py`: it fails on a skipped case, on fewer cases (41) or invariant
  reports (12) than declared, or on any violation, and writes `invariants.md`. The PR job in
  `integration.yml` calls the target; a separate `.github/workflows/chaos-assess.yml` runs it
  nightly (02:17 UTC), on every `v*` tag and on demand, keeping the report 90 days. Drift: the plan
  named a nightly job inside `integration.yml`; a schedule there would re-run every integration job
  nightly, so the nightly run is its own workflow. First local run (2026-11-10): 41 passed, gate PASS.
- **NFR report (13a).** `scripts/assess_nfr_report.py` reads only rows: commit latency from
  `assess_sessions.created_at` (the STARTING row the Start writes) to `committed_at`, full reply
  from a student turn to the next bot turn, Start outcomes from `assess_start_attempts`, closes
  from `close_reason`, and a sweep of live intervals for the concurrency distribution. First
  visible output is in no row, so it is always BLOCKED, never estimated from a server proxy.
  "Admitted" (NFR-019) excludes `capacity` (reported for NFR-020), `part_b_rejected` (student) and
  `terminated_by_teacher`: an assumption to confirm with QA. A sample below its minimum is BLOCKED
  and one BLOCKED item keeps the report from PASS. Exit 0/1/2 = PASS/FAIL/BLOCKED.
- **Shared row connection budget (13a, done).** `vektra_assess.delivery.budget.RowDbBudget`: one
  semaphore of `VEKTRA_ASSESS_WORKER_POOL_SIZE - 2` permits, never below 1, created once in
  `assess_worker._delivery_parts` and passed to both `DeliveryDeps` and `NotifyDeps`. Every
  row-level checkout of the transcript and notification handlers takes a permit first. No handler
  nests a checkout inside another, which an AST check confirmed before wiring, so the budget cannot
  deadlock. The load test `integration/test_row_db_budget_load.py` runs 2 jobs x 3 rows with 0.6 s
  of injected DB latency on a pool of 5: with the budget the rows hold at most 3 connections and
  the heartbeat never waits half a row's latency; the unbudgeted control takes all 5 and queues the
  heartbeat. The SMTP test send (single row) and the job loop stay outside the budget.
- **Claim/settle duplication (13a, done; deferred from the review of #190).** The attempt outcome
  (sent / failed / retrying, with the retry window) and the three audit events of an attempt now
  live once, in `vektra_assess.delivery.attempt` (`decide`, `record`, `count`), used by both
  `_settle` functions. The two handlers differ only in where a retry's `not_before` comes from
  (the transcript schedule, or the notification's next run) and in the event names. The claim
  stays per handler: its statement, its Message-ID scheme and the transcript window re-check are
  specific to the row kind, so a shared claim would need a parameter for every one of them.
  Behaviour is unchanged: the existing notify and delivery suites (89 tests) pass unmodified.
