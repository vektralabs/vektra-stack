# Implementation Plan: Assess privacy configuration, retention purge, erasure, export and metadata-only monitoring

**ID**: 20261101-154950-assess-privacy-retention
**Status**: active
**Branch**: N/A
**Created**: 2026-11-01T14:49:50Z
**Updated**: 2026-11-01T20:58:19Z
**Approved**: 2026-11-01 (coordinator)

## Traceability

**Source**: ARCH-071 (stable subject key, key matrix, offline key backup), ARCH-077 (purge flow, erasure records), ARCH-079 (crypto-shredding purge)
**Source Type**: architecture

## References

### Requirements
- REQ-080: Operator configuration and metadata-only monitoring @.s2s/requirements.md
- REQ-088: Student access to own transcript (post-purge expiry message) @.s2s/requirements.md
- REQ-089: Privacy configuration: retention, legal basis and notice (fail closed) @.s2s/requirements.md
- REQ-093: Student transcript access after loss of course access @.s2s/requirements.md
- REQ-085: Minimum audit event set (purge, erasure, setup-check events) @.s2s/requirements.md

### Architecture
- ARCH-071: Dedicated encrypted assess stores; stable subject key; key backup @.s2s/architecture.md
- ARCH-077: Assess data model and data flow (Flow 5 purge, erasure records) @.s2s/architecture.md
- ARCH-079: Envelope encryption; purge = crypto-shredding then row deletion @.s2s/architecture.md
- INT-001: DELETE /privacy/subjects/{userid}, GET /privacy/subjects/{userid}/transcripts, GET /privacy/activities/{cmid}/subjects, DELETE /privacy/activities/{cmid}/subjects (added 2026-11-01), GET /health/assess @.s2s/architecture.md
- COMP-002: /health/assess as readiness only @.s2s/architecture.md

### Decisions
- ADR-0031: Envelope encryption with live-session key hand-off @.s2s/decisions/ADR-0031-envelope-encryption-with-live-session-key-hand-off.md
- ADR-0029: Postgres-native leased job loop (daily purge tick under advisory lock) @.s2s/decisions/ADR-0029-postgres-native-leased-job-loop-for-assess-with-db.md

### Dependencies
- 20261101-154950-assess-transcript-delivery (transcripts, per-transcript DEKs, deliveries, download endpoint)
- 20261101-154950-assess-data-crypto (session keys, subject key, grant matrix) and 20261101-154950-assess-job-loop (tick framework, heartbeat) — transitively via plan 10

## Overview

Discussion data is personal data with exam-evidence weight. This plan makes the retention
mechanism real and verifiable: privacy settings that must be set before anything can be enabled,
a daily purge that destroys keys first and rows second and then counts, a subject erasure
endpoint for the Moodle Privacy API, a subject export that keeps working after enrolment ends,
and operator monitoring that exposes counts and ages but never content.

Per `.claude/CLAUDE.md`, "purged" is not a status field: every purge and erasure ends by counting
the content rows for the affected ids and fails loudly if the count is not 0.

## Design Notes

- **Privacy config (REQ-089).** Per site, in `assess_privacy_config(site_id, retention_days, legal_basis_label, legal_basis_text, notice_url, notice_text, notice_version, updated_by, updated_at)`,
  written by the operator through `PUT /api/v1/assess/sites/{site}/privacy` (admin key; values
  supplied by the DPO, OQ-020) and read by the plugin via the state payload for the pre-session
  screen. `privacy.is_configured(site)` is True only when every field is set and
  `1 ≤ retention_days ≤ 3650`. Unset → enable refused (plan 11), Start refused (defence in depth
  in plan 8's gate), setup-check item `privacy_config` MISSING. The pre-session summary is
  system-rendered and always contains the fixed mailbox sentence: "Le copie inviate via email
  seguono la conservazione della casella di posta universitaria e non vengono rimosse dalla
  cancellazione automatica di Vektra." `notice_version` is copied into the Start audit event
  (plan 8 reads `privacy.current_notice_version(site)`). Shortening retention is audited and
  takes effect at the next purge; lengthening never resurrects purged data.
- **Purge tombstones.** `assess_purge_records(id, site_id, subject_key, session_id, transcript_id, session_no, cmid, reason ENUM(retention, erasure), purged_at, counts JSONB)` —
  identifiers and counts only. They let `GET /transcripts/{tid}` answer 410
  `ERR_ASSESS_TRANSCRIPT_EXPIRED` ("Trascrizione non più disponibile (periodo di conservazione
  scaduto)") instead of 404, and are the "erasure records" of ARCH-077.
- **Purge algorithm** (worker tick, daily, `pg_try_advisory_lock`, batched 100 sessions):
  1. select CLOSED sessions with `closed_at < now() - retention_days` per site;
  2. deliveries still PENDING/RETRYING → ABANDONED + audit `delivery_abandoned_on_purge` +
     a `kind=purge_notice` content-free notice to the session's graders (activity, student id,
     session number), sent BEFORE step 3 is committed for that session;
  3. one transaction per batch: delete `assess_session_keys.kek_wrapped` and
     `assess_transcripts.dek_wrapped` (crypto-shredding), then delete turns, snapshots, transcripts,
     deliveries, download tokens, any handoff remnants, extra time, resets and reasons, render
     states of those sessions; insert tombstones; audit `purge` (counts only);
  4. verify: `SELECT count(*)` over every content table for the batch's ids must be 0, else
     raise, alert `purge_verification_failed`, and leave the batch's tombstones unwritten (the
     transaction rolls back).
  Done jobs and expired download tokens are cleaned in the same tick.
- **Backups vs crypto-shredding (drift found).** ARCH-079 states that deleting `kek_wrapped`
  also covers ciphertext in backups and WAL. That holds only for ciphertext whose wrapped DEK is
  absent from every retained backup, i.e. when DB backup retention is shorter than the margin
  after which a session is purged, or when the KEK is rotated and the old KEK destroyed once the
  last backup holding wrapped DEKs under it has expired. This plan documents the constraint and
  adds a startup warning when `VEKTRA_ASSESS_DB_BACKUP_RETENTION_DAYS` is unset; plan 13's
  runbook makes the KEK rotation cadence the enforcement.
- **Erasure (OQ-033 default).** `DELETE /api/v1/assess/privacy/subjects/{userid}` with
  `require_exact_scope("assess:privacy-erase")`, site-bound, rate-limited. `userid` → stable
  subject key (plan 2). Steps: close any live session through plan 8's single close path with
  reason `access_revoked` and no delivery (deliveries ABANDONED, audited); then the purge
  algorithm scoped to the subject (all sessions, any age), plus removal of the subject from
  extra-time rows and teacher config recipients (replaced per REQ-082, audited). Mode
  `VEKTRA_ASSESS_ERASURE_MODE=erase` (default) | `retain_exam_evidence` (DPO legal-basis
  exemption, OQ-020): in retain mode the call returns 200 with `outcome=retained` and the
  configured legal basis, audited, nothing deleted. Never a silent pass: any failure → 5xx with
  envelope, so the plugin's privacy provider surfaces it. Idempotent: a repeat returns counts 0.
  Response: `{outcome, counts:{sessions, transcripts, deliveries_abandoned}}`.
- **Export (REQ-093(b)).** `GET /api/v1/assess/privacy/subjects/{userid}/transcripts` with
  `require_exact_scope("assess:privacy-export")`, site-bound, rate-limited per key; does NOT
  consult enrolment or course access. Returns for that subject only:
  `[{transcript_id, cmid, course_id, session_no, closed_at, filename, sha256, content_base64}]`,
  paginated (cursor, ≤ 10 per page). Bytes are the stored transcript bytes, so hash-equal to the
  in-course copy. After purge: 0 items. Audited `privacy_export` (counts only).
- **Operator configuration (REQ-080).** Everything is `VEKTRA_ASSESS_*` settings validated at
  startup with bounds (provider/model id, admission limit K, sweeper interval, SMTP and retry
  policy from plan 10, per-session limits from plan 8). SMTP test send:
  `POST /api/v1/assess/ops/smtp-test` (admin key) enqueues `smtp_test_send` to the fixed
  `VEKTRA_ASSESS_OPS_TEST_RECIPIENT` (no recipient parameter accepted); result row readable via
  `GET /api/v1/assess/ops/smtp-test/{id}` (status + error class only). Admin keys get 403 on
  every content endpoint (transcripts, export) because assess scopes are non-inheriting.
- **Metrics (content-free).** A Prometheus collector registered on the API's existing `/metrics`
  reads `assess_metrics_snapshot()` (SECURITY DEFINER, returns counts only; cached 15 s):
  `vektra_assess_sessions{state}`, `vektra_assess_sessions_closed_total{reason}`,
  `vektra_assess_deliveries{status}`, `vektra_assess_deliveries_retrying_oldest_seconds`,
  `vektra_assess_capacity_refusals_total`, `vektra_assess_start_rollbacks_total{reason}`,
  `vektra_assess_purge_last_success_timestamp`, `vektra_assess_purge_rows_deleted_total`,
  `vektra_assess_worker_heartbeat_age_seconds`, `vektra_assess_jobs_oldest_ready_seconds`,
  `vektra_assess_smtp_probe_ok`, `vektra_assess_tls_failures_total`. Labels are fixed enums only
  (no site names, ids, user ids, course names). A test fails if any label value outside the enum
  set is emitted.
- **/health/assess (readiness).** Content-free JSON: worker heartbeat age, oldest ready job age,
  parser status, SMTP probe status and age, privacy configured (bool per site count), keys present
  (bool), purge last success age. 503 when not ready. Registered so it does NOT feed shallow
  `/health` (plan 1 mechanism; asserted again here).
- **Log hygiene.** A structlog processor for `vektra_assess.*` loggers keeps only allowlisted keys
  (ids, codes, counts, durations) at every level, unlike `_pii_redactor` (WARNING+, key-name
  based). Unknown keys are dropped and counted.

## Tasks

### Privacy config
- [ ] Migration (next revision): `assess_privacy_config`, `assess_purge_records`; SECURITY DEFINER `assess_metrics_snapshot()`; grants; extend plan 2's grant assertion
- [ ] `vektra_assess/privacy/config.py`: `is_configured`, `current_notice_version`, validation; replace plan 11's fail-closed stub
- [ ] `PUT/GET /api/v1/assess/sites/{site}/privacy` (admin key; audited old/new); pre-session summary builder with the fixed mailbox sentence; expose summary in the state payload (plan 8 field)
- [ ] Setup-check item `privacy_config` (plan 4 runner)

### Purge and erasure
- [ ] `vektra_assess/privacy/purge.py`: batch selection, ABANDONED + `purge_notice` rows, crypto-shred-then-delete transaction, tombstones, count verification with rollback on non-zero
- [ ] Register the daily purge tick with plan 3's tick framework (advisory lock, separate pool, hard timeout); include done-job and expired-token cleanup
- [ ] Add the 410 branch to `GET /transcripts/{tid}` and to the state payload's transcript links ("Trascrizione non più disponibile (periodo di conservazione scaduto)")
- [ ] `vektra_assess/privacy/erasure.py` + `DELETE /privacy/subjects/{userid}`: live-session close, scoped purge, recipient/extra-time cleanup, mode switch, idempotence, audit
- [ ] `GET /privacy/activities/{cmid}/subjects` (assess:privacy-export, site-bound, cmid re-resolved against the site binding): userids with any assess row for that activity; backs the plugin's `get_users_in_context` (added 2026-11-01, coordinator decision 3)
- [ ] `DELETE /privacy/activities/{cmid}/subjects` (assess:privacy-erase, optional `userids` body): per-activity erasure reusing `erasure.py` scoped to (site, cmid[, userids]); backs `delete_data_for_all_users_in_context` / `delete_data_for_users`; same idempotence, mode switch and row-count verification; cross-activity and cross-site rows untouched (added 2026-11-01)
- [ ] Startup: `VEKTRA_ASSESS_ERASURE_MODE` must be set explicitly outside development (fail closed: no implicit choice of the DPO's decision); warn when `VEKTRA_ASSESS_DB_BACKUP_RETENTION_DAYS` is unset

### Export
- [ ] `GET /privacy/subjects/{userid}/transcripts`: subject-key resolution, pagination, no enrolment check, rate limit, audit

### Operator config and monitoring
- [ ] Settings bounds validation for all operator-facing `VEKTRA_ASSESS_*` values (one startup step, `StartupValidationError` with remediation)
- [ ] SMTP test-send endpoints + worker handler (uses plan 10's sender; recipient fixed by setting)
- [ ] Prometheus collector over `assess_metrics_snapshot()`; label-enum guard test
- [ ] `/health/assess` readiness payload; test that killing the worker makes `/health/assess` 503 while shallow `/health` stays 200
- [ ] Assess log allowlist processor in `vektra_app/main.py configure_structlog()` scoped to `vektra_assess.*`
- [ ] `deploy/prometheus/assess-alerts.yml.example`: heartbeat > 60 s, oldest ready job > 30 s, FAILED deliveries > 0, RETRYING oldest > 1 h, TLS failures, purge last success > 26 h, purge verification failure, smtp probe down

### Keys
- [ ] `python -m vektra_assess.keys verify` CLI: compares fingerprints of loaded keys (KEK, transcript key, teacher-config key, hand-off key, token signing key, subject-key secret) with the `key_version`/kid values referenced in the DB; non-zero exit on mismatch (detects a restore with the wrong key set)
- [ ] `docs/guides/operators/assess-keys.md` (key inventory, which process holds what, loss consequences, offline backup separate from DB dumps; full rotation and restore drill in plan 13)

### Tests, docs
- [ ] Integration (testcontainers): sessions aged past retention → after purge, content row count 0 per table, only tombstones + audit remain; RETRYING delivery → ABANDONED, audited, purge_notice on the mail sink; injected failure mid-batch → rollback, 0 tombstones, alert; non-zero verification → alert and failure surfaced
- [ ] Crypto-shred check: after deleting `kek_wrapped`, decryption of a pre-purge ciphertext copy taken in the test fails with the live KEK (proves the key, not just the row, is gone)
- [ ] Erasure: subject with 3 sessions (one ACTIVE) → counts match, 0 rows remain, ACTIVE closed as access_revoked with 0 deliveries sent; repeat → counts 0; retain mode → 0 deletions + audit; other subjects' row counts unchanged
- [ ] Export: unenrolled subject → transcripts #1..#N, each sha256 equal to the in-course download; cross-user → 0 foreign records; another key/site → 403; after purge → 0 items
- [ ] REQ-080: admin key on download-token mint / transcript / export → 403; canary run (full flow with canaries in turns, files, solution) then grep of captured API, worker, parser logs, mail-sink server log and `/metrics` output → 0 hits; purge leaves 0 content rows
- [ ] REQ-089: any privacy field unset → enable refused (plan 11 path) and setup check reports `privacy_config`; Start audit event carries `notice_version`; pre-session summary contains the mailbox sentence
- [ ] Docs: `docs/reference/api.md` privacy, ops and health sections; `docs/reference/configuration.md` + `.env.example`; `CHANGELOG.md` [Unreleased]

## State & Data Lifecycle

| State element | Created by | Updated by | Invalidated/Deleted by |
|---------------|-----------|------------|------------------------|
| `assess_privacy_config` | `PUT /sites/{site}/privacy` | same endpoint (audited old/new) | site removal (operator) |
| `assess_purge_records` (tombstones) | purge / erasure transaction | never | retained as audit metadata; deleted only with the site (documented) |
| Session keys `kek_wrapped`, transcript `dek_wrapped` | plans 2/10 | key re-wrap job (plan 13 runbook) | purge/erasure step 3 (first), verified by count |
| Content rows (snapshots, turns, transcripts, deliveries, tokens, resets, extra time) | plans 2, 8, 10, 11 | their owners | purge/erasure step 3 (second), verified by count |
| SMTP test-send result rows | test-send endpoint | worker handler | daily tick deletes rows > 7 days |
| Metrics snapshot cache (API memory) | first scrape | every 15 s | process restart |
| Purge tick lease / advisory lock | tick start | — | tick end or session loss (Postgres releases) |

## Acceptance Criteria

- [ ] Any privacy setting unset → enable refused and the setup check reports `privacy_config` MISSING; Start refused (REQ-089)
- [ ] The Start audit event records the notice version; the pre-session summary contains the mailbox-retention statement (REQ-089)
- [ ] Content older than RETENTION_DAYS → content row count 0 after purge, only tombstones and audit metadata remain; RETRYING deliveries ABANDONED, audited, graders notified (REQ-089, REQ-080)
- [ ] After purge the student link returns 410 with the expiry message and the content is gone (row count 0) (REQ-088)
- [ ] Erasure removes all the subject's content (row count 0 per table), leaves other subjects untouched, is idempotent, and never returns success without having erased or recorded a `retained` outcome (OQ-033)
- [ ] Per-activity listing returns exactly the subjects with rows for that cmid; per-activity erasure leaves row counts of other activities and other sites unchanged (count-verified)
- [ ] Export for an unenrolled subject returns #1..#N hash-equal to the in-course copy; cross-user returns 0 foreign records; after purge 0 transcripts (REQ-093)
- [ ] An operator (admin) credential reading content gets 403; a canary grep of logs, mail logs and metrics returns 0; resend/test-send accept no recipient (REQ-080)
- [ ] `/health/assess` reports readiness from worker-written rows and returns 503 with the worker stopped while `/health` stays 200 (COMP-002)

## Testing Approach

Every destructive operation is tested by counting rows per content table for the affected ids
before and after, with a control subject whose counts must not change. Crypto-shredding is
proven by attempting decryption of a saved ciphertext after the purge. The canary grep runs the
real compose stack end to end and greps every log sink and the metrics output; a grep that finds
no log files fails rather than passing.

### Test Infrastructure

- **Required**: testcontainers pg16 with assess roles; injected clock for retention ageing; mail sink; compose stack (API, worker, parser, mail sink) for the canary grep and the health test
- **Provided by**: this plan (ageing helpers, canary grep script `tests/nfr/assess_canary_grep.sh`); mail sink from 20261101-154950-assess-transcript-delivery; roles from 20261101-154950-assess-data-crypto; compose assess profile from 20261101-154950-assess-foundations
- **False-pass guard**: purge tests assert the pre-purge row count is > 0 before asserting 0 after; the canary grep fails if any expected log file is missing or empty or if the canary is absent from the seeded input; the health test asserts the worker container is actually stopped before reading `/health/assess`

## Integration Notes

- Plugin P3 implements the Moodle Privacy provider: metadata with `external_location_link`,
  export via the export endpoint, `delete_data_for_user(s)` via the erasure endpoint, surfacing
  any non-2xx as a failure. It needs its own keys with `assess:privacy-export` and
  `assess:privacy-erase`.
- Plan 8 must refuse Start when `is_configured` is False and copy `notice_version` into the
  Start event; plan 11 already calls `is_configured`.
- Plan 13 owns the KEK rotation cadence that makes crypto-shredding hold against backups, and the
  restore drill.

## Notes

- OQ-020 (DPO): retention value, legal basis, notice text, erasure mode and REQ-092 approval are
  inputs; the mechanism ships fail closed until they are set. Blocker for pilot, not for build.
- OQ-033: erasure default implemented; `retain_exam_evidence` mode exists so the DPO decision is a
  setting, not a code change.
- Drift / risk: crypto-shredding vs backups (see Design Notes) — ARCH-079's claim is conditional on
  backup retention and KEK rotation.
- Risk: losing the subject-key secret breaks export and erasure mapping for historical rows
  (ARCH-071 "rekeying the subject key later would touch every historical row").
- Purge notice to graders is a new outbound flow; confirm with the DPO that it is covered by the
  notice (it carries no content).

> Request from plan 10 (review of 10a, 2026-11-07): `GET /state` returns `transcripts[].purged`
> as the constant `false` because `shred_transcript` deletes the transcript row and leaves no
> trace. When this plan adds `assess_purge_records`, set `purged` from it in
> `vektra_assess.api.state.transcripts_of` (LEFT JOIN on `transcript_id` or `session_id`) and
> update the pinning test `test_session_state.py::test_transcripts_list_generated_and_shredded_ones`.

> Request from plan 10 (CodeRabbit 4503029411 on PR #159): `transcript_generate` must not
> recreate a transcript that was purged. When the purge tombstone lands, check it in
> `vektra_assess.worker.transcript_gen.generate_transcript` before regenerating (a retry of a job
> whose settlement failed could otherwise run after `shred_transcript`).
