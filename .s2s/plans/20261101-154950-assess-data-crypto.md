# Implementation Plan: Assess data model, DB-enforced invariants and envelope encryption

**ID**: 20261101-154950-assess-data-crypto
**Status**: active
**Branch**: N/A
**Created**: 2026-11-01T14:49:50Z
**Updated**: 2026-11-01T14:49:50Z

## Traceability

**Source**: ARCH-077, ARCH-079, ARCH-071, ARCH-068 (constraints part), INT-002
**Source Type**: architecture

## References

### Requirements
- REQ-067: Server-authoritative lifecycle, at most one non-CLOSED session per (student, activity) @.s2s/requirements.md
- REQ-071: Start pre-flight, commit and rollback (rollback consumes nothing, no content before commit) @.s2s/requirements.md
- REQ-074: Transcript content and integrity (one transcript per closed session, hash) @.s2s/requirements.md
- REQ-075: Durable transcript delivery (unique delivery per transcript and recipient) @.s2s/requirements.md
- REQ-085: Minimum audit event set (content-free, fail-closed audit store) @.s2s/requirements.md
- REQ-089: Retention and purge verified by row count @.s2s/requirements.md
- REQ-093: Student transcript access after loss of course access (transcript key held by the API) @.s2s/requirements.md
- BR-013: Teacher input validation (sealed teacher config) @.s2s/requirements.md
- BR-015: Snapshot contents and integrity (encrypted, hashed, immutable, deleted on rollback) @.s2s/requirements.md
- NFR-014: Attempt isolation (epoch- and state-bound material access) @.s2s/requirements.md
- NFR-018: Discussion latency (crypto and material-fetch overhead budget) @.s2s/requirements.md

### Architecture
- ARCH-077: assess data model and data flow @.s2s/architecture.md
- ARCH-079: envelope encryption with live-session key hand-off @.s2s/architecture.md
- ARCH-071: dedicated encrypted stores, key matrix, write-only teacher config @.s2s/architecture.md
- ARCH-068: DB-enforced lifecycle (partial unique index, lease columns, outbox) @.s2s/architecture.md
- ARCH-070: probed capability matrix (assess_site_capabilities rows) @.s2s/architecture.md
- INT-002: API to worker channel via Postgres, grant matrix asserted in CI @.s2s/architecture.md
- COMP-002 / COMP-003: what the API and worker hold and write @.s2s/architecture.md

### Decisions
- ADR-0029: Postgres-native leased job loop with DB-enforced invariants and outbox @.s2s/decisions/ADR-0029-postgres-native-leased-job-loop-for-assess-with-db.md
- ADR-0031: envelope encryption with live-session key hand-off and crypto-shredding @.s2s/decisions/ADR-0031-envelope-encryption-with-live-session-key-hand-off.md
- ADR-0011: conversation encryption (contrast: pgcrypto, fail-open; not reused) @.s2s/decisions/ADR-0011-conversation-encryption.md
- ADR-0017: audit/analytics separation @.s2s/decisions/ADR-0017-audit-analytics-separation.md

### Dependencies
- 20261101-154950-assess-foundations: package scaffold, roles `vektra_assess_api`/`vektra_assess_worker`,
  `keys.py` keyrings and `KEY_MATRIX`, migration 0009, and the BUG-027 baseline result (which
  selects the crypto path).

## Overview

This plan puts the assess invariants in Postgres and the confidentiality in application-side
cryptography, so that neither one depends on the handler code that later plans write.

1. **Schema (ARCH-077).** All `assess_*` tables are created in the next revisions of the root
   Alembic chain.
2. **DB-enforced lifecycle.**
   - a partial unique index guarantees one non-CLOSED session per (site, student, cmid);
   - CHECKs constrain state and close reason;
   - a deferred constraint makes `consumed == exists(opening turn)` structural;
   - snapshots are immutable after commit.
3. **The ARCH-079 access boundary.**
   - SECURITY DEFINER functions `get_session_material` and `assess_append_turn` (plus
     `assess_close_session`) with a fixed search_path;
   - a composite-FK CHECK and a trigger that make a hand-off row for a non-live session
     unrepresentable;
   - the per-role grant matrix, asserted in CI against `pg_catalog` in both directions.
4. **`vektra_assess.crypto`.**
   - per-session DEKs and KEK wrapping (AES-256-GCM, kid/key_version);
   - X25519 HPKE-style sealing of DEKs to the API, and of teacher config to the worker;
   - per-transcript DEKs;
   - crypto-shredding and rotation helpers, as library functions that later plans wire into jobs.

The design names its own weak point: "the SECURITY DEFINER functions become the access-control
boundary; grant drift or a missed predicate silently restores A". This plan therefore treats the
grant matrix and the function predicates as code under test. Each is asserted against the live
catalog and by connecting as the real role and observing `permission denied`. A green migration
is not taken as evidence.

## Design Notes

**Crypto path.** App-side AES-256-GCM and X25519 from `cryptography`, which is a HARD dependency
*(Decision 2026-11-01, coordinator)*: no pgcrypto path and no weaker crypto path of any kind (ADR-0031 amended).
If the primitives are unavailable, the assess processes refuse to start. Falling back to
"turns in the worker" (option B) is not allowed.

**Envelope format.** Every ciphertext column stores `version(1) | kid_len(1) | kid | nonce(12) |
ct+tag`. The AAD is always `b"vektra-assess/v1/" + purpose + b"/" + row binding`:

| Purpose | Row binding in AAD |
|---|---|
| `snapshot` | session_id |
| `turn` | session_id, turn_no |
| `dek-wrap` | session_id, key_version |
| `handoff` | session_id, handoff kid |
| `transcript` | transcript_id |
| `transcript-dek` | transcript_id |
| `teacher-config` | site_id, cmid, field |

A ciphertext copied to another row or column therefore fails authentication, which closes
row-swap attacks by an actor with DB write access. Nonces are 96-bit random. DEKs come from
`AESGCM.generate_key(256)`.

**Sealing (HPKE-style).** If the locked `cryptography` version ships the RFC 9180 HPKE API, it is
used with mode base, DHKEM(X25519, HKDF-SHA256), HKDF-SHA256 and AES-256-GCM. Otherwise the
equivalent construction is built from primitives: an ephemeral X25519 key, HKDF-SHA256 over
`eph_pub | recipient_pub` with info `"vektra-assess/seal/v1/" + purpose + kid`, then AES-256-GCM
with the AAD above. The two paths have the same wire format, so a later switch is not a data
migration. Two purposes use it:
- `handoff`: the API's X25519 public key, sealing a DEK;
- `teacher-config`: the worker's X25519 public key, sealing the solution or pasted rubric.

The API can therefore write teacher config but never read it (OQ-034 default).

**Hashes.** The snapshot `content_hash` is SHA-256 over the canonical plaintext (sorted-key
JSON, UTF-8). Turns form a chain: `turn_hash = SHA-256(prev_hash | turn_no | role | kind |
plaintext)`, with the genesis hash taken from the snapshot hash. A transcript hash covers the
rendered bytes. Hashes are stored in the clear (they are needed for REQ-102 and BR-015 audit
anchoring). This is a documented trade-off: an equality oracle on short, guessable turns. It is
mitigated by the turn_no/prev_hash salt.

**Core tables (columns abridged; ids are uuid unless noted).**
- `assess_sites` (moodle_site_uuid UNIQUE, base_url, release, min_plugin_version, disabled_at),
  `assess_site_keys` (site_id, api_key_id UNIQUE FK api_keys, purpose ∈ mint|teacher|privacy-export|privacy-erase).
  This is the site binding of the narrow scopes.
- `assess_site_capabilities` (site_id, release, flag, state ∈ readable|partial|unreadable,
  evidence_hash, checked_at; PK site+flag). Missing row = UNREADABLE (ARCH-070).
- `assess_setup_checks` (site_id, item, result ∈ ok|missing, run_at): content-free.
- `assess_activity_config` (site_id, cmid UNIQUE pair, activity_type ∈ quiz|assign, enabled,
  flags jsonb (non-sensitive), rag_enabled bool NOT NULL DEFAULT false, end_at, attested_opening_at,
  attested_by, recipients_extra text[], teacher_config_sealed bytea NULL, teacher_config_kid,
  teacher_config_set bool GENERATED from IS NOT NULL), `assess_extra_time`, `assess_reset_grants`.
- `assess_sessions` (site_id, cmid, student_userid bigint, session_no int, attempt_ref,
  state ∈ STARTING|ACTIVE|OUTAGE|CLOSED, close_reason ∈ the six REQ-067 values,
  `CHECK ((state='CLOSED') = (close_reason IS NOT NULL))`, starting_expires_at, deadline_at,
  credited_outage_s, seq int, epoch int, notice_version, regrade_flag, committed_at, closed_at).
  - `UNIQUE (site_id, student_userid, cmid) WHERE state <> 'CLOSED'` (REQ-067).
  - `UNIQUE (site_id, student_userid, cmid, session_no)`.
  - `UNIQUE (id, state)` as the target of the hand-off composite FK.
  - Rollback DELETEs the STARTING row (snapshot and keys cascade) and leaves an
    `assess_start_attempts` row. That is how "rollback → ELIGIBLE, 0 snapshot rows" is
    implemented without a ROLLED_BACK state polluting the partial index.
  - A DEFERRABLE INITIALLY DEFERRED constraint trigger rejects any committed session in
    ACTIVE/OUTAGE/CLOSED without an `assess_turns` row of kind `opening`, which makes
    `consumed == exists(opening turn)` (REQ-067) structural.
- `assess_start_attempts`, `assess_outage_incidents`, `assess_enrolment_checks` (session_id,
  checked_at, result).
- `assess_snapshots` (session_id PK FK ON DELETE CASCADE, ciphertext, content_hash, created_at).
  A BEFORE UPDATE trigger raises unconditionally (BR-015: immutable; never refreshed).
- `assess_turns` (session_id, turn_no; PK; role, kind ∈ opening|normal|interrupted|system,
  ciphertext, turn_hash, prev_hash, marker_flags int, created_at). UPDATE and DELETE are revoked
  from every role except via purge.
- `assess_session_keys` (session_id PK FK CASCADE, kek_wrapped bytea NULL, kek_kid, key_version,
  shredded_at). `CHECK ((kek_wrapped IS NULL) = (shredded_at IS NOT NULL))`.
- `assess_session_handoff` (session_id PK, session_state text NOT NULL, epoch, sealed_dek,
  handoff_kid, created_at).
  - FK `(session_id, session_state) → assess_sessions(id, state) ON UPDATE CASCADE ON DELETE CASCADE`.
  - `CHECK (session_state IN ('ACTIVE','OUTAGE'))`.
  - This is the "CHECK" of ARCH-079. A hand-off row for a non-live session cannot be inserted,
    and a session cannot reach CLOSED while its hand-off row survives.
- `assess_transcripts` (session_id UNIQUE, ciphertext, dek_wrapped, transcript_kid, content_hash,
  generated_at, shredded_at), `assess_deliveries` (transcript_id, recipient_hash, recipient_sealed,
  state ∈ pending|sending|sent|retrying|abandoned, message_id, attempts, not_before, last_error_code;
  UNIQUE (transcript_id, recipient_hash)).
- `assess_jobs` (bigserial, kind (CHECK enum), args jsonb with `CHECK (pg_column_size(args) < 1024)`
  (ids only), dedupe_key, state ∈ ready|leased|done|failed|dead, not_before, lease_until,
  leased_by, attempts, max_attempts, created_at, done_at).
  - `UNIQUE (dedupe_key) WHERE state IN ('ready','leased')`.
  - Claim index `(state, not_before) WHERE state='ready'`.
  - Autovacuum tuned via `ALTER TABLE ... SET (autovacuum_vacuum_scale_factor=0.02,
    autovacuum_vacuum_cost_delay=0)`.
  - It is also the transactional outbox (close → `transcript_generate` job in the same
    transaction).
- `assess_render_checks` (site_id, student_userid, cmid, requested_at; `UNIQUE (...) WHERE
  done_at IS NULL`), `assess_render_state`, `assess_gate_inputs`: worker-written, content-free.
- `assess_consumed_jtis` (jti_hash PK, aud, expires_at, consumed_at),
  `assess_transcript_download_tokens` (jti_hash PK, transcript_id, subject_userid, expires_at,
  consumed_at).
- `assess_audit_events` (bigserial, site_id, session_id NULL, event, reason_code, actor_kind,
  data jsonb with `CHECK (pg_column_size(data) < 2048)`, created_at). It is append-only: no
  UPDATE/DELETE grant for any role, and a trigger raises on UPDATE. It is written IN the business
  transaction, so an audit failure aborts the action (fail closed). This is unlike
  `vektra_shared.audit.log_event`, which swallows write failures.
- `assess_erasure_records` (site_id, subject_userid_hash, requested_at, completed_at,
  rows_deleted jsonb), `assess_heartbeat` (component PK, beat_at, details jsonb content-free).

**SECURITY DEFINER functions.** All are owned by a new NOLOGIN role `vektra_assess_definer`,
which holds only the privileges the functions need. They have
`SET search_path = pg_catalog, pg_temp`, fully qualify `public.assess_*`, and run
`REVOKE EXECUTE ... FROM PUBLIC` (Postgres grants EXECUTE to PUBLIC by default; this is the
easiest grant to miss). The API role then gets `GRANT EXECUTE`.
- `get_session_material(p_sid uuid, p_epoch int)` returns (sealed_dek, handoff_kid, snapshot
  ciphertext, turns ciphertext ordered, seq). It joins sessions to handoff with
  `s.state IN ('ACTIVE','OUTAGE') AND s.epoch = p_epoch AND h.epoch = s.epoch`. Zero rows means
  the turn is refused.
- `assess_append_turn(p_sid, p_epoch, p_expected_seq, p_role, p_kind, p_ciphertext, p_turn_hash,
  p_prev_hash)`:
  - takes `SELECT ... FOR UPDATE` on the session;
  - requires state ACTIVE, epoch equality, `seq = p_expected_seq`, and that `p_prev_hash` equals
    the last turn's hash;
  - inserts turn_no = last+1 and bumps seq;
  - returns the new turn_no, or NULL on any failed predicate.
- `assess_close_session(p_sid, p_epoch, p_reason)` is an addition to ARCH-079, so that the API
  lazy close needs no DELETE grant on hand-off. It deletes the hand-off, sets CLOSED/close_reason/
  closed_at, inserts the `transcript_generate` outbox job and the audit event in one statement
  block, and is idempotent on an already-CLOSED session.
- CLOSED trigger: a BEFORE UPDATE OF state trigger on `assess_sessions` (SECURITY DEFINER owner
  `vektra_assess_definer`) deletes the hand-off row when NEW.state = 'CLOSED'. ARCH-079 says AFTER
  UPDATE. BEFORE is chosen because it runs before the composite-FK cascade, which would otherwise
  turn a forgotten application-side delete into a failed close instead of a defended one. Either
  way, CLOSED with a hand-off row is unrepresentable.

**Grant matrix (INT-002, written as data in `vektra_assess/db/grants.py`).** The matrix is the
artefact the CI test compares to `pg_catalog`, both ways: no missing grant and no extra grant.
API role (`vektra_assess_api`):
- **jobs, render_checks:** INSERT.
- **render_state, gate_inputs, enrolment_checks, heartbeat, sites, site_keys, site_capabilities:**
  SELECT.
- **sessions:** SELECT and INSERT; column UPDATE limited to `seq`, `epoch` and
  `last_activity_at`. State transitions go only through the functions or the worker.
- **start_attempts, audit_events, erasure_records:** INSERT.
- **activity_config:** INSERT, UPDATE and SELECT on every column EXCEPT `teacher_config_sealed`
  (column-level).
- **extra_time, reset_grants:** INSERT, SELECT, UPDATE.
- **consumed_jtis, transcript_download_tokens:** INSERT, SELECT, UPDATE(consumed_at).
- **transcripts and deliveries:** SELECT (download, REQ-093 export, teacher list).
- **No privilege at all:** snapshots, turns, session_keys, session_handoff.
- **EXECUTE:** the three functions.

Worker role (`vektra_assess_worker`):
- SELECT, INSERT, UPDATE and DELETE on plumbing, sessions, snapshots (INSERT and DELETE only),
  turns (INSERT and purge DELETE), session_keys, session_handoff, transcripts, deliveries,
  capability rows and erasure records;
- INSERT only on audit_events;
- no privilege on `api_keys` or any non-assess table.

Both roles are denied the following, and the test asserts it: CREATE on schema public, TRUNCATE,
REFERENCES, TRIGGER, and any privilege on non-assess tables.

## Tasks

### 1. Schema revisions (next revisions in the chain after 0009)
- [ ] 1.1 Revision "sites and config": `assess_sites`, `assess_site_keys`, `assess_site_capabilities`,
      `assess_setup_checks`, `assess_activity_config`, `assess_extra_time`, `assess_reset_grants`;
      ORM mirrors in `vektra_assess/db/models.py` (internal to the package, ADR-0022 style)
- [ ] 1.2 Revision "sessions": `assess_sessions` with the partial unique index, CHECKs, `UNIQUE (id,
      state)`, `assess_start_attempts`, `assess_outage_incidents`, `assess_enrolment_checks`,
      `assess_snapshots` (+ immutability trigger), `assess_turns`, the deferred opening-turn
      constraint trigger
- [ ] 1.3 Revision "keys": `assess_session_keys`, `assess_session_handoff` with the composite FK
      `ON UPDATE CASCADE` and the live-state CHECK; role `vektra_assess_definer` (NOLOGIN,
      idempotent DO block like 0009)
- [ ] 1.4 Revision "transcripts and plumbing": `assess_transcripts`, `assess_deliveries`,
      `assess_jobs` (+ autovacuum settings), `assess_render_checks`, `assess_render_state`,
      `assess_gate_inputs`, `assess_consumed_jtis`, `assess_transcript_download_tokens`,
      `assess_audit_events` (+ append-only trigger), `assess_erasure_records`, `assess_heartbeat`
- [ ] 1.5 Downgrades drop in reverse order and refuse (RAISE) if any `assess_sessions` row exists,
      so a downgrade can never silently discard exam evidence
- [ ] 1.6 Migration test (testcontainers pg16): upgrade head → downgrade to 0009 on an empty
      schema → upgrade head; `check_database_schema()` passes; `\d`-equivalent catalog query lists
      exactly the expected `assess_%` tables

### 2. SECURITY DEFINER functions and triggers
- [ ] 2.1 `get_session_material`, `assess_append_turn`, `assess_close_session` per Design Notes,
      owned by `vektra_assess_definer`, `SET search_path = pg_catalog, pg_temp`, `REVOKE EXECUTE
      FROM PUBLIC`, `GRANT EXECUTE TO vektra_assess_api` (and worker for close)
- [ ] 2.2 BEFORE UPDATE OF state trigger on `assess_sessions` deleting the hand-off row on CLOSED;
      trigger forbidding transitions out of CLOSED (`OLD.state='CLOSED' AND NEW.state<>'CLOSED'`
      raises), so no job can revive a closed session or re-wrap its DEK
- [ ] 2.3 Function tests as the real API role: wrong epoch → 0 rows; state CLOSED/STARTING → 0 rows;
      stale `p_expected_seq` → NULL and 0 turn rows added; wrong `p_prev_hash` → NULL; two
      concurrent `assess_append_turn` with the same seq → exactly one turn row (count verified)
- [ ] 2.4 Search-path hijack test: as the API role, create a temp function/table shadowing a name
      used inside the functions (`pg_temp.assess_sessions`); the function still reads the real table
- [ ] 2.5 Hand-off invariants test: INSERT hand-off for a STARTING or CLOSED session → error;
      UPDATE session to CLOSED (worker role, raw SQL, no explicit delete) → hand-off row count 0;
      transition CLOSED → ACTIVE → error

### 3. Grant matrix (INT-002)
- [ ] 3.1 `vektra_assess/db/grants.py`: the matrix as data (role → table → privileges, with column
      lists where column-level); revision "grants" generated FROM this module's rendered SQL, with
      the rendered SQL committed in the revision (no import of app code from the migration at run time)
- [ ] 3.2 `vektra-assess/tests/integration/test_grant_matrix.py`: query
      `information_schema.role_table_grants`, `column_privileges`, `pg_proc.proacl/prosecdef/proconfig`,
      `pg_roles` and compare to the matrix BOTH WAYS; fail on any `assess_%` table absent from the
      matrix; assert PUBLIC has no EXECUTE on the three functions; assert roles are not super,
      no BYPASSRLS, no CREATEROLE
- [ ] 3.3 Behavioural cross-check, connecting AS `vektra_assess_api`: `SELECT` on `assess_turns`,
      `assess_snapshots`, `assess_session_keys`, `assess_session_handoff`, and on
      `assess_activity_config.teacher_config_sealed` each raise `InsufficientPrivilege` (asserted on
      SQLSTATE 42501, not on "an exception")
- [ ] 3.4 Wire 3.2/3.3 into CI via the package-integration glob (`-m integration`); add a
      negative canary: a test-only revision adding `GRANT SELECT ON assess_turns TO
      vektra_assess_api` in a throwaway DB makes 3.2 fail

### 4. Crypto module `vektra_assess.crypto`
- [ ] 4.1 `crypto/envelope.py`: versioned envelope encode/decode, `aead_encrypt(key, plaintext,
      purpose, binding)` / `aead_decrypt(...)`, AAD construction per Design Notes; `new_dek()`
- [ ] 4.2 `crypto/kek.py`: `wrap_dek(keyring, dek, session_id) -> (blob, kid, key_version)`,
      `unwrap_dek(keyring, blob, session_id)`; unknown kid → `KeyNotAvailable` (never a fallback
      to another kid)
- [ ] 4.3 `crypto/seal.py`: `seal(recipient_pub, kid, plaintext, purpose, binding)` /
      `open_sealed(recipient_priv_keyring, blob, purpose, binding)`; HPKE API if present in the
      locked version, primitive construction otherwise, identical wire format; known-answer tests
      for the primitive path
- [ ] 4.4 `crypto/teacher_config.py`: `seal_teacher_config(worker_pub, site_id, cmid, field, text)`
      (API side) and `open_teacher_config(worker_priv, ...)` (worker side); a test that the API key
      set (per `KEY_MATRIX`) cannot open it
- [ ] 4.5 `crypto/transcript.py`: per-transcript DEK, wrapped by the `assess_transcript_kek` keyring;
      `encrypt_transcript` / `decrypt_transcript`
- [ ] 4.6 `crypto/hashing.py`: canonical snapshot hash, turn hash chain, transcript hash;
      `verify_chain(turns)`
- [ ] 4.7 Unit tests: round trips; tamper one byte of ct, nonce, kid or AAD binding → `InvalidTag`;
      ciphertext moved to another session_id/turn_no → `InvalidTag`; nonce uniqueness over 10⁵
      encryptions; repr/str of key objects contain no key bytes; structlog capture of a full
      round trip contains no key or plaintext canary

### 5. Shredding, purge and rotation helpers (library functions; jobs are wired in later plans)
- [ ] 5.1 `crypto/shred.py` `shred_session(conn, session_id) -> ShredReport`: in one worker-role
      transaction set `kek_wrapped = NULL, shredded_at = now()`, then delete turns, snapshot,
      hand-off, session row; return per-table deleted counts; refuse non-CLOSED sessions
- [ ] 5.2 `shred_transcript(conn, transcript_id)`: null `dek_wrapped`, delete deliveries and the
      transcript; same report shape (used by plan 20261101-154950-assess-privacy-retention for
      retention purge and OQ-033 erasure)
- [ ] 5.3 `rotation.py` `rewrap_session_keys(conn, keyring, from_kid, batch)`: unwrap with old kid,
      wrap with active kid, update `kek_kid/key_version` per row in short transactions; resumable;
      returns remaining count
- [ ] 5.4 `reseal_handoffs(conn, kek_keyring, new_api_pub, new_kid)` (worker side, at most K live
      rows): unwrap from `assess_session_keys`, re-seal, update hand-off rows still live; skips rows
      whose session closed meanwhile (the composite FK makes the race safe)
- [ ] 5.5 `reseal_teacher_configs(conn, old_worker_priv, new_worker_pub)` and
      `rewrap_transcript_deks(conn, keyring, from_kid)`
- [ ] 5.6 Tests: after `shred_session`, `unwrap_dek` on any surviving bytes is impossible (row
      absent), per-table counts are 0 by `SELECT count(*)`, and a `pg_dump --data-only` of the test
      DB contains neither the session id in any assess table nor the plaintext canary; rotation
      tests verify every row decrypts under the new kid and the old kid can be removed from the
      keyring afterwards

### 6. End-to-end data tests on real roles
- [ ] 6.1 Fixture `vektra-assess/tests/integration/conftest.py`: PostgresContainer
      `pgvector/pgvector:pg16`, `alembic upgrade head` as superuser, set LOGIN passwords for the
      assess roles, yield three engines (superuser for setup, API role, worker role); generated
      keyrings from `vektra_assess.keys generate`
- [ ] 6.2 Race test: 20 concurrent API-role transactions inserting STARTING for the same (site,
      student, cmid) → exactly 1 row (`SELECT count(*)`), 19 `UniqueViolation` (REQ-067)
- [ ] 6.3 Lifecycle walk with real ciphertext: worker inserts snapshot+keys+opening turn and
      commits ACTIVE with hand-off; API role decrypts via `get_session_material` + `open_sealed`;
      API appends 3 turns; `assess_close_session` → hand-off count 0, outbox job count 1, audit row
      count 1; `get_session_material` → 0 rows
- [ ] 6.4 Rollback walk: STARTING with snapshot deleted by the worker → 0 session, 0 snapshot,
      0 key rows, 1 `assess_start_attempts` row (BR-015, REQ-071); committing ACTIVE without an
      opening turn fails at COMMIT (deferred constraint)
- [ ] 6.5 Plaintext canary scan: after 6.3, `pg_dump --data-only` and a full-table `bytea` scan of
      every `assess_%` table contain 0 occurrences of the snapshot/turn/teacher-config canaries;
      postgres container logs contain 0 occurrences (with `log_statement=none` from plan
      20261101-154950-assess-foundations)
- [ ] 6.6 Snapshot immutability: worker-role UPDATE of `assess_snapshots` raises; a superuser
      forced mutation (trigger disabled) is detected by `hashing` re-verification (BR-015 "forced
      post-commit mutation is detected")

### 7. ~~Conditional: pgcrypto fallback~~ — REMOVED
- Removed 2026-11-01 (coordinator decision, ADR-0031 amended): `cryptography` is a hard
  dependency; no pgcrypto or weaker path. Numbering kept so cross-references stay valid.

### 8. Wiring, docs, CI
- [ ] 8.1 `vektra_assess/health.py` (from plan 20261101-154950-assess-foundations): read
      `assess_heartbeat` via the API role; report `degraded` when no row or age >
      `HEARTBEAT_MAX_AGE_S`; unit test with a stale row
- [ ] 8.2 Add `vektra_assess.crypto` and `vektra_assess.db` to the mypy path (already covered by
      `vektra-assess/src`) and confirm `test-assess` runs the new unit tests; integration tests run
      under the package-integration glob
- [ ] 8.3 Docs: `docs/reference/configuration.md` (keyring purposes, kid rotation); operator runbook
      section "assess keys: backup, restore drill, KEK re-wrap, hand-off re-seal" in
      `docs/guides/` (offline backup separate from DB dumps; a lost KEK = all snapshots lost);
      `.s2s/architecture.md` is NOT edited here — drift is listed in Notes for the design owner
- [ ] 8.4 CHANGELOG `[Unreleased]`: assess schema, roles' grants, crypto module

## State & Data Lifecycle

| State element | Created by | Updated by | Invalidated/Deleted by |
|---------------|-----------|------------|------------------------|
| `assess_sessions` row | API Start txn (STARTING) | Worker commit (ACTIVE), outage (OUTAGE), API `seq/epoch`; `assess_close_session` / worker close (CLOSED) | Worker rollback DELETE (STARTING only); `shred_session` after retention (5.1) |
| `assess_snapshots` | Worker pre-flight | Never (trigger raises) | Cascade on rollback delete; `shred_session` |
| `assess_turns` | Worker (opening, interrupted); API via `assess_append_turn` | Never | `shred_session` |
| `assess_session_keys.kek_wrapped` | Worker pre-flight | `rewrap_session_keys` (rotation) | Set NULL by `shred_session` (crypto-shred), then row deleted |
| `assess_session_handoff` | Worker commit-ACTIVE txn | `reseal_handoffs` (API key rotation) | `assess_close_session`; BEFORE UPDATE trigger on CLOSED; cascade on session delete |
| API in-memory DEK cache (per sid, epoch) | Plan 20261101-154950-assess-session-lifecycle | — | Epoch bump, CLOSED, TTL > deadline (that plan; this plan provides the material only) |
| `assess_transcripts` / per-transcript DEK | Worker (plan 20261101-154950-assess-transcript-delivery) | `rewrap_transcript_deks` | `shred_transcript` (5.2) at retention / erasure |
| `assess_deliveries` | Worker on transcript generation | Worker delivery attempts | `shred_transcript` |
| `assess_jobs` (incl. outbox) | API Start txn, `assess_close_session`, worker ticks | Worker claim/lease/complete | Done-job cleanup tick (plan 20261101-154950-assess-job-loop) |
| `assess_render_checks` / `render_state` / `gate_inputs` / `enrolment_checks` | API (checks) / worker (state, inputs) | Worker refresh ticks | Worker cleanup tick (plan 20261101-154950-assess-job-loop); rows are content-free |
| `assess_consumed_jtis` / `assess_transcript_download_tokens` | API mint/consume | API `consumed_at` | Cleanup tick deletes rows past `expires_at` (plan 20261101-154950-assess-job-loop) |
| `assess_activity_config.teacher_config_sealed` | API (sealed to worker key) | API replace ("sostituisci") | Activity deletion / site removal (plan 20261101-154950-assess-teacher-ops); `reseal_teacher_configs` on rotation |
| `assess_audit_events` | Business txns (API, worker, functions) | Never (trigger raises) | Retention policy for content-free audit, decided with OQ-020 (plan 20261101-154950-assess-privacy-retention) |
| `assess_erasure_records` | Privacy-erase endpoint | Worker on completion (counts) | Kept as proof of erasure (content-free, subject hashed) |
| `assess_site_capabilities` / `assess_setup_checks` | Worker setup check | Daily / release-change re-probe | Release change invalidates all flags (plan 20261101-154950-assess-moodle-ws) |
| `assess_heartbeat` | Worker tick | Every tick | Never deleted; staleness is the signal |

## Acceptance Criteria

- [ ] 20 concurrent Start inserts for the same (site, student, cmid) → `SELECT count(*)` = 1 non-CLOSED session (REQ-067)
- [ ] Committing ACTIVE without an opening turn fails at COMMIT; for every session row in the test corpus, `state <> 'STARTING'` ⇔ an `opening` turn exists (REQ-067 consumed rule)
- [ ] Rollback → 0 snapshot rows, 0 key rows, 0 session rows, 1 start-attempt row, counted (BR-015, REQ-071)
- [ ] As `vektra_assess_api`: SELECT on turns, snapshots, session_keys, session_handoff and `teacher_config_sealed` each raise SQLSTATE 42501 (INT-002, ARCH-079)
- [ ] The grant-matrix test passes in both directions, and the canary extra grant makes it fail (INT-002)
- [ ] `get_session_material` returns 0 rows for a wrong epoch, a CLOSED session and a STARTING session; `assess_append_turn` with a stale seq adds 0 rows under concurrency (NFR-014)
- [ ] After any close path, the hand-off row count for that session = 0, including a raw `UPDATE ... SET state='CLOSED'` with no explicit delete (ARCH-079)
- [ ] `pg_dump --data-only` plus the postgres container logs contain 0 plaintext canaries after a full lifecycle walk (BR-015, REQ-085)
- [ ] After `shred_session` / `shred_transcript`: per-table counts = 0 by query, and `pg_dump` contains no row for that session or transcript (REQ-089)
- [ ] Teacher config sealed by the API key set cannot be opened by any key the API holds per `KEY_MATRIX`; it is opened by the worker key (ARCH-071, BR-013)
- [ ] An `assess_audit_events` insert failure inside a business transaction aborts that transaction (fail closed; REQ-085)
- [ ] `rewrap_session_keys` leaves 0 rows on the old kid, and every DEK unwraps under the new kid (ARCH-071)

## Testing Approach

Every boundary is tested from the side of the actor it constrains. The tests connect as
`vektra_assess_api` and `vektra_assess_worker` with real LOGIN roles on pg16, never as the
superuser, except to set up fixtures and to simulate the forced-mutation attacker. Outcomes are
asserted on observable facts:
- row counts from `SELECT count(*)`;
- SQLSTATE codes;
- bytes, through `pg_dump` and bytea scans for canaries;
- successful or failed decryption.

They are never asserted on function return flags alone. Crypto unit tests include tamper,
transplant and known-answer cases. Concurrency tests use independent connections, not one
session with savepoints.

### Test Infrastructure

- **Required**: Docker for testcontainers `pgvector/pgvector:pg16`; `pg_dump` client in the test
  image (or `docker exec` into the container); generated keyrings; three engines (superuser, API
  role, worker role)
- **Provided by**: task 6.1 (fixture), plan 20261101-154950-assess-foundations (roles in migration
  0009, `vektra_assess.keys generate`, `test-assess` CI job, package-integration glob)
- **False-pass guard**: integration tests are `@pytest.mark.integration` and collected by the
  package-integration glob. The fixture asserts `current_user` for each engine before yielding,
  so a fixture that silently connects as superuser fails rather than passing every grant test. The
  grant test fails if the catalog query returns zero `assess_%` tables, which would mean the
  migration did not run. If Docker is unavailable, the tests SKIP with a reason, and the CI
  integration job treats a fully skipped assess integration suite as a failure (assert collected >
  0 and skipped == 0 in that job).

### NFR Verification

| NFR | Dataset/Load | Tool | Environment | Pass criterion |
|-----|--------------|------|-------------|----------------|
| NFR-014 | Negative matrix: wrong epoch, wrong sid, CLOSED, STARTING, stale seq, wrong prev_hash, API-role direct SELECTs | pytest integration as the API role | testcontainers pg16 | 0 material rows returned and 0 turns appended across the matrix; every direct SELECT → 42501 |
| NFR-018 | One session with a 200 KB snapshot plus 40 turns of ~1 KB | pytest-benchmark: `get_session_material` + `open_sealed` + full decrypt, 500 iterations | testcontainers pg16, CI runner | p95 ≤ 50 ms, a sub-budget well inside the NFR-018 p95 8 s time-to-first-output |

## Integration Notes

- Plan 20261101-154950-assess-job-loop owns the claim/lease SQL over `assess_jobs`, the cleanup
  ticks, and the heartbeat writer. This plan only fixes the columns and indexes.
- Plan 20261101-154950-assess-session-lifecycle uses the Start insert, the three functions and the
  DEK cache. Its token jti consumption uses `assess_consumed_jtis`.
- Plan 20261101-154950-assess-context writes the snapshot through `crypto.envelope` with purpose
  `snapshot`. The canonical form fed to `hashing` must be the exact bytes encrypted.
- Plan 20261101-154950-assess-transcript-delivery uses `crypto.transcript`, `assess_deliveries`
  and the outbox job created by `assess_close_session`.
- Plan 20261101-154950-assess-privacy-retention calls `shred_session` and `shred_transcript` and
  writes `assess_erasure_records`.
- Plan 20261101-154950-assess-teacher-ops uses `seal_teacher_config` and must never be granted
  SELECT on `teacher_config_sealed`.
- Later plans add columns only through new revisions, and every new `assess_%` table must get a
  matrix row, or the 3.2 test fails.

## Notes

- **Moot boundary until the API de-privileges**: the API process also holds the superuser
  `VEKTRA_DATABASE_URL`. The grant matrix and the SECURITY DEFINER functions defend against
  SQL-level bugs on the assess path, not against API code execution. Against code execution, the
  cryptography is the boundary: wrapped DEKs need the worker's KEK, and hand-off rows exist only
  for live sessions. See the BACKLOG proposal in plan 20261101-154950-assess-foundations.
- **Drift: CLOSED trigger is BEFORE, not AFTER UPDATE** (see Design Notes). Report this to the
  design owner for an ARCH-079 amendment.
- **Drift: `assess_close_session`** is a third SECURITY DEFINER function not named in ARCH-079 or
  INT-002. It is needed so the API's lazy close requires no DELETE grant on hand-off rows.
- **Drift: ARCH-070 names the table `moodle_site_capabilities`**; ARCH-073 fixes `assess_*`
  names, so this plan uses `assess_site_capabilities`.
- **Drift: audit store**: ARCH-077 sends content-free events to the existing `audit_log`, but
  `vektra_shared.audit.log_event` swallows write failures, the table is not append-only, and
  `key_id` is not meaningful for worker events. A dedicated append-only `assess_audit_events`,
  written in-transaction, is used instead (brief: no inherited fail-open defaults).
- **Resolved 2026-11-01: pgcrypto fallback dropped.** The incompleteness flagged here (no X25519
  equivalent) was resolved by making `cryptography` a hard dependency (ADR-0031 amended).
- **Risk: lost KEK** means every snapshot is lost (ARCH-079). The restore drill in 8.3 must be
  performed before the pilot, not just documented.
- **OQ-020 / OQ-033** (DPO, erasure vs evidence retention): the shredding helpers support both
  outcomes. The audit-event retention period is pending OQ-020.
- **OQ-034** (teacher config re-display): the write-only default is implemented. Re-display
  would need a symmetric API-held key (a documented exposure).
- Hash columns are stored in the clear by design (integrity anchoring). The equality-oracle
  trade-off is recorded in Design Notes.
- BUG-027 baseline result (from plan 20261101-154950-assess-foundations task 2.3): _pending_.
  Section 7 is N/A on PASS.
