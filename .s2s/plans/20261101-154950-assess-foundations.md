# Implementation Plan: Assess foundations — package, boundaries, keys, roles, topology skeleton

**ID**: 20261101-154950-assess-foundations
**Status**: in_progress
**Branch**: feat/assess-w1
**Created**: 2026-11-01T14:49:50Z
**Updated**: 2026-11-01T20:58:19Z
**Approved**: 2026-11-01 (coordinator)

## Traceability

**Source**: ARCH-066, ARCH-071, ARCH-073, ARCH-075, ARCH-078, COMP-002, COMP-003, COMP-008
**Source Type**: architecture

## References

### Requirements
- REQ-079: Site admin setup check for the dedicated service account (sidecar + key matrix as prerequisites) @.s2s/requirements.md
- REQ-085: Minimum audit event set for discussions (content-free logging baseline) @.s2s/requirements.md
- REQ-089: Privacy configuration, fail closed (no fail-open defaults inherited) @.s2s/requirements.md
- REQ-101: Input handling hardened (isolated, network-less parsing topology) @.s2s/requirements.md
- NFR-014: Attempt isolation (token helper with mandatory aud/iss, non-inheriting scopes) @.s2s/requirements.md
- NFR-020: Admission limit and expected peak (memory and connection envelope of the profile) @.s2s/requirements.md

### Architecture
- ARCH-066: process and secret isolation; parser sidecar network none; API refuses WS key @.s2s/architecture.md
- ARCH-071: key matrix, each process refuses to start missing/holding a key @.s2s/architecture.md
- ARCH-073: top-level vektra_assess, stable names, import-linter contracts (a)-(d), worker import ceiling @.s2s/architecture.md
- ARCH-075: 'assess' Compose profile, worker + parser from the same image @.s2s/architecture.md
- ARCH-078: technology choices; BUG-027 baseline in week 1; postgres log_statement=none @.s2s/architecture.md
- COMP-002: vektra_assess API (holds signing key, hand-off key, transcript key, teacher-config public key) @.s2s/architecture.md
- COMP-003: vektra-assess-worker (holds KEK, WS, SMTP, teacher-config private key; no listener) @.s2s/architecture.md
- COMP-008: vektra-assess-parser (sidecar) @.s2s/architecture.md
- INT-002: API to worker channel via Postgres (roles, connection budget) @.s2s/architecture.md

### Decisions
- ADR-0027: assess as top-level vektra_assess with thin Moodle plugin @.s2s/decisions/ADR-0027-assessment-discussion-as-a-top-level-vektra-assess.md
- ADR-0028: trust domains and 'assess' Compose profile (ADR-0004 addendum) @.s2s/decisions/ADR-0028-assess-trust-domains-and-deployment-topology-asses.md
- ADR-0010: authentication gateway (scope layer extended with non-inheriting scopes) @.s2s/decisions/ADR-0010-authentication-gateway.md

### Dependencies
- None. Plan 20261101-154950-assess-moodle-ws starts in parallel (it needs only the package scaffold from section 1 of this plan).
- Downstream: every other assess plan depends on this one.

## Overview

This plan builds the empty, correctly fenced shell that every later assess plan fills in. It
delivers no student-visible behaviour. Its value is that the boundaries the design depends on are
mechanical from the first commit, not intentions written in an ADR:

- the `vektra_assess` package exists and is wired only by `vektra_app`;
- import-linter makes the forbidden imports a CI failure;
- the key matrix is enforced at process start, in both directions;
- the narrow `assess:*` scopes cannot be satisfied by `admin`;
- the API and the worker connect as distinct, non-superuser Postgres roles;
- the parser sidecar runs with no network and no secrets;
- shallow `/health` is not tied to the worker.

It also runs the BUG-027 qemu baseline against the new native dependencies (`cryptography`)
in week 1. *(Decision 2026-11-01, coordinator)* `cryptography` is a HARD dependency with no pgcrypto or weaker
fallback (ARCH-079 / ADR-0031 amended): a FAIL is a blocker to fix in the image (wheel/build),
not a branch to another crypto path. Plan 20261101-154950-assess-data-crypto cannot start its
crypto module without a PASS.

Several items here are guards around capabilities that are dormant today. Examples are an API
container that happens to hold a WS key, an `admin` key passing an `assess:mint` check, or a
rate limiter that is never set. The CLAUDE.md rule applies to them: each guard is proved by a
negative test that runs the real check and observes the refusal, not by a status flag.

## Design Notes

**Package layout.** `vektra-assess/src/vektra_assess/` with stub sub-packages created now so that
the contracts bind to real modules: `api/`, `worker/`, `moodle/`, `parser_client/`, `context/`,
`llm/`, `transcript/`, `delivery/`, `crypto/`, `db/`, plus `config.py`, `keys.py`, `errors.py`
(re-exports only), `health.py`. Each stub has a module docstring naming the owning plan. No
sub-package `__init__` imports another sub-package, so a partial import graph stays partial.

**Import-linter contracts (ARCH-073 adapted).** Drift fix: ARCH-073(b) says vektra_assess may
import "the vektra_core public API" and "the vektra_analytics write API". In the codebase,
`LLMProvider` and `SafeguardHook` live in `vektra_shared.protocols`, `QueryTrace` lives in
`vektra_shared.types`, and every existing contract forbids component-to-component imports. The
adapted contracts are therefore:
- (a) `vektra_assess` and `vektra_learn` are independent (`type = "independence"`).
- (b) `vektra_assess` may not import `vektra_core`, `vektra_analytics`, `vektra_learn`,
  `vektra_ingest`, `vektra_index`, `vektra_admin` or `vektra_app`. The LLM provider, the
  safeguard and the trace writer arrive by injection from the two composition roots in
  `vektra_app`: `create_app()` and the new `vektra_app/assess_worker.py`.
- (c) Only `vektra_assess.worker` may import `vektra_assess.moodle` and
  `vektra_assess.parser_client`. This is a forbidden contract whose source modules are every
  other assess sub-package (api, context, llm, transcript, delivery, crypto, db, health, config,
  keys). A test asserts that the source list equals the set of sub-packages on disk minus
  `worker`, `moodle` and `parser_client`, so a new sub-package cannot slip outside the contract.
- (d) "Only `vektra_assess.context` constructs `ContextBundle`" cannot be expressed by
  import-linter, because other modules legitimately import the type. It becomes an AST test,
  `vektra-assess/tests/test_boundaries.py::test_contextbundle_constructed_only_in_context`: it
  scans for `ContextBundle(` calls and `ContextBundle.__new__`/`model_construct` outside
  `vektra_assess/context/`. Plan 20261101-154950-assess-context owns the type. This plan ships
  the test, which is SKIPPED with the reason "ContextBundle not defined yet" while the symbol is
  absent, and fails once the symbol exists and a construction site appears outside context.
- (e) The external packages `sentence_transformers`, `torch`, `transformers`, `qdrant_client`,
  `presidio_analyzer`, `presidio_anonymizer`, `spacy` and `unstructured` are forbidden for
  `vektra_assess` (`include_external_packages = true` already). `litellm` is NOT forbidden by
  contract, because the worker composition root injects `LitellmProvider` from `vektra_app`.
  Its import cost is measured instead (see the RSS test).
- Every existing contract adds `vektra_assess` to `forbidden_modules`.

**Non-inheriting scopes.** `require_exact_scope(scope)` in `vektra_shared/auth.py` mirrors
`require_scope` but tests `scope in info.scopes` without the ARCH-059 admin shortcut. Missing
scope → ERR-AUTH-003 (403). The existing auth taxonomy is reused rather than adding an assess
code. Migration 0009 replaces `ck_api_keys_scopes` with the seven-value set. It also adds
`ck_api_keys_assess_scope_exclusive`: a key holding any `assess:*` scope holds no
`admin`/`ingest`/`query` scope. This puts ARCH-067's "a plugin leak no longer gives Vektra
admin" in the database. Site binding of these keys belongs to `assess_sites` (plan
20261101-154950-assess-data-crypto).

**Shared JWT helpers (`vektra_shared/tokens.py`).** The helpers are `encode_jwt(claims, *,
key: SigningKey, audience: str, issuer: str, ttl: timedelta)` and `decode_jwt(token, *,
keyring: Keyring, audience: str, issuer: str, require: Sequence[str], leeway_s: int = 0)`.
- All parameters are keyword-only and have no defaults for audience, issuer and require. A call
  without them is a `TypeError`, not a silent unbound decode.
- The algorithm is pinned to HS256 and the `kid` header is mandatory and must exist in the
  keyring. `alg: none` and unknown kids are rejected. `require` is always merged with
  `{"exp", "iat", "aud", "iss"}`.
- Learn refactor: today `LearnService.validate_token` decodes with HS256 only, does not require
  `exp`, and issues no aud/iss/kid. Because PyJWT rejects an aud-bearing token when no audience
  is given, learn already refuses assess tokens by construction; this is kept. After the
  refactor, learn mints `aud="vektra-learn-widget"`, `iss="vektra"` and `kid="learn-1"`, and
  verifies them strictly. That is a tightening, so tokens minted before the upgrade (TTL
  1-24 h) are rejected once.
- Task 3.4 verifies empirically whether the widget and block_vektra recover by re-minting on
  401. If they do not, a learn-private `_decode_legacy_widget_token` is kept in
  `vektra_learn/service.py` for exactly one release. It has today's semantics (not looser), is
  never exported from `vektra_shared`, and is removed by a dated CHANGELOG item.

**Keyring files and the key matrix.** Each secret is a Docker secret file under
`/run/secrets/` holding a JSON keyring `{"active": "<kid>", "keys": {"<kid>": "<base64>"}}`, so
that rotation (ARCH-071) is a file change and not a schema change. `vektra_assess/keys.py`
does the following:
- `load_keyring(name)` checks that the file is regular, owned by the process uid or root, mode
  ≤ 0440, and that the key lengths are right (32 bytes for AES and X25519). It never logs key
  bytes and the repr is redacted.
- `KEY_MATRIX` maps each key name to `{api: required|forbidden|allowed, worker: ..., parser: ...}`
  following ARCH-071:

  | Key file | API | Worker | Parser |
  |---|---|---|---|
  | `assess_token_signing` | required | forbidden | forbidden |
  | `assess_handoff_private` (X25519) | required | forbidden | forbidden |
  | `assess_handoff_public` | allowed | required | forbidden |
  | `assess_transcript_kek` | required | required | forbidden |
  | `assess_teacher_config_public` | required | allowed | forbidden |
  | `assess_teacher_config_private` | forbidden | required | forbidden |
  | `assess_kek` | forbidden | required | forbidden |
  | `assess_ws_tokens` | forbidden | required | forbidden |
  | `assess_smtp` | forbidden | required | forbidden |

- `enforce_key_matrix(role)` raises `StartupValidationError` naming every violated key. A
  forbidden key is detected by file presence under the secrets dir AND by any env var whose
  name matches the key (defence against `.env` leakage).
- The API applies the forbidden half ALWAYS, even with `VEKTRA_ASSESS_ENABLED=false`. That is
  ARCH-066's dormant-capability guard.
- The parser asserts the secrets dir is absent or empty and that no `VEKTRA_*` DSN or key env
  var is set.

**Postgres roles.** The roles are `vektra_assess_api` and `vektra_assess_worker`, both
NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS NOINHERIT. They are created three ways:
- for fresh volumes, NOLOGIN by `deploy/postgres/init.sql`;
- idempotently, NOLOGIN, by migration 0009 (`DO $$ ... IF NOT EXISTS ...`), for existing volumes
  where init.sql never re-runs;
- with LOGIN and a password from secret files by a one-shot `vektra-assess-db-init` service in
  the 'assess' profile.

Passwords never appear in migrations or the shared `.env`. Grants are NOT written here: plan
20261101-154950-assess-data-crypto owns the grant matrix and its pg_catalog assertion.

**Moot boundary, stated explicitly.** The main API keeps `VEKTRA_DATABASE_URL` as the
superuser `vektra` for all non-assess modules. So the restricted assess role and the SECURITY
DEFINER boundary protect against SQL-level bugs on the assess path (a missed predicate, an
injected query), but NOT against code execution in the API process. Such an attacker reads the
superuser DSN from the environment. ARCH-079's cryptography still bounds that exposure
(wrapped DEKs need the worker's KEK, and hand-off rows exist only for live sessions), but the
grant layer is defence in depth until the main DSN is de-privileged. Recommended BACKLOG item:
split the migration-owner role from a non-superuser runtime role for `VEKTRA_DATABASE_URL`.

**Health.** `vektra_admin/health.py::check_all` today excludes only `"llm"` from shallow
`/health`. Registering `("health", "assess")` would make Docker HEALTHCHECK and the CI startup
wait depend on the worker. The fix is a module constant `SHALLOW_EXCLUDED = {"llm", "assess"}`.
`/health/assess` stays reachable through the existing `/health/{component}` route and in
`?detail=full`.

**Rate limiter.** `RateLimiter.check(key: Hashable, rpm_limit)` is a type and storage
generalisation only. `app.state.rate_limiter` stays unset, so per-key RPM for API keys remains
dormant exactly as today. This plan does NOT arm it. Plan 20261101-154950-assess-session-lifecycle
instantiates its own limiter instance for per-token limits.

## Tasks

### 1. Package scaffold and the six touch points
- [x] 1.1 Create `vektra-assess/pyproject.toml` (hatchling, `packages=["src/vektra_assess"]`,
      version 0.7.1, deps: vektra-shared (workspace), sqlalchemy, asyncpg, httpx, pydantic-settings,
      cryptography, defusedxml, html5lib, aiosmtplib), `README.md`, `src/vektra_assess/__init__.py`,
      and the stub sub-packages listed in Design Notes, each with an owning-plan docstring
- [x] 1.2 Wire the touch points: root `[tool.uv.workspace] members`, `[tool.ruff.lint.isort]
      known-first-party`, `[tool.importlinter] root_packages`, `[tool.coverage.run] source`,
      `vektra-app/pyproject.toml` dependency + `[tool.uv.sources]`, `Dockerfile` (copy
      pyproject/README/src for vektra-assess in the builder stage); run `uv lock` and commit the lock
- [x] 1.3 Declare `cryptography`, `defusedxml`, `html5lib` explicitly (today transitive or
      ocr-extra only) and add `aiosmtplib` (absent); verify with `uv tree --package vektra-assess`
      that they resolve in the DEFAULT image, not only with the ocr extra
- [x] 1.4 Create `vektra-assess/tests/conftest.py` importing `vektra_shared.testing.hermetic_env`
      (required by `vektra-shared/tests/test_env_isolation_coverage.py`) and a smoke test that
      imports every stub sub-package

### 2. Week-1 BUG-027 baseline (ARCH-078) — run before plan 2's crypto tasks
- [x] 2.1 Add `vektra_assess`, `vektra_app.assess_worker`, `cryptography.hazmat.primitives.ciphers.aead`,
      `cryptography.hazmat.primitives.asymmetric.x25519`, `cryptography.hazmat.primitives.kdf.hkdf`,
      `defusedxml.ElementTree`, `html5lib`, `aiosmtplib` to the hand-written import list of the
      "Image imports on a pre-x86-64-v2 CPU (BUG-027)" step in `.github/workflows/integration.yml`
- [x] 2.2 Extend that step to EXECUTE one AES-256-GCM encrypt/decrypt round trip and one X25519
      exchange under `qemu64,+sse3` (import alone does not exercise the OpenSSL code path that
      would use missing instructions); keep the existing numpy control assertion first
- [x] 2.3 Record the result in Notes of this plan and of 20261101-154950-assess-data-crypto:
      PASS → proceed; FAIL → BLOCKER: fix the image so the wheel passes (no pgcrypto fallback
      exists, ADR-0031 amended 2026-11-01). Add a startup self-test (one AES-GCM + one X25519
      round trip) to the assess API and worker; failure refuses startup (fail closed)

### 3. Shared JWT helpers and learn refactor
- [x] 3.1 Add `pyjwt` to `vektra-shared/pyproject.toml`; create `vektra_shared/tokens.py` with
      `SigningKey`, `Keyring`, `encode_jwt`, `decode_jwt` per Design Notes (kw-only, no defaults
      for audience/issuer/require; HS256 pinned; kid mandatory and looked up in keyring)
- [x] 3.2 Unit tests `vektra-shared/tests/test_tokens.py`: wrong aud, missing aud, wrong iss,
      missing exp, expired, `alg: none`, `alg: HS512`, unknown kid, missing kid header, key from a
      different keyring, call without `audience=` (TypeError) — each asserted on the exception type
- [x] 3.3 Refactor `vektra_learn/service.py` `LearnService.generate_token`/`validate_token` onto
      the helpers with `aud="vektra-learn-widget"`, `iss="vektra"`, `kid="learn-1"`; keep
      `VEKTRA_LEARN_JWT_SECRET` ≥ 32 chars (startup step 10) and existing claims
- [x] 3.4 Verify empirically (running stack + block_vektra dev env from plan
      20261101-154950-assess-moodle-ws, or the widget's fetch path if the env is not up yet) what
      happens to a pre-upgrade learn token: if the widget re-mints on 401, document the one-off
      invalidation in CHANGELOG; otherwise add the learn-private legacy decoder for one release
- [x] 3.5 Regression test in `vektra-learn/tests/`: a token signed with the learn secret but
      carrying `aud="discussion"` is rejected by `validate_token`; a learn token is rejected by
      `decode_jwt(audience="discussion")`; the suite proves learn is not looser than before
      (missing exp now rejected, previously accepted)

### 4. Non-inheriting scopes
- [ ] 4.1 Add `require_exact_scope(scope)` to `vektra_shared/auth.py` (same key-store lookup,
      `request.state.key_id`, ERR-AUTH-001/003 envelopes; no admin superscope)
- [ ] 4.2 Unit tests in `vektra-shared/tests/test_auth.py` with the existing `MockKeyStore`: an
      admin-only key is REJECTED by `require_exact_scope("assess:mint")` (403, ERR-AUTH-003); a key
      with `assess:mint` passes; `assess:teacher` does not satisfy `assess:mint`
- [ ] 4.3 Migration `migrations/versions/0009_assess_scopes_roles.py` (revision "0009", down
      revision "0008"): drop/re-add `ck_api_keys_scopes` with the seven scopes, add
      `ck_api_keys_assess_scope_exclusive`, create the two NOLOGIN roles idempotently; downgrade
      refuses if any key holds an `assess:*` scope (no silent data loss)
- [ ] 4.4 Mirror the constraints in `vektra_admin/models.py` `__table_args__`; update
      `valid_scopes` in `vektra_admin/api.py` (create-key handler) and add the four checkboxes in
      `vektra_admin/templates/keys.html`, grouped under an "assess (site-bound)" label; the API
      rejects mixed admin+assess requests with the same message as the DB constraint
- [ ] 4.5 Route guard test `vektra-app/tests/test_assess_route_scopes.py`: introspects
      `create_app()` routes under `/api/v1/assess`, asserts each depends on `require_exact_scope`
      or the assess token verifier and never on `require_scope`; SKIPPED with reason "no assess
      routes registered yet" while the count is 0 (plan 20261101-154950-assess-session-lifecycle
      removes the skip by adding routes)

### 5. Settings, errors, keys
- [ ] 5.1 `vektra_assess/config.py` `VektraAssessSettings` (pydantic-settings, explicit
      `alias="VEKTRA_ASSESS_*"`, no env_file): `ENABLED` (default false), `API_DATABASE_URL`,
      `WORKER_DATABASE_URL`, `SECRETS_DIR` (default `/run/secrets`), `API_POOL_SIZE` (15),
      `WORKER_POOL_SIZE` (5), `HEARTBEAT_MAX_AGE_S` (60), `PARSER_SOCKET` path; later plans add
      their fields to this class only
- [ ] 5.2 Add `ERR_ASSESS_001` ("assess unavailable", 503, remediation names the failing
      readiness item) to `vektra_shared/errors.py` with `_CODE_STATUS_OVERRIDE`; document the
      ERR-ASSESS-NNN namespace rule in `docs/reference/error-codes.md` (later plans take the next
      free number; codes are never reused)
- [ ] 5.3 `vektra_assess/keys.py`: keyring loader, `KEY_MATRIX`, `enforce_key_matrix(role)`,
      redacted repr; `python -m vektra_assess.keys generate --out <dir>` writing all keyrings
      (X25519 pairs via cryptography, 32-byte random symmetric keys) with mode 0400; `make
      assess-keys` target writing to `./secrets/assess/` (gitignored)
- [ ] 5.4 Unit tests: for each role, every required key missing → refusal naming it; every
      forbidden key present (file AND env-var variants) → refusal naming it; mode 0644 file →
      refusal; wrong key length → refusal; generated set satisfies all three roles

### 6. Startup wiring (API) and worker entry
- [ ] 6.1 `vektra_app/main.py`: add `_step_12_assess_check` to the `_run_startup` list. It always
      runs `enforce_key_matrix("api")` forbidden half. When enabled it also: runs the required
      half, builds the assess engine from `VEKTRA_ASSESS_API_DATABASE_URL`, asserts `SELECT
      current_user, rolsuper` returns `vektra_assess_api`/false, and asserts the DSN user differs
      from `VEKTRA_DATABASE_URL`'s; registers the engine on `app.state.assess_engine`; runs
      `run_crypto_selftest()` before serving (task 2.3, fail closed)
- [ ] 6.1b Dormant by default (added 2026-11-01; second line of defence behind the wave-gated merges):
      `create_app()` includes the assess router ONLY when `VEKTRA_ASSESS_ENABLED=true`; with the
      default `false`, no route under `/api/v1/assess` is mounted, `/api/v1/assess/ui` is not
      served, `/health/assess` reports `disabled`, and the `assess-worker` / `assess-parser`
      entrypoints exit non-zero with a clear message instead of running. Test: build the app with
      the flag off and assert the route table contains zero `/api/v1/assess` paths and that a
      request to one returns 404 (the route table is the evidence, not a status field)
- [ ] 6.2 `vektra_app/assess_worker.py`: composition root stub — load settings,
      `enforce_key_matrix("worker")`, connect with `VEKTRA_ASSESS_WORKER_DATABASE_URL` asserting
      role `vektra_assess_worker`, import `LitellmProvider` (to measure its cost now), assert
      none of `sentence_transformers`, `torch`, `qdrant_client`, `vektra_index`, `vektra_ingest`,
      `presidio_analyzer`, `spacy` are in `sys.modules`, write a heartbeat FILE every 10 s,
      handle SIGTERM within 30 s; runs `run_crypto_selftest()`; exits non-zero on any failed check;
      add `vektra_app.assess_worker` to the BUG-027 import list (task 2.1)
- [ ] 6.3 `vektra_app/assess_parser.py` stub: `enforce_key_matrix("parser")` (no secrets, no
      DSN), bind the unix socket at `VEKTRA_ASSESS_PARSER_SOCKET`, answer a framed `ping`, reply
      `not_implemented` to everything else (plan 20261101-154950-assess-parser replaces it)
- [ ] 6.4 `docker/entrypoint.sh`: add `assess-worker` and `assess-parser` targets (no
      `alembic upgrade`; `exec python -m vektra_app.assess_worker` / `assess_parser`), update the
      "Valid targets" message; never route assess through a generic `worker` target (ARCH-066)
- [ ] 6.5 Tests `vektra-app/tests/test_assess_startup.py`: API with a WS keyring file present and
      assess DISABLED still fails step 12 with `StartupValidationError(step="assess_check")`;
      API with an assess DSN that logs in as `vektra` fails; worker entry with `torch` pre-imported
      (monkeypatched `sys.modules`) fails the assertion

### 7. Compose 'assess' profile and Postgres
- [ ] 7.1 `deploy/postgres/init.sql`: create `vektra_assess_api` / `vektra_assess_worker` NOLOGIN
      with the attributes in Design Notes; add `deploy/postgres/assess-roles.sql` run by the
      one-shot service (`ALTER ROLE ... LOGIN PASSWORD` from `/run/secrets/assess_db_*_password`)
- [ ] 7.2 `docker-compose.yml` postgres command adds `-c log_statement=none -c
      log_min_duration_statement=-1 -c log_parameter_max_length=0 -c
      log_parameter_max_length_on_error=0` (ARCH-078); verify with `SHOW` in an integration test
- [ ] 7.3 Profile `assess`: `vektra-assess-db-init` (one-shot, superuser DSN, db password
      secrets only), `vektra-assess-worker` (same image, `CMD_TARGET=assess-worker`, no ports,
      `mem_limit: 512m`, `stop_grace_period: 30s`, `restart: unless-stopped`, healthcheck = heartbeat
      file mtime < 30 s, secrets per the worker column, `depends_on` db-init completed),
      `vektra-assess-parser` (`network_mode: none`, `read_only: true`, `user: "1000:1000"`, tmpfs
      `/tmp` size cap, `mem_limit: 768m`, `pids_limit`, `cpus`, NO secrets, no env DSN)
- [ ] 7.4 Named volume `assess_parser_sock` mounted ONLY into worker and parser; the `vektra` API
      service gets the API-column secrets and `VEKTRA_ASSESS_API_DATABASE_URL`, and no socket volume
- [ ] 7.5 Compose policy test `tests/test_compose_assess_profile.py` parsing `docker compose
      --profile assess config` output: parser has `network_mode: none` and zero secrets; socket
      volume mounted in exactly {worker, parser}; every secret mounted into each service is allowed
      for that role by `KEY_MATRIX` (reuses the module, so the matrix and compose cannot diverge)
- [ ] 7.6 Integration test (compose, `-m integration`): from inside the parser container, a TCP
      connect to `postgres:5432` and a DNS lookup both fail; `/run/secrets` is absent; from the API
      container the socket path does not exist

### 8. Health and rate limiter
- [ ] 8.1 `vektra_admin/health.py`: `SHALLOW_EXCLUDED = {"llm", "assess"}`; unit test that a
      registered failing `assess` check leaves shallow status `healthy` while `?detail=full` and
      `/health/assess` report it
- [ ] 8.2 `vektra_assess/health.py` readiness function registered as `("health", "assess")` by
      create_app when enabled: returns `disabled` when off; when on, reads the heartbeat source
      (the table arrives with plan 20261101-154950-assess-data-crypto; until then reports
      `degraded` with reason `heartbeat_source_missing`, never `healthy`)
- [ ] 8.3 `vektra_admin/rate_limit.py`: `check(key: Hashable, rpm_limit)`; tests with UUID,
      `tuple`, and `str` keys; test asserting `app.state.rate_limiter` is still unset after
      `create_app()` (the dormant per-key RPM stays dormant)

### 9. CI and docs
- [ ] 9.1 Root `pyproject.toml` import-linter: contracts (a)-(c), (e) and `vektra_assess` added to
      all existing `forbidden_modules`; AST test (d) and the contract-coverage test in
      `vektra-assess/tests/test_boundaries.py`; a canary test that a temporary module importing
      `vektra_core` from `vektra_assess.api` makes `lint-imports` fail (run in a tmp copy)
- [x] 9.2 `.github/workflows/ci-unit.yml`: `assess` paths filter (`vektra-assess/**`,
      `vektra-shared/**`), `test-assess` job, add to `ci-gate` needs; `Makefile` `test` target adds
      `vektra-assess/tests/`; confirm `test_suite_execution_coverage.py` passes with the new suite
- [ ] 9.3 `.github/workflows/lint.yml`: add `vektra-assess/src` (and `vektra-app/src`, missing
      today) to the mypy path list
- [ ] 9.4 Integration job: build image, run `CMD_TARGET=assess-worker` with generated test keys
      and a pg16 service, wait 15 s idle, read `VmRSS` from `/proc/1/status`; fail above 300 MB;
      print the top import contributors (`python -X importtime`) as an artifact
- [ ] 9.5 `.env.example` (VEKTRA_ASSESS_* with comments, no secrets), `docs/reference/configuration.md`
      (settings + key matrix + secret file format), `docs/reference/api.md` (`/health/assess`
      readiness semantics; new scopes), CHANGELOG `[Unreleased]` (scopes, learn token aud/iss,
      postgres logging flags, new profile)

## State & Data Lifecycle

| State element | Created by | Updated by | Invalidated/Deleted by |
|---------------|-----------|------------|------------------------|
| `ck_api_keys_scopes` / `ck_api_keys_assess_scope_exclusive` constraints | 4.3 migration 0009 | Future scope additions via a new revision only | 0009 downgrade (refuses while assess keys exist) |
| Postgres roles `vektra_assess_api` / `vektra_assess_worker` | 7.1 init.sql (fresh), 4.3 migration (existing), 7.3 db-init (LOGIN) | 7.3 db-init on password rotation (re-run) | Operator runbook (`DROP ROLE` after revoking grants); not automated |
| Keyring secret files `/run/secrets/assess_*` | 5.3 generator / operator | Rotation: operator adds kid and flips `active` (re-wrap jobs in plan 20261101-154950-assess-data-crypto) | Operator removes old kid after re-wrap completes; process restart picks it up |
| In-process keyrings (API, worker) | 6.1 / 6.2 at startup | Only on restart (no hot reload, by design) | Process exit |
| `app.state.assess_engine` (API pool, size 15) | 6.1 | — | App shutdown disposes the engine |
| Worker heartbeat FILE | 6.2 every 10 s | 6.2 | Container restart (tmpfs/overlay); a stale mtime fails the healthcheck |
| `assess_parser_sock` volume / socket file | 7.4 compose; 6.3 binds | 6.3 rebinds on start (unlink stale path first) | `docker compose down -v`; parser exit leaves a stale path that 6.3 unlinks |
| `RateLimiter` windows (generalised key) | 8.3 instance per user | `check()` | `cleanup()` (unchanged; plan 20261101-154950-assess-session-lifecycle schedules it for its instance) |
| Learn widget tokens (aud/iss now) | 3.3 mint | — | Expiry; pre-upgrade tokens invalidated once (3.4) |

## Acceptance Criteria

- [ ] `uv run lint-imports` passes; adding `import vektra_core` to any `vektra_assess` module, or `import vektra_assess.moodle` to `vektra_assess.api`, makes it fail (canary test 9.1) — ARCH-073
- [ ] With `VEKTRA_ASSESS_ENABLED=false` (the default) the app's route table contains zero `/api/v1/assess` paths, a request to any of them returns 404, and the assess worker/parser entrypoints refuse to run (task 6.1b) — dormant-capability rule, `.claude/CLAUDE.md`
- [ ] An API container with `assess_ws_tokens` mounted refuses to start with `VEKTRA_ASSESS_ENABLED=false`, exit code non-zero, `[STARTUP ERROR] assess_check` naming the key — ARCH-066, REQ-079
- [ ] For each of the three roles, removing any required key or adding any forbidden key (file or env var) produces a refusal naming exactly that key (5.4, 6.5) — ARCH-071
- [ ] An `admin`-only key receives 403 ERR-AUTH-003 from a `require_exact_scope("assess:mint")` route; inserting a key row with `{admin,assess:mint}` fails with the DB constraint name in the error — NFR-014
- [ ] `decode_jwt` rejects the full negative matrix of 3.2 (11 cases), and a learn token and an assess-audience token are mutually rejected (3.5) — NFR-014
- [ ] `SELECT current_user, rolsuper` over the assess API DSN returns `vektra_assess_api, false`; over the worker DSN returns `vektra_assess_worker, false` (integration) — INT-002
- [ ] From inside the parser container: 0 successful TCP connects, 0 DNS resolutions, `/run/secrets` absent (7.6) — REQ-101, ARCH-066
- [ ] Stopping the worker container leaves `GET /health` 200 `healthy` while `GET /health/assess` reports a non-healthy status (8.1 + integration) — COMP-002
- [ ] Worker idle RSS measured from `/proc/1/status` ≤ 300 MB, and `sys.modules` contains none of the forbidden ML modules (9.4) — ARCH-073, NFR-020
- [ ] BUG-027 qemu step executes an AES-GCM round trip and an X25519 exchange on `qemu64,+sse3` and the result is recorded (task 2.3) — ARCH-078
- [ ] `SHOW log_statement` returns `none` and `SHOW log_parameter_max_length` returns `0` on the compose postgres — ARCH-078
- [ ] No `app.state.rate_limiter` after `create_app()` (dormant per-key RPM not armed as a side effect)

## Testing Approach

Most of this plan is guards. Each guard has a positive test (the correct configuration starts)
and a negative test that drives the real check into refusal and asserts on the step name and the
key or role it names. Status fields are not trusted. Role checks query `current_user` and
`pg_roles`. Network isolation is probed by actually attempting the connection from inside the
container. RSS is read from `/proc`. The compose policy test reads the rendered `docker compose
config`, not the YAML source, so env interpolation and profile merging are what get tested.

### Test Infrastructure

- **Required**: unit (no services) for tokens, scopes, key matrix, health, rate limiter, AST
  boundaries; testcontainers `pgvector/pgvector:pg16` for migration 0009 and role attributes;
  compose stack with `--profile assess` and generated test keys for 6.x/7.x/9.4; Docker + qemu for
  the BUG-027 step
- **Provided by**: tasks 1.4 (conftest/hermetic_env), 5.3 (`make assess-keys` test keyrings), 7.3
  (profile), 9.2/9.4 (CI jobs); the existing `integration.yml` BUG-027 step (extended by 2.1/2.2)
- **False-pass guard**: integration tests carry `@pytest.mark.integration` and are run by the
  package-integration glob; the AST ContextBundle test and the route-scope test SKIP with an
  explicit reason while their subject is absent and are listed in Notes as pending activation;
  the RSS job fails (not skips) if the container did not reach the heartbeat state; the qemu step
  keeps its control assertion so a non-masking emulator cannot pass

### NFR Verification

| NFR | Dataset/Load | Tool | Environment | Pass criterion |
|-----|--------------|------|-------------|----------------|
| NFR-014 | 11-case JWT negative matrix + exact-scope matrix (admin, wrong assess scope, correct scope) | pytest unit | CI unit job | 100% of negative cases rejected with the expected error; 0 accepted |
| NFR-020 | Idle worker process with real imports (LitellmProvider included) | `/proc/1/status` VmRSS after 15 s | CI integration, compose `--profile assess` | VmRSS ≤ 300 MB (input to the OQ-026/OQ-032 memory budget) |

## Integration Notes

- Plan 20261101-154950-assess-data-crypto consumes: the roles (writes their grants), `keys.py`
  keyrings and `KEY_MATRIX`, the BUG-027 result (task 2.3), the `ERR-ASSESS` namespace, and the
  heartbeat source for 8.2.
- Plan 20261101-154950-assess-moodle-ws needs only section 1 (scaffold) and can start in parallel.
- Plan 20261101-154950-assess-parser replaces the 6.3 stub and must keep `enforce_key_matrix("parser")`.
- Plan 20261101-154950-assess-session-lifecycle uses `require_exact_scope`, `vektra_shared.tokens`
  (aud `discussion` / `transcript`), and a dedicated `RateLimiter` instance keyed by token jti/sid.
- Plan 20261101-154950-assess-job-loop replaces the 6.2 heartbeat-file stub with the real loop,
  keeping the import assertion and the RSS job.
- Moodle plugin: none of these changes is visible to block_vektra except the learn token
  aud/iss change (3.4).

## Notes

- **Drift ARCH-073(b)**: the design permits vektra_core/vektra_analytics imports; the codebase's
  contracts forbid component-to-component imports and the Protocols are in vektra_shared. This
  plan forbids them and injects from vektra_app (brief decision).
- **Moot boundary**: until `VEKTRA_DATABASE_URL` stops being superuser, the assess grant matrix
  protects against SQL-level bugs, not API-process compromise (see Design Notes). Propose a
  BACKLOG item "de-superuser the runtime DSN".
- **Risk: litellm import cost** in the worker may consume most of the 300 MB ceiling; task 9.4
  measures it in week 1. If it exceeds, options are a lazy import inside the Part-B handler or a
  thin httpx OpenAI-compatible provider in vektra_app (decision recorded in plan
  20261101-154950-assess-llm-runner).
- **Learn token tightening (task 3.4, decided 2026-11-01)**: pre-upgrade tokens get one 401; the widget POSTs to
  `data-token-refresh-url` (block_vektra sets it, `ajax.php` re-mints) and retries once. Proved on the real
  `ApiClient` by `vektra-learn/widget/test/api-client.test.mjs` (stubbed fetch) plus the server-side rejection in
  `vektra-learn/tests/test_token_binding.py`. No legacy decoder kept; CHANGELOG documents the one-off effect.
- **Task 9.2 pulled forward** with section 3: `make test` and ci-unit run `vektra-assess/tests/` (job
  `test-assess`, in `ci-gate`), because `test_suite_execution_coverage.py` fails as soon as the suite exists.
  ci-unit also gains `test-learn-widget` (`npm test`, node 22, as the Dockerfile widget stage).
- **Risk: role creation in a migration** requires CREATEROLE; managed Postgres deployments may
  forbid it — the migration's DO block must fail with a clear message, and the operator runbook
  offers pre-created roles.
- **OQ-026 / OQ-032** (hardware, memory envelope): the profile limits here (512M worker, 768M
  parser) are design defaults; NFR-020's K is not fixed by this plan.
- **Pending activation**: test (d) ContextBundle AST guard and 4.5 route-scope guard are SKIPPED
  until plans 20261101-154950-assess-context and 20261101-154950-assess-session-lifecycle land.
- BUG-027 result (task 2.3): PASS (local, 2026-11-01): `cryptography==49.0.0` (the locked version) under `qemu-x86_64 -cpu qemu64,+sse3` in a linux/amd64 `python:3.12-slim` container: numpy control assertion ok (no POPCNT/SSE41/SSE42), AES-256-GCM round trip, X25519 exchange and HKDF all ok. The CI step in `integration.yml` now runs the same operations against the built image; its run on the W1 PR is the gate evidence. `vektra_app.assess_worker` is added to the import list by task 6.2 (the module does not exist before it). Self-test: `vektra_assess/crypto/selftest.py::run_crypto_selftest`, wired into API step 12 (6.1) and worker start (6.2).
