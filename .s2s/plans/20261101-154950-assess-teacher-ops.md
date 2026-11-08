# Implementation Plan: Assess teacher operations (config, session list, reset, terminate, extra time, opening fallback)

**ID**: 20261101-154950-assess-teacher-ops
**Status**: active
**Branch**: feat/assess-teacher-ops (11a)
**Created**: 2026-11-01T14:49:50Z
**Updated**: 2026-11-01T20:58:19Z
**Approved**: 2026-11-01 (coordinator)

## Traceability

**Source**: COMP-002 (teacher endpoints of INT-001), ARCH-071 (write-only teacher config), ARCH-072 (RAG activation preconditions)
**Source Type**: architecture

## References

### Requirements
- REQ-076: Teacher in-Moodle session list and transcript download @.s2s/requirements.md
- REQ-077: Teacher configuration in the activity settings page @.s2s/requirements.md
- REQ-078: Per-student extra time (accommodations) @.s2s/requirements.md
- REQ-081: Teacher disables discussion, or terminates active sessions @.s2s/requirements.md
- REQ-082: Transcript recipients (graders only in v1) @.s2s/requirements.md
- REQ-085: Minimum audit event set for discussions (teacher events) @.s2s/requirements.md
- REQ-087: Teacher reset: grant a new session #N (with continuity rule) @.s2s/requirements.md
- REQ-090: Teacher preview mode (could) @.s2s/requirements.md
- REQ-091: Manual opening-date fallback when the effective close cannot be read @.s2s/requirements.md
- REQ-092: Student notification when a reset is granted (should) @.s2s/requirements.md
- BR-013: Teacher input validation @.s2s/requirements.md

### Architecture
- INT-001: Assess REST API (teacher endpoints; capability re-verified via gate_inputs or fail closed) @.s2s/architecture.md
- COMP-002: vektra_assess API @.s2s/architecture.md
- ARCH-071: Dedicated encrypted stores; teacher config write-only (sealed to worker key) @.s2s/architecture.md
- ARCH-072: Discussion retrieval isolation and RAG activation preconditions @.s2s/architecture.md
- ARCH-070: Capability matrix (readability of overrides/extensions drives REQ-091) @.s2s/architecture.md

### Decisions
- ADR-0030: Site-bound narrow scopes (assess:teacher) @.s2s/decisions/ADR-0030-discussion-token-contract-and-ui-embedding-separat.md
- ADR-0031: Envelope encryption; worker-only teacher-config private key @.s2s/decisions/ADR-0031-envelope-encryption-with-live-session-key-hand-off.md
- ADR-0032: LLM context boundary at capture time @.s2s/decisions/ADR-0032-llm-context-boundary-capture-time-minimisation-typ.md

### Dependencies
- 20261101-154950-assess-session-lifecycle (state model, START-gate evaluator, single close path, epoch bump)
- 20261101-154950-assess-transcript-delivery (`authz/graders.py`, delivery outbox with `kind=reset_notice`, delivery status)
- 20261101-154950-assess-moodle-ws (worker-written gate_inputs: graders, editors, activity facts, readability matrix)
- 20261101-154950-assess-data-crypto (sealed-box helper to the worker public key)

## Overview

Teachers act through the plugin (P3), which calls Vektra server-to-server with a site-bound
`assess:teacher` key and names the acting Moodle user. Vektra never trusts that claim on its own:
every teacher endpoint re-verifies the actor's role for that activity against fresh
worker-written `gate_inputs` and fails closed when they are missing or stale.

This plan delivers the configuration endpoint with BR-013 validation done in Vektra (the plugin
form is a convenience, not the authority), write-only sealed storage for the solution and the
pasted rubric, the session list, reset, terminate/disable, per-student extra time and the
REQ-091 attestation, all audited content-free. The RAG field ships locked OFF.

## Design Notes

- **Auth.** `require_exact_scope("assess:teacher")` (non-inheriting: an admin key gets 403) +
  site binding of the key. The actor is `actor_userid` in the body (or header
  `X-Assess-Actor` for GET), resolved against `gate_inputs` for `(site, cmid)`: `editors` for
  config endpoints (REQ-077 "activity editors only"), `graders` for list/reset/terminate/extra
  time/attestation. Freshness bound `VEKTRA_ASSESS_TEACHER_GATE_MAX_AGE_S` (default 120 s);
  stale or absent → 403 `ERR_ASSESS_TEACHER_UNVERIFIED`, and a `gate_refresh` job is enqueued so
  a retry can succeed. `{cmid}`, `{sid}`, `{userid}` are resolved server-side against the site and
  course (IDOR tests). Reuse `authz/graders.py` from plan 10, extended with `require_fresh_editor`.
- **Config storage (revision 0036, as built in 11a).** `assess_activity_config` keeps its existing
  columns (`enabled`, `flags` with `show_solution` / `rubric_source`, `rag_enabled`, `end_at` = the
  end date "disponibile fino al", `attested_opening_at` / `attested_by`, `cap_minutes`) and gains
  `version` (optimistic concurrency: `expected_version` in the request; mismatch → 409) and
  `updated_by`; `CHECK (rag_enabled = false)`; the composite `teacher_config_sealed` / `_kid` /
  `_set` and the dormant `recipients_extra` are dropped.
  `assess_activity_config_sealed(site_id, cmid, field ∈ {solution, pasted_rubric, transcript_recipients}, sealed_blob, kid, is_set, refs, item_count, updated_by, updated_at)`:
  one row per field, each sealed under the binding `[site_id, cmid, field]`; a cleared field keeps
  its row with a NULL blob. The API role has INSERT/UPDATE and SELECT on the key columns, `is_set`
  and `item_count` only, never `sealed_blob`, `kid` or `refs` (grant matrix); the worker SELECTs and
  opens the fields at snapshot capture and delivery. A SECURITY DEFINER trigger audits each write
  of a sealed field old/new (set / replaced / cleared; recipient refs), since the API cannot read
  the old refs. Sessions copy what they need at Start (`duration_s`, `cap_s`, the snapshot), so a
  change applies only to sessions not yet started; no per-session `config_version` is needed.
- **Write-only semantics (OQ-034 default).** Request field absent = unchanged; `null` = clear;
  string = replace. Responses expose only `solution_state` / `pasted_rubric_state` ∈
  {`impostata`, `non impostata`}. Text is normalised (NFC, control characters except `\n`/`\t`
  removed), length counted in code points after normalisation (the plugin's live counter must use
  the same rule), then sealed with the worker's X25519 public key. Never logged, never in audit.
- **BR-013 / REQ-077 validation in Vektra.** Field-level errors return 400
  `ERR_ASSESS_CONFIG_INVALID` with `details.fields=[{field, code, range}]` (explicit envelope; not
  the 422 path that bypasses it, DEBT-035). Rules: cap 5-120; solution and pasted rubric ≤ 20,000;
  pasted source with empty rubric → error; total duration ≤ 240 min; `available_until` > effective
  opening; recipients 1-3 graders of the course (default: the configuring teacher). Enable is
  refused when: quiz without close date / assignment without cut-off (BR-007), Moodle-rubric
  source without a rubric, team submission, marking-guide grading (EX-023), privacy settings
  unset (`vektra_assess.privacy.is_configured(site)` from plan 12; until it lands a stub returns
  False, i.e. fail closed), setup check not green. Moodle facts come from fresh gate_inputs; stale
  → refuse enable with `gate_inputs_stale`.
- **RAG locked OFF (ARCH-072).** `rag_enabled=true` → field error `rag_unavailable` with reason
  `operator_flag_off`. The DB CHECK makes ON unrepresentable. The operator flag
  `VEKTRA_ASSESS_RAG_ENABLED` exists only so that setting it is REFUSED at startup ("not
  available in this release"): no dormant path. Real enablement (preconditions 2-4) is a future
  BACKLOG item.
- **Recipients (REQ-082 as amended 2026-11-07; supersedes "Moodle-resolved graders only").**
  `transcript_recipients` in the PUT body is 1..3 free-text addresses, validated in Vektra with
  `delivery/address.normalize_address` and the mandatory allowlist `VEKTRA_ASSESS_RECIPIENT_DOMAINS`
  (empty refuses all): an address outside it is the field error `domain_not_allowed` naming the
  allowed domains; duplicates (case-insensitive) collapse. Sealed as a JSON list in the
  `transcript_recipients` row. Changes are audited old/new as `recipient_ref`s
  (`sha256(site_id|address)`), never an address. GET returns only `transcript_recipients_state`
  (`impostata` / `non impostata`) and a count. Enable requires at least one recipient.
- **Session list (REQ-076).** `GET /activities/{cmid}/sessions?cursor=` returns per student
  (Moodle userid only; names are resolved by the plugin): sessions #1..#N with state, close reason,
  delivery status per recipient (incl. FAILED/bounce), transcript id, reset count and reasons,
  extra time, `reset_notice` status or "studente non avvisato via email"; activity-level gate
  panel: effective opening date, the override holder's userid only if the actor has the
  override-viewing capability in gate_inputs (never the reason), C7 fallback reason
  "Vektra non può verificare proroghe/override per questa attività", should-hint for
  suspended/unenrolled holders, and "N sessioni attive".
- **Reset reasons and terminate reasons** are teacher-entered text: stored in `assess_resets`
  / `assess_terminations` encrypted under the transcript key (API-readable for the list), never
  in transcripts, metrics or operator-visible audit. Audit rows reference `reset_id` only. This
  deviates slightly from REQ-085 ("reason stored in audit"): the reason is reachable from the
  audit entry but kept outside the operator-readable audit table.
- **Reset (REQ-087).** Only when the latest session is CLOSED (else 409); mandatory reason
  (1-1000 chars); re-runs all START gates via plan 8's evaluator including the end date (refusal
  points to `available_until`); creates `assess_resets(record_id, session_no, reason_ct, include_previous_turns, created_by)`
  with UNIQUE `(record_id, session_no)` so concurrent resets yield one. `include_previous_turns`
  defaults OFF; auto-ON when the previous close reason is `service_failure`. Enforcement of the
  continuity boundary (c)-(e) lives in plan 6's `assemble_context`; this plan only stores the
  choice and tests the end-to-end outcome.
- **REQ-092 notice (should).** `VEKTRA_ASSESS_RESET_NOTICE_ENABLED` default false (gated on
  OQ-020 DPO approval). When true, the reset transaction inserts a `kind=reset_notice` delivery
  row (plan 10 outbox); the worker resolves the student's address and language at send time; body
  contains the activity name and the plain Moodle results-page URL from gate_inputs, no token,
  no reason. Failure never reverts the reset. `reset_notice_sent` audited on SENT.
- **Terminate (REQ-081).** `POST /activities/{cmid}/terminate {actor_userid, reason, expected_active_count, also_disable}`.
  One transaction: lock the activity's live sessions; if their count ≠ `expected_active_count` →
  409 with the current N; else close each ACTIVE/OUTAGE session through plan 8's single close
  path as CLOSED(terminated_by_teacher) (handoff deleted, epoch bumped, transcript job queued);
  STARTING sessions are rolled back to ELIGIBLE (not consumed) and, when `also_disable`, cannot
  restart. Disable alone is `PUT config enabled=false`; mint and Start check `enabled` (plan 8
  gates) so the effect is immediate. Re-enable never reopens CLOSED.
- **Extra time (REQ-078).** `PUT /activities/{cmid}/students/{userid}/extra-time {minutes|percent, confirm_over_100}`.
  0-200% or 0-240 min, total ≤ 240 prevails, > +100% requires `confirm_over_100=true`. No reason
  field exists. Refused when the student's current session is STARTING/ACTIVE/OUTAGE/CLOSED
  without a pending reset. Plan 8 reads it at Start to compute the deadline.
- **Opening fallback (REQ-091).** `PUT /activities/{cmid}/opening-fallback {opening_at, attestation:true}`.
  Accepted only when the readability matrix marks any BR-006(d) input unreadable (partial =
  unreadable); `opening_at` ≥ activity close/cut-off; after any session of this cmid started,
  only later moves; attestation required on every edit; audited with teacher and attestation text
  version. The BR-006(d) evaluator (plan 8) gains `gate = max(effective_close_or_NOT_AVAILABLE, teacher_date)`;
  no teacher date in fallback → NOT_AVAILABLE; readable instance → `max(automatic, teacher_date)`.
- **Preview (REQ-090, could).** Not built in v1. The documented alternative is a Moodle
  test-student account with REQ-087 resets. A route-inventory test asserts no preview route
  exists, so the capability cannot appear dormant.

## Tasks

### Foundation
- [x] (added, 11a) Sealed storage per field: revision 0036 `assess_activity_config_sealed` (one row per `solution` / `pasted_rubric` / `transcript_recipients`, binding `[site_id, cmid, field]`), `crypto/teacher_config.py` `store_sealed_field` (API) / `read_teacher_config` (worker), `start_source.py`, `delivery/recipients.py`, `crypto/rotation.py` switched; composite `teacher_config_*` and dormant `recipients_extra` dropped; `version`, `updated_by`, `CHECK (rag_enabled = false)`; DEFINER trigger auditing sealed writes old/new (`test_teacher_config_schema.py`, `test_teacher_config_format.py`, `test_shred_rotation.py`, `test_grant_matrix.py`)
- [ ] Migration (next revision): `assess_activity_config`, `assess_activity_config_sealed`, `assess_resets`, `assess_terminations`, `assess_extra_time`; CHECK `rag_enabled = false`; grants (API: no SELECT on `_sealed`); extend plan 2's grant assertion
- [x] (added, 11a) Activity-level teacher facts: `assess_teacher_gate(site_id, cmid, inputs, refreshed_at)` (revision 0036, API SELECT, worker writes) filled by the new `teacher_gate` job (`worker/teacher_gate.py`, `MoodleStartSource.teacher_facts`): the cmid resolved inside the hinted course only, editors (`moodle/course:manageactivities`, new `read_editors`, same WS function as the graders), graders, close date, team submission, grading method, Moodle rubric; ids and flags only (`test_teacher_gate.py`; editors request SYNTHETIC from the graders recording, live read BLOCKED, see Notes)
- [x] `vektra_assess/authz/teachers.py`: `require_fresh_editor` / `require_fresh_grader` / `can_view_overrides` over gate_inputs with max age, fail closed + `gate_refresh` enqueue (11a: `check_fresh_editor` in `api/teachers.py` over `assess_teacher_gate`, stale / absent / unreadable -> 403 `ERR-ASSESS-021` `editor_check_stale` and a `teacher_gate` job per hint, `test_teacher_authz.py`; `require_fresh_grader` / `can_view_overrides` moved to the 11b task below)
- [x] `vektra_assess/teacher/schemas.py`: Pydantic models (`extra="forbid"`), write-only field tri-state, field-error envelope builder; error codes `ERR_ASSESS_CONFIG_INVALID`, `ERR_ASSESS_TEACHER_UNVERIFIED`, `ERR_ASSESS_CONFIG_CONFLICT`, `ERR_ASSESS_RESET_REFUSED`, `ERR_ASSESS_TERMINATE_COUNT_MISMATCH` in `vektra_shared/errors.py` + `docs/reference/error-codes.md` (11a: `ConfigPut` strict, `FieldError`; CONFIG_INVALID = `ERR-ASSESS-022` 400 with `details.fields`, CONFIG_CONFLICT = `ERR-ASSESS-023` 409, TEACHER_UNVERIFIED = the existing `ERR-ASSESS-021`; `test_teacher_schemas.py`. RESET_REFUSED and TERMINATE_COUNT_MISMATCH arrive with their 11b routes, task below)
- [x] Startup step: refuse `VEKTRA_ASSESS_RAG_ENABLED=true`; refuse `VEKTRA_ASSESS_RESET_NOTICE_ENABLED=true` unless `VEKTRA_ASSESS_RESET_NOTICE_TEXT_VERSION` is set (records the DPO-approved text) (a settings validator: both the API (step 12, assess on and off) and the worker refuse; `test_config.py`, `test_assess_startup.py::test_the_api_refuses_the_rag_flag`)
- [x] (added, 11a) Privacy precondition stub: `vektra_assess/privacy.py` `is_configured(conn, site)` returns False (fail closed) until plan 12 binds the REQ-089 store; the Start gate's `privacy_not_configured` delegates to it (`test_teacher_schemas.py::test_privacy_is_not_configured_until_plan_12`)

### Config
- [x] `teacher/validation.py`: BR-013 + REQ-077 rules as pure functions over (request, gate_inputs facts, privacy status, setup-check status); one test per boundary (20,000/20,001; cap 4/5/120/121; total 240/241; +100/+101 confirm; empty pasted rubric; end date ≤ opening) (`test_teacher_validation.py`; also recipients 1..3 + allowlist, RAG, rubric source per type, the enable refusals; `check_extra_time` is ready for the 11b endpoint)
- [x] `PUT /api/v1/assess/activities/{cmid}/config`: editor check, validation, seal secret fields, versioned insert, audit old/new (flags/values; secret fields as `replaced|cleared` only); `GET` (ratified in INT-001 on 2026-11-01) returns the computed opening condition, end date, review-options warning, `N sessioni attive`, `impostata/non impostata` (`api/teacher_config.py`; `test_teacher_config_api.py`: defaults, save + worker opens the fields + audit old/new with 0 secret content, absent/null tri-state, 409 on a stale version with 0 change, field errors naming ranges with 0 change, enable refusals, 403 stale / not-an-editor / admin / mint / other site, audited and 0 change)
- [x] Wire `rag_enabled` rejection and the free-text recipient rejection with their field codes (superseded for recipients by REQ-082 as amended: free text IS v1; an address outside `VEKTRA_ASSESS_RECIPIENT_DOMAINS` is `domain_not_allowed` with `allowed_domains`, never the address; RAG ON is `rag_unavailable` / `operator_flag_off`)
- [x] Worker side: snapshot capture (plan 6) opens the sealed blobs for the session's `config_version`; add a test that the API process cannot `SELECT` `_sealed` (permission denied asserted) (no `config_version`: the capture runs in the Start's pre-flight and opens the current fields, solution only with the toggle ON, pasted rubric only with the source "pasted", `test_teacher_config_format.py`; SQLSTATE 42501 on `sealed_blob`, `kid`, `refs`, `SELECT *` and a predicate on the blob, `test_teacher_config_schema.py`, `test_teacher_config_api.py`)

### Teacher operations
- [ ] (moved from Foundation, 11b) error codes RESET_REFUSED and TERMINATE_COUNT_MISMATCH (next free `ERR-ASSESS-0NN`) with their routes
- [ ] (moved from Foundation, 11b) `require_fresh_grader` / `can_view_overrides` in `api/teachers.py` over `assess_teacher_gate` (graders and the override-viewing capability of the activity), with their routes
- [ ] `GET /activities/{cmid}/sessions`: list per Design Notes, cursor pagination, decrypt reset/terminate reasons for the grader view only
- [ ] `POST /activities/{cmid}/sessions/{sid}/reset`: CLOSED check, reason, START-gate re-run, unique session_no, continuity flag, audit `reset`; REQ-092 notice row when enabled
- [ ] `POST /activities/{cmid}/terminate`: count check, single-close-path loop in one transaction, STARTING rollback, optional disable, audit with `termination_id`
- [ ] `PUT /activities/{cmid}/students/{userid}/extra-time`: validation, state check, audit
- [ ] `PUT /activities/{cmid}/opening-fallback`: readability check, bounds, later-only rule, attestation, audit; extend plan 8's BR-006(d) evaluator with the max() rule
- [ ] Route-inventory test: no preview route; every teacher route declares `require_exact_scope("assess:teacher")`

### Tests, docs
- [ ] IDOR matrix (integration, testcontainers): other-course teacher, non-grader, grader of another cmid in the same course, cross-site key, admin key → 403 on every teacher route, with audit rows counted
- [ ] Reset tests: no reason / ACTIVE → refused; after end date refused, after moving date accepted; #1..#N-1 transcript bytes unchanged (hash) and #N created; service_failure → previous turns in context, other reason unticked → 0 previous-turn canaries in the captured LLM request bytes (with plan 6/7 recording provider); narrowed boundary → 0; reset reason canary → 0 in transcripts and metrics
- [ ] Terminate tests: N=3 ACTIVE → 3 CLOSED(terminated_by_teacher), 3 transcript rows, 0 handoff rows, audit with reason reference; N mismatch → 409, 0 state changes; after disable, mint and Start on an ELIGIBLE record refused; disable without terminate → ACTIVE sessions still accept turns and close normally; re-enable → CLOSED stays CLOSED
- [ ] Extra-time tests: +50% on 30 min → deadline exactly 45 min after commit (plus outage credit); change while ACTIVE/CLOSED refused; reason absent from every response and transcript
- [ ] REQ-091 tests across the readability matrix fixtures (quiz overrides readable + assignment extensions unreadable → fallback active)
- [ ] REQ-092 tests (flag on): one notice per reset on the mail sink, no reason/token in body or link; sink down → reset effective, state (10); flag off → 0 notices and list shows "studente non avvisato via email"
- [ ] Docs: `docs/reference/api.md` teacher endpoints; `docs/reference/configuration.md` + `.env.example` (`VEKTRA_ASSESS_TEACHER_GATE_MAX_AGE_S`, `VEKTRA_ASSESS_RAG_ENABLED` documented as refused, `VEKTRA_ASSESS_RESET_NOTICE_*`); `CHANGELOG.md` [Unreleased]

## State & Data Lifecycle

| State element | Created by | Updated by | Invalidated/Deleted by |
|---------------|-----------|------------|------------------------|
| `assess_activity_config` versions | `PUT config` (first save) | each `PUT config` / opening-fallback (new version row) | plan 12 erasure removes actor ids where required; kept for audit while any session references the version; purge of the activity's sessions then old versions by plan 12 |
| `assess_activity_config_sealed` blobs | `PUT config` with solution/rubric | replace = new version row; clear = no row for the new version | rows for versions no longer referenced by a retained session deleted by plan 12 purge |
| `assess_resets` (reason ciphertext) | reset endpoint | never | plan 12 purge/erasure with the subject's sessions |
| `assess_terminations` | terminate endpoint | never | plan 12 purge when no referenced session remains |
| `assess_extra_time` | extra-time endpoint | extra-time endpoint (before start only) | consumed at Start (copied into the session); deleted with the subject by plan 12 |
| `reset_notice` delivery rows | reset transaction | plan 10 delivery handler | plan 12 purge |
| gate_inputs freshness (read only) | worker (plan 4) | worker refresh tick / `gate_refresh` job | superseded by next refresh |

## Acceptance Criteria

- [ ] Non-grader, other-course teacher, cross-site key and admin key each get 403 on every teacher route, with an audit row per rejection (REQ-076, INT-001)
- [ ] Stale or missing gate_inputs → 403 `ERR_ASSESS_TEACHER_UNVERIFIED`, 0 state change (INT-001)
- [ ] New activity: RAG OFF, mostra soluzione OFF, end date empty; saving RAG ON → field error with reason; startup with the RAG flag ON refuses (REQ-077, ARCH-072)
- [ ] BR-013 boundary set passes exactly as specified; each error names the range (BR-013, REQ-077)
- [ ] Enable refused with a field error for: no quiz close date, no assignment cut-off, team submission, marking guide, empty pasted rubric, privacy unset, setup check red (REQ-077)
- [ ] After saving a solution, the API role's `SELECT` on the sealed table fails with permission denied and GET shows only "impostata" (ARCH-071, OQ-034)
- [ ] Changing the cap after a session started leaves its deadline unchanged; every change has an audit row old/new with 0 secret content (REQ-077, REQ-085)
- [ ] Reset criteria of REQ-087 pass, including canary counts on the captured LLM request bytes (REQ-087)
- [ ] Terminate moves exactly N sessions to CLOSED(terminated_by_teacher) with N transcripts and 0 remaining handoff rows; disable blocks mint/Start immediately; re-enable never reopens (REQ-081)
- [ ] +50% on 30 min → 45 min server deadline; extra time immutable once ACTIVE/CLOSED; no reason anywhere (REQ-078)
- [ ] REQ-091 acceptance set passes including later-only moves and max() once readable (REQ-091)
- [ ] With notice flag ON: exactly one notice per reset, no reason/token in it, failure does not revert; OFF: 0 notices and the list flag shown (REQ-092)
- [ ] No preview route exists (REQ-090 not built; dormant-capability rule)

## Testing Approach

Validation rules are pure functions tested at every boundary. Endpoints are tested against a
real Postgres with the restricted API and worker roles (so "API cannot read the solution" is a
permission-denied error, not a code path that happens not to read). Moodle facts are supplied by
seeding gate_inputs rows as the worker would; one end-to-end run per Moodle version (4.3, 5.2)
uses plan 4's dev envs. Anti-leak outcomes (reset continuity, sealed solution) are asserted on
the recording LLM provider's captured request bytes, not on bot replies.

### Test Infrastructure

- **Required**: testcontainers pg16 with the assess roles; gate_inputs seeding helpers; recording `LLMProvider`; mail sink (REQ-092); Moodle 4.3 / 5.2 dev envs for the e2e pass
- **Provided by**: roles/grants from 20261101-154950-assess-data-crypto; gate_inputs writer and dev envs from 20261101-154950-assess-moodle-ws; recording provider from 20261101-154950-assess-llm-runner; mail sink from 20261101-154950-assess-transcript-delivery
- **False-pass guard**: integration tests connect with the restricted API role and fail if the connection role is a superuser (asserted at fixture setup); the IDOR matrix asserts the number of executed cases equals the declared matrix size; Moodle e2e reports BLOCKED (not passed) when a dev env is unavailable

## Integration Notes

- Plugin P3 renders the settings section, list, dialogs and the live counter; it must send
  `actor_userid`, `expected_version` and `expected_active_count`, and handle field-level errors by
  `field` code. It never caches solution text after save.
- Plan 8 must read `enabled`, `available_until`, extra time and the opening fallback in its gate
  evaluator and at mint; plan 6 must honour `include_previous_turns` and the BR-011 narrowing rule.
- Plan 12 provides `privacy.is_configured(site)` and purges the tables listed above.

## Notes

- **OQ-025 spike (2026-11-01)**: on Moodle 4.3, quiz overrides and per-student time limits are unreadable, so the REQ-091 attested-date UI is the EXPECTED path for any quiz with user/group overrides; the teacher page must explain why (not an error). On 5.2 they are readable. Grace is readable only while the quiz is open; enabling the discussion after close also lands on REQ-091. Rubric descriptor attestation is a possible addition pending OQ-035.

- OQ-034: write-only default implemented; if product requires re-display, the API needs a
  symmetric teacher-config key (about 1 day, documented exposure).
- OQ-020: REQ-092 notice ships disabled until DPO approval of the flow and text.
- Future BACKLOG items (not created here): RAG-ON enablement per ARCH-072 preconditions 2-4;
  free-text recipients with a site-admin domain allowlist; site-admin emergency stop (REQ-081
  should); REQ-090 preview (must pass the full START-gate, NFR-015/NFR-017 and audit suites).
- Risk: the actor is asserted by the plugin and only role-checked by Vektra; a leaked
  `assess:teacher` key can act as any grader of that site. Mitigated by site binding, narrow
  scope and audit; residual risk accepted in ARCH-067.
- Dependency risk: editors/graders/override-capability sets must be in plan 4's gate_inputs; if
  not, this plan adds them to the refresh step.

> Note (independent review of W1, PR 146, 2026-11-03): the review-HTML sanitiser output (`SanitizeResult.text`, type `PlainText`, `vektra_assess.parser_protocol`) is TEXT, not markup, and is NOT escaped: it can contain literal `<img onerror=...>` (decoded entities) or `<math>...</math>` source. **Every consumer of `PlainText` in this plan must HTML-escape it at the render site** (`parser_protocol.html_escape`, or a contextual escaper) **and must have a test that proves it** (feed `&lt;img src=x onerror=alert(1)&gt;`, assert nothing in the rendered output parses as an element). The same holds for file text returned by `parse_file` (`ParseResult.text`) when it is rendered.

### Design decisions taken in 11a (one line each, with the reason)

- 11a = Foundation + Config on `feat/assess-teacher-ops`; 11b = Teacher operations (sessions list, reset, terminate, extra time, opening fallback, route inventory), next PR.
- Sealed storage per field, not one composite blob (coordinator, 2026-11-08; supersedes the 10b "Requests from 10b" format): a PUT changes one field (absent / null / string, OQ-034) and the API can never read the others back to merge them, and the plugin never caches the solution. One row per field in `assess_activity_config_sealed`, each under the binding `[site_id, cmid, field]`; the composite columns are dropped (dormant-path rule).
- Clearing a field is an UPDATE to a NULL blob (the row stays, `is_set` false), not a DELETE: the API needs no DELETE grant, and the row keeps the clearing actor (`updated_by`) for the audit.
- The API's upsert repeats the bound parameters in `DO UPDATE SET` instead of `EXCLUDED.col`: verified on pg16 that an `EXCLUDED` reference needs SELECT on that target column (permission denied for `sealed_blob`), so `EXCLUDED` would force the very grant ARCH-071 forbids.
- Sealed-field changes are audited by a SECURITY DEFINER trigger (`assess_audit_sealed_change`, nobody may EXECUTE it; it fires for the API role without EXECUTE, verified) from OLD/NEW: the API cannot read the old recipient refs it must audit (REQ-085 old/new); it fires on `UPDATE OF updated_at`, which a key rotation never sets, and skips a re-sent identical recipient list. Non-secret fields are audited by the API in Python.
- Existing columns reused: `enabled`, `flags.show_solution` / `flags.rubric_source` (the capture and gates already read them from `flags`), `rag_enabled`, `end_at` (= "disponibile fino al"), `attested_opening_at` / `attested_by`, `cap_minutes`; added only `version` and `updated_by`. `recipients_extra` was never read: dropped.
- No per-session `config_version`: a session copies at Start what the config gives it (`duration_s`, `cap_s` from plan 10, the snapshot with the solution), so a later change cannot reach a started session (tested on the deadline).
- Editors and the enable facts come from a new activity-level `assess_teacher_gate` (site, cmid), not `assess_gate_inputs`: the latter is keyed per student (render check), and a config request names no student (the plan's "dependency risk" note foresaw adding them). The worker resolves the cmid only inside the course the plugin hints (`resolve_in_course`), so a hint of another course verifies nobody; the row records the hint it answers, and a request with another hint queues its own refresh (dedupe per hint), so a wrong hint cannot block a right one for long.
- Authz lives in `api/teachers.py` next to `api/graders.py`, not in a new `authz/` package (same reason as 10a: a new top-level child changes the designed layout and every import-contract list); `ERR-ASSESS-021` is reused for the editor check (`details.reason` `not_an_editor` / `editor_check_stale`) rather than a new code: same meaning, same retry semantics.
- `teacher/` is a new top-level package (the plan's own layout: pure, API-importable, added to the import-contract lists and the scaffold's expected set); `privacy.py` is top-level too because plan 12 owns `vektra_assess.privacy` and the worker's Start gate now calls the same stub.
- Field errors are `ERR-ASSESS-022` with `details.fields=[{field, code, range?, ...}]`, never the value; the schema keeps BR-013 ranges OUT of the Pydantic model (a cap of 121 must reach the rule that names "5-120"), while wrong types and unknown fields stay the `ERR-ASSESS-014` 400 of `parse_body`.
- Text that is empty after normalisation counts as cleared (an empty string is not a "set" solution); quiz + rubric source `moodle` is a field error (REQ-077: Moodle rubric for assignments, pasted text for quizzes); the empty pasted rubric is refused on every save, not only on enable (BR-013).
- The end date is checked against the effective opening = the later of the activity's own close (worker fact) and the attested REQ-091 date; unknown opening is `opening_unknown`. Participant overrides are not in the teacher facts: the Start gate's BR-006(d) evaluation stays authoritative.
- PUT re-checks the Moodle facts, privacy and setup check only when the request itself switches `enabled` on; a save of an already enabled activity re-checks what it can break (cap, rubric source, pasted rubric, recipients). The Start gates re-check the Moodle facts at every Start.
- GET takes the actor and the hints as headers (`X-Assess-Actor`, `X-Assess-Course`, `X-Assess-Activity-Type`); no query parameters on teacher routes. The review-options warning is `review_options_hide_content` = a quiz on a site whose `review_html_filterable` capability is not readable (ARCH-070: quiz detail reduced); per-quiz review options are per-student WS reads and are not in the teacher facts.
- The config body cap is 256 KiB (`MAX_CONFIG_BODY_BYTES`) instead of the shared 64 KiB: two 20,000-code-point texts of up to 4 UTF-8 bytes each, JSON-escaped.
- The 0036 upgrade refuses a database holding a composite sealed config instead of dropping it: no release wrote one (no writer existed), and it cannot be split without the worker's private key.

