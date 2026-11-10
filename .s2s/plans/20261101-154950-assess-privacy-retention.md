# Implementation Plan: Assess privacy configuration, retention purge, erasure, export and metadata-only monitoring

**ID**: 20261101-154950-assess-privacy-retention
**Status**: in_progress (12a on `feat/assess-privacy-retention`; 12b later)
**Branch**: feat/assess-privacy-retention
**Created**: 2026-11-01T14:49:50Z
**Updated**: 2026-11-09T18:30:00Z
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
- [x] Migration (next revision): `assess_privacy_config`, `assess_purge_records`; SECURITY DEFINER `assess_metrics_snapshot()`; grants; extend plan 2's grant assertion
- [x] `vektra_assess/privacy/config.py`: `is_configured`, `current_notice_version`, validation; replace plan 11's fail-closed stub
- [x] `PUT/GET /api/v1/assess/sites/{site}/privacy` (admin key; audited old/new); pre-session summary builder with the fixed mailbox sentence; expose summary in the state payload (plan 8 field)
- [x] Setup-check item `privacy_config` (plan 4 runner)

### Purge and erasure
- [x] `vektra_assess/privacy/purge.py`: batch selection, ABANDONED + `purge_notice` rows, crypto-shred-then-delete transaction, tombstones, count verification with rollback on non-zero
- [x] Register the daily purge tick with plan 3's tick framework (advisory lock, separate pool, hard timeout); include done-job and expired-token cleanup
- [x] Add the 410 branch to `GET /transcripts/{tid}` and to the state payload's transcript links ("Trascrizione non più disponibile (periodo di conservazione scaduto)")
- [x] `vektra_assess/privacy/erasure.py` + `DELETE /privacy/subjects/{userid}`: live-session close, scoped purge, recipient/extra-time cleanup, mode switch, idempotence, audit
- [x] `GET /privacy/activities/{cmid}/subjects` (assess:privacy-export, site-bound, cmid re-resolved against the site binding): userids with any assess row for that activity; backs the plugin's `get_users_in_context` (added 2026-11-01, coordinator decision 3)
- [x] `DELETE /privacy/activities/{cmid}/subjects` (assess:privacy-erase, optional `userids` body): per-activity erasure reusing `erasure.py` scoped to (site, cmid[, userids]); backs `delete_data_for_all_users_in_context` / `delete_data_for_users`; same idempotence, mode switch and row-count verification; cross-activity and cross-site rows untouched (added 2026-11-01)
- [x] Startup: `VEKTRA_ASSESS_ERASURE_MODE` must be set explicitly outside development (fail closed: no implicit choice of the DPO's decision); warn when `VEKTRA_ASSESS_DB_BACKUP_RETENTION_DAYS` is unset

### Export
- [x] `GET /privacy/subjects/{userid}/transcripts`: subject-key resolution, pagination, no enrolment check, rate limit, audit

### Operator config and monitoring
- [x] Settings bounds validation for all operator-facing `VEKTRA_ASSESS_*` values (one startup step, `StartupValidationError` with remediation)
  (12b: every numeric setting bounded on both sides, guarded by a test; `explain_settings_error` names each rejected variable without echoing its value, used by API step 12 and by the worker before it opens a connection)
- [x] SMTP test-send endpoints + worker handler (uses plan 10's sender; recipient fixed by setting)
  (12b, revision 0042. Decisions: the fixed recipient is `VEKTRA_ASSESS_OPERATOR_EMAIL` (already mandatory with the relay) rather than a second `VEKTRA_ASSESS_OPS_TEST_RECIPIENT`: one operator address, one setting. The API cannot read a job back, so it generates the test id and the worker writes the result as an `assess_deliveries` row of kind `smtp_test` with THAT id; GET reads status / error class over the columns the API already may read, `queued` until the row exists. One attempt (`max_attempts=1`), one pending test at a time (429), a rerun after a crash never sends twice (`interrupted`). Rows deleted by the purge tick after 7 days. Audited: `assess_smtp_test_requested` (API), `assess_smtp_test_result` (worker))
- [x] Prometheus collector over `assess_metrics_snapshot()`; label-enum guard test
  (12b: revision 0041; the API refreshes the snapshot every 15 s in a background task, a scrape never waits on the database; values outside an enum are counted as `other`; `*_total` series count retained rows, so a purge can lower them; the purge records its runs in the `purge` heartbeat row: `ok`, `last_success`, cumulative `rows_deleted`)
- [x] `/health/assess` readiness payload; test that killing the worker makes `/health/assess` 503 while shallow `/health` stays 200
  (12b: content-free `details`: worker heartbeat age and state, oldest ready job, parser status from the new `parser_ping` tick, SMTP probe (the `last` verdict while busy), active vs configured privacy sites, API keys present, last successful purge age. A check marked `readiness = True` answers 503 unless `healthy`/`disabled`; every other component keeps its 200. Only the worker rows and the API's keys decide readiness: SMTP, parser and purge have alerts. Proven on real Postgres (`test_health_readiness_db.py`) and on the compose stack (`tests/assess/test_assess_stack.py`, CI job `assess-stack`))
- [x] Assess log allowlist processor in `vektra_app/main.py configure_structlog()` scoped to `vektra_assess.*`
  (12b: the processor itself shipped with plan assess-context (`vektra_shared.log_safety`, wired in `configure_structlog()` and in the worker's logging); 12b adds the missing "counted": every replaced key is counted once per record on both stacks and exposed as `vektra_assess_log_keys_dropped_total` on the API's /metrics)
- [x] `deploy/prometheus/assess-alerts.yml.example`: heartbeat > 60 s, oldest ready job > 30 s, FAILED deliveries > 0, RETRYING oldest > 1 h, TLS failures, purge last success > 26 h, purge verification failure, smtp probe down
  (12b: plus `VektraAssessMetricsDown` (every other rule is blind while the snapshot cannot be read); a unit test parses the file and fails if a rule names a series the collector does not emit)

### Keys
- [x] `python -m vektra_assess.keys verify` CLI: compares fingerprints of loaded keys (KEK, transcript key, teacher-config key, hand-off key, token signing key, subject-key secret) with the `key_version`/kid values referenced in the DB; non-zero exit on mismatch (detects a restore with the wrong key set)
  (12b: the DB stores kids, not fingerprints, so a kid match alone cannot catch a restore with other bytes under the same kid; the check therefore also OPENS one row per referenced kid (session DEK, transcript, reset and termination reasons) and reports `wrong_key`. Teacher-config and hand-off kids are checked for presence. Fingerprints are printed for comparing deployments. The token signing key has no DB reference, and there is no subject-key secret (the subject ref is an unkeyed hash): both stated in the output and the runbook. Exit 0 / 1 mismatch / 2 cannot run; `--role worker|api`)
- [x] `docs/guides/operators/assess-keys.md` (key inventory, which process holds what, loss consequences, offline backup separate from DB dumps; full rotation and restore drill in plan 13)
  (12b: written into the existing `docs/guides/assess-keys-runbook.md` (backup apart from dumps, restore drill and rotation were already there) rather than a second guide: key inventory with holder, DB reference and loss consequence, and the verify check)

### Tests, docs
- [x] Integration (testcontainers): sessions aged past retention → after purge, content row count 0 per table, only tombstones + audit remain; RETRYING delivery → ABANDONED, audited, purge_notice on the mail sink; injected failure mid-batch → rollback, 0 tombstones, alert; non-zero verification → alert and failure surfaced
- [x] Crypto-shred check: after deleting `kek_wrapped`, decryption of a pre-purge ciphertext copy taken in the test fails with the live KEK (proves the key, not just the row, is gone)
- [x] Erasure: subject with 3 sessions (one ACTIVE) → counts match, 0 rows remain, ACTIVE closed as access_revoked with 0 deliveries sent; repeat → counts 0; retain mode → 0 deletions + audit; other subjects' row counts unchanged
- [x] Export: unenrolled subject → transcripts #1..#N, each sha256 equal to the in-course download; cross-user → 0 foreign records; another key/site → 403; after purge → 0 items
- [x] REQ-080: admin key on download-token mint / transcript / export → 403; canary run (full flow with canaries in turns, files, solution) then grep of captured API, worker, parser logs, mail-sink server log and `/metrics` output → 0 hits; purge leaves 0 content rows
  (12b. Done: admin 403 on the download-token mint (`test_transcript_api.py::test_keys_are_exact_and_site_bound`, `test_session_mint.py`) and on export (`test_privacy_retention.py`); the canary grep script `tests/nfr/assess_canary_grep.sh` with its false-pass guards (a canary absent from the seed, a missing or empty sink, no canary: exit 2, never 0; `tests/nfr/test_assess_canary_grep.py`); `/metrics` label values outside the fixed enums are impossible by construction (`test_metrics.py`); the in-process canary matrix already greps the structured logs of every sink case (`test_canary_matrix.py`); purge leaves 0 content rows (12a). **BLOCKED: the compose run of the script over a full seeded discussion.** It needs a real discussion end to end on the compose stack (an LMS site with a released attempt carrying the canaries, an activated model, a relay sink in compose): none of the three is in the compose stack today (NFR-021 is BLOCKED on the same relay sink). Runner command for when they are: capture `docker compose logs` of `vektra`, `vektra-assess-worker`, `vektra-assess-parser` and the sink, plus `curl /metrics`, then `tests/nfr/assess_canary_grep.sh --canaries <list> --seed <seeded input> <files>`. Moved to plan 13 (NFR campaign).)
- [x] REQ-089: any privacy field unset → enable refused (plan 11 path) and setup check reports `privacy_config`; Start audit event carries `notice_version`; pre-session summary contains the mailbox sentence
- [ ] Docs: `docs/reference/api.md` privacy, ops and health sections; `docs/reference/configuration.md` + `.env.example`; `CHANGELOG.md` [Unreleased] (12a: the privacy routes, purge, erasure, export and their settings are documented; ops and health go with 12b)

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

- [x] Any privacy setting unset → enable refused and the setup check reports `privacy_config` MISSING; Start refused (REQ-089)
- [x] The Start audit event records the notice version; the pre-session summary contains the mailbox-retention statement (REQ-089)
- [x] Content older than RETENTION_DAYS → content row count 0 after purge, only tombstones and audit metadata remain; RETRYING deliveries ABANDONED, audited, graders notified (REQ-089, REQ-080)
- [x] After purge the student link returns 410 with the expiry message and the content is gone (row count 0) (REQ-088)
- [x] Erasure removes all the subject's content (row count 0 per table), leaves other subjects untouched, is idempotent, and never returns success without having erased or recorded a `retained` outcome (OQ-033)
- [x] Per-activity listing returns exactly the subjects with rows for that cmid; per-activity erasure leaves row counts of other activities and other sites unchanged (count-verified)
- [x] Export for an unenrolled subject returns #1..#N hash-equal to the in-course copy; cross-user returns 0 foreign records; after purge 0 transcripts (REQ-093)
- [x] An operator (admin) credential reading content gets 403; a canary grep of logs, mail logs and metrics returns 0; resend/test-send accept no recipient (REQ-080) (12b: 403 and no-recipient proven; the compose canary grep is BLOCKED as noted in Tests, moved to plan 13)
- [x] `/health/assess` reports readiness from worker-written rows and returns 503 with the worker stopped while `/health` stays 200 (COMP-002)

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

> [x] (done in 12a) Request from plan 10 (review of 10a, 2026-11-07): `GET /state` returns `transcripts[].purged`
> as the constant `false` because `shred_transcript` deletes the transcript row and leaves no
> trace. When this plan adds `assess_purge_records`, set `purged` from it in
> `vektra_assess.api.state.transcripts_of` (LEFT JOIN on `transcript_id` or `session_id`) and
> update the pinning test `test_session_state.py::test_transcripts_list_generated_and_shredded_ones`.

> [x] (done in 12a) Request from plan 10 (CodeRabbit 4503029411 on PR #159): `transcript_generate` must not
> recreate a transcript that was purged. When the purge tombstone lands, check it in
> `vektra_assess.worker.transcript_gen.generate_transcript` before regenerating (a retry of a job
> whose settlement failed could otherwise run after `shred_transcript`).

> **Split and design decisions (2026-11-09, coordinator; part 12a on `feat/assess-privacy-retention`).**
>
> - **Split.** 12a = Privacy config, Purge and erasure, Export, their tests and the docs of those
>   endpoints. 12b = Operator config and monitoring (bounds step, SMTP test send, metrics with
>   `assess_metrics_snapshot()`, `/health/assess`, log allowlist, alert rules), Keys (verify CLI,
>   key guide), the canary grep and the health / metrics tests. `assess_metrics_snapshot()` moved
>   to 12b with its only consumer. Also for 12b: the `delivery_retry` tick safety net for the
>   notify rows (a job that dies with the database down for its whole retry budget), and
>   `/health/assess` reading the `smtp` probe row; and the orphan `queued` / `running` setup-check
>   reports (a report whose job died stays queued: not purge-related, a reliability item).
> - **Revision 0040.** `assess_privacy_config` (CHECKs repeat the bounds; `is_configured`
>   re-checks on read), `assess_purge_records` (one per session, `UNIQUE (session_id)`, an
>   earlier tombstone is kept), and `assess_erasure_records` (revision 0012, never written) becomes
>   the erasure request the API queues and the worker completes (scope, cmid, `subject_userids`
>   cleared on completion by CHECK, outcome). Grants: the API reads/writes the settings, reads the
>   tombstones and its erasure records, and still deletes nothing; the worker reads the settings,
>   writes tombstones and gains DELETE on extra time and resets.
> - **The session row survives the retention purge** (ids, numbers, timestamps, state; no
>   content): the START gate counts CLOSED sessions against reset grants, so deleting them would
>   reopen an activity to a student, and deleting grants would close it. The reset grant stays too;
>   only the teacher's reason is overwritten (`reason_kid = 'purged'`, empty bytes), and the
>   teacher's list shows `reason_state: "purged"`. The test-wide REQ-067 invariant (a consumed
>   session has its opening turn) excludes tombstoned sessions by design.
> - **Erasure deletes the session rows** (the subject asked for its data to go): on an activity
>   still open the student could Start again. Erasure is an administrator's privacy action, not a
>   student's self-service; recorded here for the DPO. Live sessions are closed in their OWN
>   transaction first (`access_revoked`, the single close path): the REQ-067 constraint trigger
>   checks a CLOSED session's opening turn at COMMIT from the row the close wrote, so a close and
>   the deletion of that turn cannot share a transaction. Audit rows stay (append-only, content-
>   free). Teacher-entered recipient addresses are not tied to a student id (REQ-082 as amended),
>   so nothing is removed from the sealed configuration.
> - **The API never deletes.** Erasure is an `erasure` job; the API waits up to
>   `VEKTRA_ASSESS_ERASURE_WAIT_S` (4 s: the plugin's HTTP timeout is 5 s) and answers the counts,
>   or `503 ERR-ASSESS-027 pending` (the same request again is idempotent: a new request, counts 0
>   if already erased) / `failed`. Never a 2xx without an erasure or a `retained` outcome.
> - **Purge notice = the #190 notify path** (no third sender): a `teacher_notify` job with
>   `reason: delivery_abandoned_on_purge` and the ids it needs in its arguments (activity, student
>   id, session number), queued in the purge transaction itself (content-free, so it does not
>   need the purged rows; and a batch that rolls back queues no notice of a purge that did not
>   happen). This replaces "sent BEFORE step 3 is committed": the outbox gives the same guarantee
>   (the notice is durable once the purge is) without making the purge wait on the relay.
>   Erasure abandons copies too but sends no notice.
> - **Notification rows' retention** (request (c)): `teacher_notify` / `operator_notify` rows (the
>   purge notice included) in a final state are deleted by the purge tick after 30 days (ids and
>   codes only, kept for the operator meanwhile). Download tokens are deleted a day after expiry.
> - **In-flight copies**: a session with a delivery attempt started in the last 15 minutes waits
>   for the next daily run, so a send is never cut from under the relay.
> - **A site without settings is never purged** (no retention to apply); such a site cannot be
>   enabled outside development anyway.
> - **410** is answered at the download-token mint (the link the plugin follows); a token minted
>   before the purge is deleted with it (401 as any unknown token). The owner gets the 410 only for
>   their own transcript (the tombstone's subject reference), another student a 404.
> - **Subject reference**: `sha256("<site>|<userid>")`, a lookup key, not a secret (LMS user ids
>   are small integers).
> - **Error codes**: `ERR-ASSESS-026` (410 expired), `027` (503 erasure not completed), `028` (400
>   privacy settings refused).
> - **Notice version**: the Start records the site's `notice_version`; the deployment-wide
>   `VEKTRA_ASSESS_PRIVACY_NOTICE_VERSION` / `_URL` only stand in for a site with no settings
>   (development override).
> - **`VEKTRA_ASSESS_ERASURE_MODE`** is checked at the API's startup step (the API decides the
>   retained outcome), not in the settings model, so a dormant feature's setting never blocks a
>   disabled deployment.

> **Code review of 12a (2026-11-09, high, 10 findings).** Fixed, each with a test that failed on
> the earlier code:
> 1. queued jobs (render check, gate refresh, notices, per-session jobs) survived an erasure and
>    would write the subject's rows back: the erasure deletes the READY ones and counts them in
>    its verification. A job already leased runs to its end (residual: a render check in flight
>    at that instant can write one cache row back; the next erasure removes it);
> 3. a 503 `pending` retry inserted another request carrying the same ids, and a request whose
>    job died stayed incomplete with its ids forever: a retry now waits on the pending request
>    of the same scope, and the purge tick records a request older than a day `failed`
>    (`abandoned`), clearing its ids;
> 4. the activity listing missed subjects whose only rows are minted tokens or the render
>    worker's caches: one table list (`privacy.subject.SUBJECT_TABLES`) now drives the listing and
>    the erasure (the API gains SELECT on `assess_render_checks`);
> 5. an erased subject's tombstones kept `sha256(site|userid)` (trivially reversible): the
>    erasure writes `subject_ref = 'erased'`, blanks the subject's earlier retention tombstones
>    (worker UPDATE on `subject_ref` only) and clears the request's subject hash on completion;
> 6. a grader got 410 for a purged transcript of an activity they do not grade (an existence
>    oracle): only the owner gets 410, everyone else 404;
> 7. a too-short value was reported `too_long`: now `too_short` with `min`;
> 8. the purge UPDATEd delivery rows to ABANDONED just before deleting them: the abandonment is
>    recorded in the audit and the notice only;
> 9. / 10. the session -> transcript map was queried three times and the privacy-row read and the
>    subject listing had copies: one map from `shred_sessions`, one `READ`, one listing query.
>
> **Product owner decision (2026-11-09, finding 2): accepted as is, documented.** Erasure is
> started by an administrator or the DPO, not by the student, so using it to earn a fresh attempt
> is unlikely; the API reference states the consequence. No 409, no residual counter.
>
> Original note (finding 2): erasing a subject deletes their
> session rows, so on an activity still open the START gate counts 0 closed sessions and the
> student could Start again. The retention purge keeps the rows for this reason; an erasure
> cannot keep them without keeping "this user took the exam". Options: refuse an erasure while
> the activity is open (409), keep an anonymous per-activity counter, or accept it (the erasure
> is an administrator's action; `retain_exam_evidence` exists for exam evidence).

> **CodeRabbit round 1 on PR #191 (2026-11-09).** Fixed, each functional fix with a test that
> failed on the earlier code: an explicit `{"userids": null}` erased the whole activity
> (4516399722); a reset of a purged session wrote a reason the purge never scrubs: refused,
> `ERR-ASSESS-024` `purged` (4516399820); the `purged` marker could be a real key id: reserved
> in `load_keyring` (4516399792); a generation racing the purge inserted a transcript after the
> tombstone: `FOR KEY SHARE` on the session first (4516399835); an erasure now waits (job retried)
> while a leased job of the scope or a transcript send started in the last 15 minutes exists, and
> deletes finished jobs that still name the subject (4516399770, 4516399779, 4516399805); two
> identical erasure requests queued twice: advisory lock per scope (4516411675); the erasure
> wait now bounds the pool wait too (4516399711); a notice URL must name a host and never raise
> (4516411695); the purge validates a site's settings like `is_configured` (nitpick); a backlog
> above one run's cap queues a bounded follow-up `purge` job (outside-diff); compose forwards
> the API's privacy settings (outside-diff); the failed-erasure docs (4516399670).
>
> **Plugin requests P3 (vektra-moodle-discuss, owned by the plugin session), not done here:**
> - render `pre_session.privacy.summary` (it ends with the fixed `mailbox_notice`) before the
>   student confirms (CodeRabbit 4516399743). Once a plugin release does, the server raises the
>   minimum plugin version (`VEKTRA_ASSESS_MIN_PLUGIN_VERSION` / the site minimum) so an older
>   plugin can no longer Start;
> - echo the `notice_version` the student saw in the Start request, so the API can refuse a
>   Start whose notice changed meanwhile (CodeRabbit 4516399732). Until then the Start records
>   the site's version at commit; the window is the pre-session token's TTL (at most 120 s).

> **12b, the items moved from 12a (2026-11-10).**
> - `delivery_retry` safety net for the notification chains (`delivery.notify.rescue_dead_chains`,
>   run by the tick after the transcript sweep): live `teacher_notify` / `operator_notify` rows
>   older than 5 min whose chain has no ready or leased job get ONE new run with the newest chain
>   job's arguments (event, root, try and window start: the chain keeps its own backoff and
>   window). A chain whose jobs were all cleaned up has nothing to rebuild from: its rows become
>   FAILED `chain_lost` with the alert `notify_chain_lost`. Transcript rows already had their net
>   (`_DUE` re-queues every due transcript).
> - Abandoned setup-check reports (`worker.setup_check.fail_abandoned_reports`, hourly in the
>   `job_cleanup` tick): a `queued` / `running` report older than the kind's lease with no
>   `setup_check` job that could still run it becomes `failed` (it would otherwise read "queued"
>   forever and never be trimmed).
