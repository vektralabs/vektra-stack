# Implementation Plan: Assess transcript generation, safe serving and durable SMTP delivery

**ID**: 20261101-154950-assess-transcript-delivery
**Status**: active
**Branch**: feat/assess-transcript-delivery
**Created**: 2026-11-01T14:49:50Z
**Updated**: 2026-11-07T17:00:00Z
**Approved**: 2026-11-01 (coordinator)

## Traceability

**Source**: COMP-007 (vektra_assess.transcript, vektra_assess.delivery), ARCH-067 (transcript download token)
**Source Type**: architecture

## References

### Requirements
- REQ-074: Transcript content and integrity @.s2s/requirements.md
- REQ-075: Durable transcript delivery by Vektra SMTP @.s2s/requirements.md
- REQ-082: Transcript recipients: course graders, optional allowlisted free text @.s2s/requirements.md
- REQ-088: Student access to own transcript @.s2s/requirements.md
- REQ-102: Student in-session controls (turn-hash re-verification at generation) @.s2s/requirements.md
- BR-016: Transcript format, naming and safe serving @.s2s/requirements.md
- NFR-016: Accessibility of the discussion UI and transcript (transcript part) @.s2s/requirements.md
- NFR-021: Transcript availability and delivery timing @.s2s/requirements.md

### Architecture
- COMP-007: vektra_assess.transcript and vektra_assess.delivery @.s2s/architecture.md
- ARCH-067: Discussion token contract (transcript download token) @.s2s/architecture.md
- ARCH-068: Transactional outbox on the leased job loop @.s2s/architecture.md
- ARCH-071: Key matrix (transcript key in both processes; SMTP credentials worker-only) @.s2s/architecture.md
- ARCH-077: Assess data model (assess_transcript_download_tokens) @.s2s/architecture.md
- INT-003: Worker to SMTP relay (AUTH only after TLS; probe never sends) @.s2s/architecture.md

### Decisions
- ADR-0029: Postgres-native leased job loop with DB invariants and outbox @.s2s/decisions/ADR-0029-postgres-native-leased-job-loop-for-assess-with-db.md
- ADR-0030: Token contract (download token, aud=transcript) @.s2s/decisions/ADR-0030-discussion-token-contract-and-ui-embedding-separat.md
- ADR-0031: Envelope encryption with live-session key hand-off @.s2s/decisions/ADR-0031-envelope-encryption-with-live-session-key-hand-off.md

### Dependencies
- 20261101-154950-assess-job-loop (job claim/lease, ticks, NOTIFY, per-kind pools and timeouts)
- 20261101-154950-assess-session-lifecycle (single close path inserting the `generate_transcript` job; persisted turns with per-turn hash; Part B "Punti contestati" output)
- 20261101-154950-assess-data-crypto (AES-256-GCM helpers, key loading by kid, grant matrix test)
- 20261101-154950-assess-moodle-ws (graders reader, address resolution, worker-written gate_inputs)

## Overview

When a session reaches CLOSED, the close transaction (plan 8) enqueues `generate_transcript`.
This plan builds that handler and everything downstream: an inert-by-construction HTML transcript
encrypted under its own DEK, one outbox row per recipient written in the same transaction, a
worker-only SMTP sender with mandatory TLS and a 72 h backoff, and the API endpoints that let the
owner or a grader download the exact same bytes through a single-use download token.

The rule from `.claude/CLAUDE.md` applies with full force: a delivery that "succeeded" is
evidence of nothing until the mail sink holds the message, and a transcript that was
"generated" is evidence of nothing until its bytes re-hash to the stored value.

## Design Notes

- **Package layout.** `vektra_assess/transcript/` (`render.py`, `templates/transcript.html.j2`,
  `allowlist.py`, `mathml.py`, `naming.py`, `hashing.py`, `store.py`, `handler.py`) and
  `vektra_assess/delivery/` (`outbox.py`, `message.py`, `smtp.py`, `backoff.py`, `handler.py`,
  `probe.py`). The worker imports both; the API imports only `transcript.store` (decrypt + serve)
  and `delivery.outbox` (resend enqueue). Import-linter contract: the API side never imports
  `delivery.smtp` (so `aiosmtplib` stays out of the API process).
- **Tables (next revision in the chain).**
  `assess_transcripts(id, session_id UNIQUE, ciphertext, nonce, dek_wrapped, key_version, content_sha256, turns_digest, filename, byte_size, generated_at)` —
  the per-transcript DEK is wrapped by the transcript key (held by both processes, ARCH-071),
  so purge can crypto-shred a transcript by deleting `dek_wrapped`.
  `assess_deliveries(id, transcript_id NULL, kind ENUM(transcript, reset_notice), round, recipient_kind ENUM(grader, student), recipient_userid, status ENUM(PENDING, RETRYING, SENT, FAILED, ABANDONED, REPLACED), attempts, not_before, attempt_started_at, last_error_class, smtp_code, message_id, sent_at, created_at, UNIQUE(transcript_id, round, recipient_userid))`.
  `kind=reset_notice` is the hook plan 11 uses for REQ-092. Addresses are NOT stored: they are
  re-resolved from Moodle at send time (REQ-082).
  `assess_transcript_download_tokens(jti PK, transcript_id, sub, audience_role ENUM(owner, grader), expires_at, consumed_at)`.
  Grants: worker writes transcripts and deliveries; API SELECTs transcripts and delivery status
  columns, INSERTs/UPDATEs download tokens and INSERTs resend rounds through a SECURITY DEFINER
  `assess_enqueue_resend(tid, actor)`. Add these rows to plan 2's pg_catalog grant assertion.
- **Generation handler** (idempotent per `session_id`; the UNIQUE constraint makes a second run a
  no-op). Reads snapshot + turns with the KEK copy; recomputes every turn hash and compares with
  the hash persisted at turn time (REQ-102); any mismatch → no transcript, job FAILED with
  `integrity_mismatch`, audit + alert (never deliver a transcript that does not match its
  turns). Renders, runs the allowlist check, encrypts, then in ONE transaction inserts the
  transcript row, one PENDING delivery per configured recipient (≤ 3), the
  `transcript_generated` audit event, and NOTIFY (empty payload).
- **Template.** Jinja2 `Environment(autoescape=True, undefined=StrictUndefined)`; no `|safe`, no
  `Markup`, no macros taking markup; a test greps the template for `safe`/`Markup`. Header fields
  (REQ-074) come only from session/snapshot rows, never from LLM output: course, activity,
  student identifier, "Sessione #N di M" with earlier sessions, "tentativo #k di M" + method,
  grade at start, grade at close only if released (else the neutral notice), cap + extra (no
  reason), timestamps, close reason (6-value vocabulary), outage intervals + credited total,
  failed start attempts, the BR-009 statement, config flags, per-file status list, conversation
  language(s). Body: `<ol>` of turns with role label, timestamp, `lang`, markers
  "[risposta sostituita dal sistema]", post-check replacement marker, "segnalato dallo studente".
  "PUNTI CONTESTATI" section labelled "Riepilogo generato dall'assistente", rendered as escaped
  text from plan 8's Part B output. Footer: SHA-256 turns digest.
- **Post-render allowlist check** (`allowlist.py`, stdlib `html.parser`): every start tag and
  attribute must be in the template's own set; exactly one `<style>` in `<head>` whose content
  hash equals the template constant; no attribute value may contain a URL scheme. Explicit
  rejection tests for `script`, `on*`, `style=`, `@import`, `data:`/`javascript:` URIs, `srcset`,
  `base`, `meta http-equiv=refresh`, `form`/`action`, `iframe`/`object`/`embed`,
  `svg`/`foreignObject`, `annotation-xml`. A failure aborts generation (fail closed). A test-only
  differential run with `html5lib` (dev dependency) catches parser-differential gaps.
- **Math.** TeX segments are converted with `latex2mathml` (pure Python, must pass the BUG-027
  baseline) and the result is re-parsed and filtered against a presentation-only MathML
  allowlist (`math mrow mi mn mo ms mtext mspace mfrac msqrt mroot msub msup msubsup munder mover munderover mtable mtr mtd mstyle`;
  attributes `display mathvariant stretchy fence separator accent columnalign`); no `href`,
  `xlink:*`, `maction`, `semantics`, `annotation(-xml)`. Any conversion or filter failure →
  escaped TeX in `<code>` with `aria-label`. If the dependency fails BUG-027, ship escaped TeX
  only and record it.
- **Naming (BR-016).** `<course-shortname>_<activity-id>_<user-id>_s<N>_<yyyymmdd>.html`;
  shortname folded to `[A-Za-z0-9-]` (others → `-`), date of CLOSED in the site timezone
  (default `Europe/Rome`); never the full name (OQ-020). `Content-Disposition` uses both
  `filename=` (ASCII) and `filename*=UTF-8''`.
- **Email.** `email.message.EmailMessage` with `policy=SMTP`; plain-text body (no transcript
  content) + the transcript bytes as attachment `text/html; charset=utf-8`; subject carries
  course shortname, activity and the student identifier (REQ-075; full name pending OQ-020).
  CR/LF and other C0 controls stripped from every header-bound value before assignment.
  Deterministic `Message-ID: <sha256(tid|round|recipient_userid)[:32]@VEKTRA_ASSESS_MESSAGE_ID_DOMAIN>`.
- **SMTP** (`aiosmtplib`, worker only): `start_tls=True` (or implicit TLS on 465 by setting),
  `validate_certs=True`, AUTH only after TLS; a server without STARTTLS → TLS failure class, never
  plaintext. Concurrency capped by a semaphore `VEKTRA_ASSESS_SMTP_MAX_CONNECTIONS` (default 2) in
  the delivery pool, hard timeout per send (30 s).
- **Exactly-once reading (OQ-029 design default).** Before connecting, the handler commits
  `attempt_started_at`; after the relay's final 250 it commits SENT. On reconciliation a row with
  `attempt_started_at` older than the lease and no outcome is resent with the SAME Message-ID
  and an audited `delivery_possible_duplicate` event. Result: exactly one SENT row per recipient;
  at most one known, audited duplicate after a crash in the window.
- **Backoff.** `VEKTRA_ASSESS_SMTP_RETRY_SCHEDULE=1m,5m,15m,1h` then hourly until
  `VEKTRA_ASSESS_SMTP_RETRY_WINDOW_HOURS=72` → FAILED. 4xx and connection/TLS errors → RETRYING;
  synchronous 5xx recipient rejection → FAILED immediately (shown as bounce in REQ-076). TLS
  failures and every FAILED raise an alert (ERROR log event `assess.delivery.alert` with class
  only + status row read by plan 12's metrics). Asynchronous DSN bounces are out of scope for v1.
- **Recipients (REQ-082).** v1 ships Moodle-resolved graders only; free-text overrides are not
  built (allowed by REQ-082 when they slip). At send time the worker re-resolves the address and
  checks the recipient still holds the grading capability; if not, the row becomes REPLACED and
  rows for current course graders not already recipients are inserted (total ≤ 3), audited.
- **Download path (API).** `POST /api/v1/assess/transcripts/{tid}/download-token`: key with
  `assess:mint` (owner: body `sub`, must equal the transcript's subject) or `assess:teacher`
  (grader: actor re-verified as grader of that cmid via fresh worker-written gate_inputs, fail
  closed). Token via `vektra_shared/tokens.py`: `aud=transcript`, `jti`, `sub`, `tid`, `site`,
  TTL ≤ 120 s; jti row inserted at mint. `GET /api/v1/assess/transcripts/{tid}`: bearer download
  token only; atomic `UPDATE … SET consumed_at=now() WHERE jti=… AND consumed_at IS NULL AND expires_at>now() AND transcript_id=… RETURNING`;
  re-checks ownership/grader on the request; decrypts, verifies `content_sha256`, serves with
  `Content-Disposition: attachment`, `Content-Type: text/html; charset=utf-8`,
  `Content-Security-Policy: sandbox; default-src 'none'`, `X-Content-Type-Options: nosniff`,
  `Cache-Control: no-store`, `Referrer-Policy: no-referrer`. Audit `student_download` /
  `teacher_download`; mismatches audit `ownership_mismatch`. The 410 "expired" branch is added
  by plan 12 (purge tombstones).
- **Resend.** `POST /api/v1/assess/transcripts/{tid}/resend` (grader via `assess:teacher`, or
  operator via admin key; neither can name a recipient): request model `extra="forbid"` and an
  explicit check of query params; any recipient-like field → 400 `ERR_ASSESS_RESEND_RECIPIENT_FORBIDDEN`
  (explicit envelope, not the 422 path of DEBT-035). Creates round n+1 rows for re-resolved
  recipients.

## Tasks

### Transcript
- [x] Migration (next revision): `assess_transcripts`, `assess_deliveries`, `assess_transcript_download_tokens`, enums, indexes `(status, not_before)`; add grants + SECURITY DEFINER `assess_enqueue_resend`; extend plan 2's grant-matrix CI assertion
- [x] `transcript/hashing.py`: canonical turn serialisation shared with plan 8's turn-time hash (single function, imported by both), `turns_digest`
- [x] `transcript/templates/transcript.html.j2` + `render.py`: header/body/footer per Design Notes with semantic headings, lists, `lang`, a single static `<style>`; `StrictUndefined`; template grep test for `safe|Markup`
- [x] `transcript/allowlist.py`: post-render validator + rejection test corpus (one test per listed vector) + html5lib differential test
- [x] `transcript/mathml.py`: latex2mathml + presentation allowlist + escaped-TeX fallback; add dependency to `vektra-assess/pyproject.toml` and the BUG-027 module list (drop and record if it fails)
- [x] `transcript/naming.py`: BR-016 file name + RFC 6266 header builder; unit tests for unicode/space/slash shortnames
- [x] `transcript/store.py`: per-transcript DEK generate/wrap/unwrap with the transcript key (kid), encrypt/decrypt, sha256 verification
- [x] `transcript/handler.py` (lives in `worker/transcript_gen.py`, see Notes): `generate_transcript(session_id)` job handler (idempotent, turn-hash re-verification, allowlist, one-transaction insert of transcript + deliveries + audit + NOTIFY); register with plan 3's JobRunner under the generation pool
- [x] (added) Persist what the transcript must mark but no row held: `assess_turn_notes` (revision 0034) written by the API TurnDriver in the bot turn's own transaction (post-check mark codes + contest entries label/status + reply language); the transcript renders the markers and "Punti contestati" from it
- [x] (added) Recipient selection `delivery/recipients.py` (one function, used by generation and by `/state`): ≤ 3 current course graders; render worker writes `graders` (user ids) and `recipients` (`{role, name}`) into the student's `assess_gate_inputs`

### Delivery
- [x] `delivery/message.py`: EmailMessage builder, header sanitiser (CR/LF/C0 strip), deterministic Message-ID, plain-text body template (Italian, content-free) (`test_delivery_message.py`: CR/LF/C0/C1/U+2028 injection in the course -> one Subject, no Bcc; Message-ID over `tid|round|recipient_ref`)
- [x] `delivery/smtp.py`: aiosmtplib client with mandatory TLS, AUTH after TLS, cert validation, semaphore cap, timeout, error classification (`tls`, `connect`, `temp_4xx`, `perm_5xx`, `auth`) (`test_delivery_smtp.py` on the sink: no STARTTLS / untrusted CA -> `tls` with 0 messages and 0 AUTH; every AUTH seen over TLS; cap 2 reached and never passed; silent relay times out)
- [x] `delivery/backoff.py`: schedule parser + `next_not_before(attempts, first_attempt_at)`; window expiry → FAILED (`test_delivery_backoff.py`: 1/6/21/81 min, then hourly, last attempt exactly at 72 h, then None)
- [ ] `delivery/handler.py`: claim due rows (`status IN (PENDING, RETRYING) AND not_before<=now()`), re-read the configured recipients from the current sealed config + domain allowlist, REPLACED fan-out (see "Decision REVISED"), attempt-start commit, send, outcome commit, audit per attempt and result (REQ-085), alerts; reconciliation of in-window crashes per OQ-029 default
- [ ] `delivery/probe.py`: EHLO + STARTTLS + AUTH + QUIT, never sends; result written to a worker status row for `/health/assess` (plan 12 reads it)
- [ ] Settings in the worker settings class: `VEKTRA_ASSESS_SMTP_HOST|PORT|TLS_MODE|FROM|USERNAME_FILE|PASSWORD_FILE|MAX_CONNECTIONS|RETRY_SCHEDULE|RETRY_WINDOW_HOURS`, `VEKTRA_ASSESS_MESSAGE_ID_DOMAIN`; API startup refuses if SMTP credential files are present (ARCH-071 matrix)

### API endpoints
- [x] `vektra_assess/api/transcripts.py`: download-token mint (owner via `assess:mint`, grader via `assess:teacher` with `require_exact_scope` + site binding + fresh grader check helper `authz/graders.py` reused by plan 11)
- [x] `GET /transcripts/{tid}`: single-use consumption, ownership re-check, decrypt + hash verify, headers, audit; per-token rate limit using plan 1's generalised `RateLimiter`
- [x] `POST /transcripts/{tid}/resend` with recipient-parameter rejection and audit
- [x] (added) `/state` gains `transcripts: [{tid, n, available, purged}]` (n = session number, `purged` false until plan 12, absent when none) and `pre_session.recipients: [{role, name}]` from the worker-written gate_inputs; documented in `docs/reference/api.md` next to the other `/state` fields; never an email address
- [x] (added) Plan 9 UI download wired end to end: postMessage token request → `GET /transcripts/{tid}` with the bearer header, file name from `Content-Disposition`; UI unit tests and the Playwright e2e download scenario
- [x] Add `ERR_ASSESS_TRANSCRIPT_NOT_FOUND` (ERR-ASSESS-017), `ERR_ASSESS_DOWNLOAD_TOKEN_INVALID` (018), `ERR_ASSESS_RESEND_RECIPIENT_FORBIDDEN` (019), `ERR_ASSESS_TRANSCRIPT_INTEGRITY` (020) and the teacher-unverified 021 (plan 11 reuses it) to `vektra_shared/errors.py` with `_CODE_STATUS_OVERRIDE` entries and `docs/reference/error-codes.md`

### Tests, CI, docs
- [x] Mail-sink fixture `vektra-assess/tests/fixtures/mailsink.py` (aiosmtpd, dev dependency): STARTTLS with a self-signed CA trusted by the test client, toggles for down / 4xx / 5xx / no-STARTTLS / drop-after-DATA, message store queried by Message-ID (root dev group `aiosmtpd>=1.4`; per-sink CA, `require_starttls` + `auth_require_tls`; the false-pass guard is a real STARTTLS handshake in `start()`)
- [ ] Integration tests (testcontainers pg16, `@pytest.mark.integration`): generation idempotence (two concurrent jobs → 1 row); tampered turn → no transcript + alert; XSS / markdown-image / remote-URL / header-like turn payloads → allowlist passes, Playwright opens the file with network logging and records 0 requests and 0 script executions; copies byte-identical (download bytes == attachment bytes) — 10a done: `test_transcript_generation.py` (idempotence 4 concurrent jobs -> 1 row, tampered turn, count == CLOSED), `test_transcript_inert_browser.py`; remaining: download bytes == attachment bytes (10b)
- [ ] Delivery tests with injected clock: SMTP down for a simulated 2 h then up → each recipient exactly one message on the sink; no-STARTTLS sink → 0 messages, RETRYING + TLS alert; crash between 250 and SENT commit → one SENT row, ≤ 1 duplicate on the sink with identical Message-ID, audit event present; 72 h expiry → FAILED + alert; header injection payload in course shortname → single header on the wire
- [x] API tests: student B's token for A's transcript → 403 + audit; replayed token → 401; expired token (clock) → 401; non-grader teacher → 403; admin key on download-token mint → 403 (non-inheriting); resend with `recipient` in body or query → 400; response headers asserted (`tests/integration/test_transcript_api.py`)
- [ ] NFR-021 benchmark `tests/nfr/test_assess_transcript_timing.py`: 50 sessions closed concurrently on the compose stack with the mail sink; measure `generated_at - closed_at` and `sent_at - closed_at`
- [x] Accessibility: axe-core scan of a rendered transcript (quiz + assignment fixtures with math) and HTML validation (`html5validator` or `vnu` container) for UTF-8 and `lang` (`tests/integration/test_transcript_a11y.py`; manual NVDA read-through BLOCKED, see Notes)
- [x] CI: add the new modules to the BUG-027 import list (worker side), `aiosmtplib`/`latex2mathml` declared explicitly in `vektra-assess/pyproject.toml`; mypy path list; `test-assess` job picks the new tests up (10a: transcript modules in the qemu list; `aiosmtplib` was already declared; mypy covers the whole package; the browser checks run in the assess-ui-e2e job with `VEKTRA_ASSESS_E2E_REQUIRED=1`, exempted from the package job's skip guard; 10b adds `delivery/*` to the qemu list)
- [x] Docs (10a): `docs/reference/api.md` transcript endpoints and `/state` fields, `docs/reference/error-codes.md` 017-021, `configuration.md` + `.env.example` transcript settings, `CHANGELOG.md` [Unreleased] Added
- [ ] Docs (10b): `docs/reference/configuration.md` + `.env.example` SMTP settings; CHANGELOG for delivery

## State & Data Lifecycle

| State element | Created by | Updated by | Invalidated/Deleted by |
|---------------|-----------|------------|------------------------|
| `assess_transcripts` row + per-transcript DEK (`dek_wrapped`) | `transcript/handler.py` in the generation transaction | never (immutable; copies identical by construction) | plan 12 purge/erasure: delete `dek_wrapped` then row, verified by count |
| `assess_deliveries` rows | generation transaction (round 1); resend (round n+1); REPLACED fan-out; plan 11 `reset_notice` | `delivery/handler.py` on every attempt (status, attempts, not_before, attempt_started_at) | plan 12 purge (RETRYING → ABANDONED, audited, then deleted with the transcript) |
| `assess_transcript_download_tokens` | download-token mint | `GET /transcripts/{tid}` sets `consumed_at` | expired/consumed rows deleted by a cleanup tick (daily, plan 3 tick framework); purge with the transcript |
| SMTP probe status row | `delivery/probe.py` tick | each probe run | overwritten; stale age reported by `/health/assess` |
| In-process SMTP semaphore | worker start | — | worker stop |

## Acceptance Criteria

- [ ] Transcript row count == CLOSED session count after the chaos/integration suite; download bytes and email attachment bytes hash-equal (REQ-074)
- [x] Canary strings for system prompt, solution, pasted rubric and reset reason → 0 occurrences in decrypted transcript bytes; unreleased regrade → 0 occurrences of the new grade (REQ-074) — evidence: `test_transcript_render.py::test_snapshot_secrets_never_reach_the_transcript` (solution, pasted rubric), `test_transcript_generation.py::test_a_closed_session_gets_one_verified_transcript_and_recipient_rows` (system prompt and context framing canaries, 0), `test_transcript_render.py::test_an_unreleased_regrade_never_prints_the_new_grade`; reset reasons do not exist until plan 11 and the renderer reads no such column
- [x] Every safeguard/post-check replacement and every student flag is marked; header lists every file with exactly one status and the language(s) (REQ-074) — evidence: `test_transcript_render.py::test_header_fields_come_from_rows_and_markers_are_shown`; marks persisted by the TurnDriver: `test_session_turns.py::test_turn_notes_are_written_with_the_bot_turn`
- [x] Re-hashing every turn at generation matches 100%; a tampered turn yields 0 transcripts and an alert (REQ-102) — evidence: `test_transcript_generation.py::test_a_tampered_turn_yields_no_transcript_and_an_alert` (re-encrypted turn 2 with the session DEK: 0 rows, audit with turn_no 2, dead job), every generated transcript re-hashes in `_open`
- [x] Transcripts with XSS, markdown-image and remote-URL payloads execute 0 scripts and make 0 network requests when opened; served responses carry `Content-Disposition: attachment` and `CSP: sandbox`; file name matches the BR-016 pattern with 0 full-name occurrences (BR-016) — evidence: `test_transcript_inert_browser.py` (7 payload files opened in Chromium: 0 requests, 0 scripts, 0 executed payloads; false-pass guard on the file count), `test_transcript_api.py::test_the_owner_downloads_the_exact_bytes_once` (headers), `test_transcript_render.py` naming tests + `test_transcript_generation.py` (pattern, 0 full names)
- [ ] SMTP down 2 h then restored → every pending transcript delivered exactly once per recipient on the mail sink; no row is SENT without a sink-recorded message; crash-window duplicate ≤ 1 and audited (REQ-075, OQ-029)
- [ ] An address removed from the activity config is REPLACED by the current configured ones (≤ 3), audited old/new; an address outside `VEKTRA_ASSESS_RECIPIENT_DOMAINS` (or any, with the list empty) gets 0 messages, FAILED + alert; a 4th recipient never exists (REQ-082 as amended 2026-11-07)
- [x] Student B requesting A's transcript gets 403 and an audit event; the student copy is hash-equal to the teacher copy; a download token works exactly once and expires ≤ 120 s (REQ-088, ARCH-067) — evidence: `test_transcript_api.py` (`test_student_b_cannot_get_a_token_for_a_transcript`, `test_a_grader_downloads_the_same_bytes_and_a_non_grader_is_refused`, `test_the_owner_downloads_the_exact_bytes_once`: second use 401, `test_expired_foreign_and_query_tokens_are_refused`), TTL <= 120 by the setting bound and the token check
- [x] Resend carrying a recipient parameter (body or query) is rejected with 400 (REQ-080 via REQ-082) — evidence: `test_transcript_api.py::test_resend_never_takes_a_recipient` (body keys at any depth and query names, 0 rows written)
- [ ] Generation p99 ≤ 60 s after CLOSED and SENT p95 ≤ 5 min with SMTP healthy; ≥ 99% SENT or downloadable within 15 min (NFR-021, REQ-075)
- [x] Transcript passes the automated accessibility check and validates as UTF-8 HTML with correct `lang` (NFR-016, BR-016) — evidence: `test_transcript_a11y.py` (0 axe violations, 0 Nu errors on quiz and assignment transcripts with math; a broken control proves the checker ran); manual NVDA read-through BLOCKED (Notes)

## Testing Approach

Unit tests cover rendering, allowlist, naming, header sanitising and backoff as pure functions.
Integration tests run against a real Postgres (testcontainers) and an in-process TLS mail sink
whose stored messages are the only accepted evidence of delivery: every "SENT" assertion is
paired with a sink lookup by Message-ID, every "generated" assertion with a decrypt + re-hash.
Time-dependent behaviour (2 h outage, 72 h window, token TTL) uses an injected clock; one short
real-time run guards against clock-injection bugs.

### Test Infrastructure

- **Required**: testcontainers Postgres pg16; aiosmtpd mail sink with STARTTLS and fault toggles; Playwright Chromium for the "0 requests / 0 scripts" check; vnu/html5validator; compose stack with assess profile for NFR-021
- **Provided by**: this plan (mail sink, fault toggles, timing benchmark); Postgres fixtures and roles from 20261101-154950-assess-data-crypto; closed-session fixtures from 20261101-154950-assess-session-lifecycle; graders-reader fake from 20261101-154950-assess-moodle-ws
- **False-pass guard**: delivery tests fail if the mail sink did not start (fixture asserts a listening socket and a successful STARTTLS handshake before yielding); the Playwright inertness test fails if fewer than the declared number of payload files were opened; the NFR benchmark fails when fewer than 50 sessions reached CLOSED rather than reporting percentiles over a smaller set

### NFR Verification

| NFR | Dataset/Load | Tool | Environment | Pass criterion |
|-----|--------------|------|-------------|----------------|
| NFR-021 | 50 sessions closed concurrently, 2 grader recipients each, healthy mail sink | `tests/nfr/test_assess_transcript_timing.py` reading `closed_at`, `generated_at`, `sent_at` from DB + sink timestamps | compose stack (CI integration) and target VM (plan 13) | p99(generated − closed) ≤ 60 s; p95(sent − closed) ≤ 5 min |
| NFR-016 | rendered quiz and assignment transcripts with math, markers, Punti contestati | axe-core (Playwright) + vnu + manual screen-reader read-through | CI + manual | 0 WCAG 2.1 AA violations; valid UTF-8 HTML with `lang`; reads end-to-end with NVDA |

## Integration Notes

- Plan 8's close path must enqueue `generate_transcript` inside the CLOSED transaction and expose
  Part B "Punti contestati" output in a typed field; the canonical turn-hash function lives in
  `transcript/hashing.py` and plan 8 imports it (or vice versa) — one implementation only.
- Plan 11 reuses `authz/graders.py`, the delivery outbox with `kind=reset_notice`, and reads
  delivery status for the REQ-076 list. Plan 12 adds purge tombstones (410 branch), ABANDONED
  handling, metrics over delivery status, and the operator test-send.
- Plugin P2/P3 mint download tokens server-side and never cache them; the iframe (plan 9) fetches
  with the bearer header.

## Notes

- OQ-029 (exactly-once vs crash window): the design default is implemented; product/QA
  confirmation is still needed for the REQ-075 criterion. Blocker for pilot sign-off, not for
  implementation.
- OQ-020: subject line and file name use identifiers only until the DPO rules on full names;
  REQ-075 "student in the subject" is satisfied by the identifier.
- Free-text recipients (REQ-082 SHOULD) are not built; add a BACKLOG item (domain allowlist +
  audit) for a later release.
- Risk: `latex2mathml` and `aiosmtplib` are new dependencies; both must pass the BUG-027 qemu
  baseline in week 1 (ARCH-078). `aiosmtpd` is dev-only.
- Drift: the existing `_pii_redactor` only redacts at WARNING+ by key name; delivery code must
  never log addresses or bodies at any level (plan 12 adds the assess log allowlist processor).

> Note (independent review of W1, PR 146, 2026-11-03): the review-HTML sanitiser output (`SanitizeResult.text`, type `PlainText`, `vektra_assess.parser_protocol`) is TEXT, not markup, and is NOT escaped: it can contain literal `<img onerror=...>` (decoded entities) or `<math>...</math>` source. **Every consumer of `PlainText` in this plan must HTML-escape it at the render site** (`parser_protocol.html_escape`, or a contextual escaper) **and must have a test that proves it** (feed `&lt;img src=x onerror=alert(1)&gt;`, assert nothing in the rendered output parses as an element). The same holds for file text returned by `parse_file` (`ParseResult.text`) when it is rendered.

> **State (2026-11-07): 10a merged (PR #159, 0e38f49). 10b in progress on `feat/assess-transcript-delivery-smtp`; the recipient decision is "Decision REVISED" below.**
> 10a is done and green: schema (rev 0034), generation with turn-hash re-verification, inert render
> + allowlist + MathML, sealed store, download tokens + single-use download + resend, `/state`
> transcripts and recipients, UI download end to end, a11y checks, docs. 10b (delivery: `message`,
> `smtp`, `backoff`, delivery handler, probe, SMTP settings, mail sink, delivery tests, NFR-021
> benchmark, SMTP docs) is NOT started: see "Decision needed" below. The PR for 10a can be opened
> as is; 10b goes on its own branch/PR after the decision.

### Decision needed before 10b (stop rule: changes REQ-079 or an integration contract)

- **Where the grader's email address comes from.** Plan 4 found (finding 2, recorded fixtures) that
  `core_enrol_get_enrolled_users_with_capability` returns NO email to the service account: it lacks
  `moodle/user:viewalldetails`. REQ-082 requires "the address resolved from Moodle, re-resolved at send
  time"; ARCH/plan design says addresses are never stored. Options: (a) add a capability that exposes
  the email (`moodle/user:viewalldetails`, or `moodle/course:useremail` in the course context, to be
  verified on 4.3/5.2) to the service-account role, i.e. amend REQ-079's spike-verified minimal role and
  add a setup-check item; (b) the plugin resolves addresses at send time through a new server-to-server
  call (new INT contract, plugin change); (c) teacher-entered addresses (REQ-082 free text: needs the
  site-admin domain allowlist and change audit, which v1 does not build). Everything in 10b except the
  production address resolver can be built against a resolver interface, but binding the worker's
  `transcript_deliver` handler needs the choice. 10a already writes the recipient USER IDS
  (PENDING rows, round 1) and the `transcript_deliver` job, so nothing is lost while this is open.
- **Decided (user, 2026-11-07): option (a).** The service-account role gains the narrowest capability
  that exposes grader emails (verify `moodle/course:useremail` vs `moodle/user:viewalldetails` live on
  4.3 and 5.2, pick the narrowest that works). 10b amends REQ-079 (minimal role) in
  `.s2s/requirements.md`, adds a setup-check item for it, updates
  `docs/guides/assess-moodle-service-account.md`, re-records the graders fixtures with emails (scrubbed
  to the dev seed's fake addresses), and asks vektra-moodle-cf to add the capability to the dev role
  provisioning in `vektra-moodle/docker`.
- **Verified live (vektra-moodle-cf, 2026-11-07, vektra-moodle#44 `06a16bd`, 4.3 and 5.2, maildisplay 0
  and 2):** `moodle/course:useremail` makes `core_enrol_get_enrolled_users_with_capability` return the
  email for 2/2 graders; `core_user_get_users_by_field` still returns 0/2 (course-scoped check only);
  `moodle/user:viewalldetails` gives 0/2 on both; `moodle/site:viewuseridentity` gives 2/2 on both but
  exposes every identity field, so it is broader. 10b uses `moodle/course:useremail` and resolves the
  address through `core_enrol_get_enrolled_users_with_capability` only.
- **10b must also (agreed with vektra-moodle-cf, 2026-11-07):** with `moodle/course:useremail` Moodle
  returns `email` for EVERY enrolled user, in both `core_enrol_get_enrolled_users_with_capability` and
  `core_enrol_get_enrolled_users` (4.3 and 5.2). The strict (`extra="forbid"`) enrol models in
  `moodle/models/v4_3|v5_2/enrol.py` would then fail every enrolment read with `unknown_shape`. 10b
  (a) adds `email: str | None` to those models and (b) passes `userfields` on every enrol call, asking
  only the fields read, with `email` only on the graders call. vektra-moodle#44 (the dev-role change) stays
  draft until the 10b PR is merged; dev 43/52 run without the capability until 10b's live test, when
  vektra-moodle-cf re-provisions them from the #44 branch.
- **Open for the user with 10b (REQ-079 scope):** the capability lets the service account READ every
  enrolled user's hidden email, not only graders'; `userfields` limits what is returned, not what the
  account may read. Options: accept and document it in the REQ-079 amendment, or assign the service
  role per course instead of at system level.

### Decision REVISED (user, 2026-11-07 night): recipients come from the teacher's activity config

Supersedes option (a) above and the "10b must also" item: no `moodle/course:useremail`, no `email`
in the enrol models, no re-recorded graders fixtures. vektra-moodle#44 closed unmerged (dev 43/52
run develop's provisioning). REQ-082 amended in `.s2s/requirements.md` (free-text path is v1).

10b design (branch `feat/assess-transcript-delivery-smtp`):
- **Field.** Sealed teacher config JSON gains `"transcript_recipients": ["a@x.it", ...]`
  (1..3 strings, trimmed, lowercased domain, RFC 5321 addr-spec subset: one `@`, no whitespace, no
  control chars, no display name, total <= 254 bytes; duplicates collapse). Written by plan 11's
  config PUT (plugin P3 form, prefilled with the teacher's own Moodle address); 10b only reads it
  (`delivery/recipients.py: configured_recipients(sealed_config) -> tuple[str, ...]`).
- **Allowlist.** `VEKTRA_ASSESS_RECIPIENT_DOMAINS` (comma list, exact domain match, worker
  setting; empty = every address refused). Checked by the worker at send time (FAILED,
  `last_error_class=domain_not_allowed`, audit + alert) and by plan 11's PUT (field error).
- **Rows.** `assess_deliveries` keys a recipient by `recipient_ref = sha256(site_id|address)` hex
  (revision 0035: add `recipient_ref`, make `recipient_userid` NULL-able, unique
  `(transcript_id, round, recipient_ref)`, `recipient_kind` gains `address`). The address itself
  is never stored outside the sealed config; logs and audit carry the ref only.
- **Send time.** Re-open the current sealed config: a row whose ref is no longer configured ->
  REPLACED (audited old/new refs), configured refs with no row in the round -> new PENDING rows,
  never more than 3 live per round. Config unreadable or no recipients -> rows stay, RETRYING with
  `recipients_unresolved`, alert; the transcript stays downloadable.
- **Generation** (10a code) switches from graders to `configured_recipients`; a transcript with
  none still generates (audit `assess_transcript_recipients_unresolved` as today).
- **/state** `pre_session.recipients`: `[{role: "recipient", name: "m***@unical.it"}]` (first char
  of the local part, then `***`), written by the render worker into gate_inputs as today. The API
  never sees a full address.
- **Grader download/resend authz unchanged** (fresh grader list from Moodle user ids).
- **Plugin request (P3, sent to vektra-moodle-cf):** form field "Destinatari della trascrizione"
  1..3 emails, prefilled with `$USER->email`, validated against the site-admin copy of the domain
  allowlist (error names the allowed domains), sent in the sealed teacher config as
  `transcript_recipients`; the pre-session screen names them from the config.

### Design decisions taken in 10a (one line each, with the reason)

- Transcript id = session id (1:1 by the UNIQUE `session_id`): the discussion UI knows only its
  session, so its `download-token-request {tid}` names the transcript without another lookup; the
  plugin gets the same ids from `/state.transcripts`.
- The job body lives in `worker/transcript_gen.py`, not `transcript/handler.py`: import-linter forbids
  `vektra_assess.transcript` to import `context.capture` (snapshot integrity), `llm` (language
  detector, shipped strings) and `moodle`; `transcript/` holds the pure parts (render, allowlist,
  mathml, naming, store, hashing, facts protocol). The shipped BR-009 and "[risposta sostituita dal
  sistema]" strings are inlined in `render.py` and pinned equal to the YAML by a test.
- The fresh grader check and the download tokens are `api/graders.py` and `api/download_tokens.py`
  (not `authz/`): a new top-level child would change the designed layout and three import contracts.
  Plan 11 reuses `vektra_assess.api.graders.check_fresh_grader`.
- Grader facts for the API: the render check (worker) reads the course's graders and writes
  `graders` (user ids) and `recipients` (`{role: "grader", name}`) into the student's
  `assess_gate_inputs`; the API accepts them only when at most `VEKTRA_ASSESS_TEACHER_GATE_MAX_AGE_S`
  (120 s) old, otherwise 403 `ERR-ASSESS-021` and a render check is queued (fail closed). Source chosen
  over the snapshot: the snapshot is the model's frozen allowlist, graders change over time.
- Recipients until plan 11 stores the teacher's choice: at most 3 current course graders, ascending
  user id, the student excluded (`delivery/recipients.py`, one function for generation and `/state`).
  REQ-082's "default: the configuring teacher" belongs to plan 11's config.
- Plan 8 did not persist post-check marks, contest entries or the reply language: revision 0034 adds
  `assess_turn_notes` (API INSERT in the bot turn's own transaction, worker SELECT/DELETE); the turn
  row is never updated (BR-015). Student-turn language: the runner's detector at generation.
- Integrity failures (snapshot, turn hash, decrypt) and allowlist violations raise the new
  `worker.runtime.PermanentJobError`: the job goes dead at once with `integrity_mismatch` (audited
  first), never retried; plus an `assess_transcript_integrity_failed` audit row and an ERROR
  `assess.transcript.alert` log line.
- Graders unreadable at generation: the transcript is still generated (downloadable, NFR-021) with no
  delivery row, audited `assess_transcript_recipients_unresolved` + alert. 10b's delivery tick must fan
  out recipients for a transcript that has none.
- Wake-up of delivery: a deduped `transcript_deliver` job (`transcript_deliver:<tid>:<round>`) in the
  generation and resend transactions; its handler is 10b's.
- Download token: `aud=transcript`, signed with the existing `assess_token_signing` keyring (no new
  secret), TTL `VEKTRA_ASSESS_DOWNLOAD_TOKEN_TTL_S` default 60 s (max 120), jti stored as SHA-256.
  A student asking for another's transcript: 403 `ERR-AUTH-003` + `assess_ownership_mismatch_rejected`.
- The resend refuses ANY query parameter (a recipient-like name -> `ERR-ASSESS-019`); the static
  credential test allows exactly that one function (`_refuse_query`, names only, an AST check).
- Header without Moodle names: course SHORT name (from `core_enrol_get_users_courses` at close) and
  "Quiz/Compito (id <cmid>)"; the student by Moodle id (OQ-020). Grade at close: release-gated reader
  (assignment: grade item visible and workflow released; quiz: item visible); an unreleased regrade
  prints only the neutral notice.
- "Punti contestati": latest entry per label wins (a correction replaces), with the turn number and,
  for assignment criteria only, the criterion remark (a quiz review text may carry the right answer).
- MathML: `latex2mathml` 3.81 (pure Python: nothing native for BUG-027; the qemu import list now
  includes it); output re-parsed into an allowlisted node tree; TeX fallback is escaped text in
  `<code>` with a constant `aria-label` (user text never reaches an attribute, so a student cannot
  make the allowlist fail their own transcript).
- `/state.transcripts[].purged` is the constant `false` (review #9): `shred_transcript` deletes the row, so no trace survives a shred; plan 12 (privacy-retention) must record a tombstone and set `purged` from it (request recorded for plan 12). A test pins today's contract (a shredded transcript reads `available: false, purged: false`).
- Revision 0034 re-creates `assess_deliveries` (nothing ever wrote the plan-2 draft) and adds the
  live-transcript CHECK as `NOT VALID` (exempts rows of no generator).

### Plugin requests (vektra-moodle-discuss, owned by the other session)

- P2 (student): on `vektra-assess-download-token-request {tid}` from the iframe, mint server-side with
  the site's mint key `POST /api/v1/assess/transcripts/{tid}/download-token {"sub": <userid>}` and post
  back `{v: 1, type: "vektra-assess-download-token", tid, token}`; never cache a token. The results
  page lists `/state.transcripts` (`n`, `available`, `purged`) and mints per click the same way; a 404
  `ERR-ASSESS-017` right after the close means "not generated yet, retry".
- P3 (teacher): same endpoint with the `assess:teacher` key and `{"actor_userid"}`; on 403
  `ERR-ASSESS-021` retry after a few seconds (a fresh grader check was queued). Resend:
  `POST .../resend {"actor_userid"}`, never with a recipient.

### Acceptance and test notes

- BLOCKED: manual NVDA read-through (NFR-016) needs a person with a screen reader; the automated half
  (axe WCAG 2.1 AA + Nu checker, with a broken control document) passes.
- BLOCKED until 10b: NFR-021 benchmark, SMTP criteria (REQ-075, OQ-029), recipient replacement
  (REQ-082 send-time half), download bytes == email attachment bytes (the download half is proven).

### Code review of 10a (2026-11-07), decisions

- Resend limits (review #6): at most one delivery round per transcript per
  `VEKTRA_ASSESS_RESEND_MIN_INTERVAL_S` (default 600 s, generation's round 1 included), checked
  inside `assess_enqueue_resend` under the per-transcript advisory lock (so concurrent calls and
  several API replicas cannot both pass; the function returns -1, the API answers 429
  `resend_interval` with `Retry-After`), plus `VEKTRA_ASSESS_RESEND_RPM` (6) per API key in an
  assess-local limiter. Reason: each round mails up to 3 graders; a DB check is the only one that
  holds across replicas.
- Grader re-check at download (review #7): the jti is locked first but CONSUMED only after the
  ownership / grader check passes, so a refusal a retry can cure (a grader list gone stale between
  mint and GET; the 403 queued a refresh) leaves the token usable within its TTL; and at GET the
  list is accepted up to `TEACHER_GATE_MAX_AGE_S + DOWNLOAD_TOKEN_TTL_S` old, since it was fresh
  (<= max age) when the token was minted. Fail closed is kept: a grader the list no longer names
  is refused. An integrity refusal (stored bytes fail their hash) still consumes the token.

### CodeRabbit round 1 on PR #159 (2026-11-07), decisions

- Activity-context grading permission (4503025975): not checkable by Vektra. The WS layer
  offers only course-context capability listings (`core_enrol_get_enrolled_users_with_capability`);
  a module-level prohibit is invisible to the service account. Vektra's fresh grader list stays a
  defence in depth; **plugin request (P3)**: check `has_capability('mod/assign:grade' |
  'mod/quiz:grade', $modulecontext)` for the acting teacher before minting a grader download token
  or calling resend. Follow-up for 10b/plan 11: `read_graders` asks for `mod/assign:grade` for
  quizzes too; a quiz-specific capability needs a recorded fixture first.
- Recipients unresolved at generation (4503025990): deferred to 10b, where the delivery tick fans
  out recipients for a transcript that has none (already recorded above); until then resend
  answers 404 for such a transcript.
- Durable completion marker across shredding (4503029411): deferred to plan 12, which owns the
  purge tombstone (`assess_purge_records`); generation must check it before regenerating.
- `/state` `tid` (4503029392): by design the transcript id IS the session id (generation inserts
  `id = session_id`), so listing the session id is the stored id.
- Graders after the student left the course (4503025969): the grader side falls back to the
  course hint (session or plugin), accepted only when the cmid is found in that course; the Start
  gates still require the student's own enrolment.
- Session cap frozen at commit: `assess_sessions.cap_s` (revision 0034) feeds the header.
- MathML deeper than 32 levels renders as escaped TeX (the template renders the tree recursively).
