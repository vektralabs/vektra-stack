# Assess: deployment

Operator guide for the assessment discussion feature (`vektra_assess`). It describes the
Compose `assess` profile, the processes and their database roles, the connection budget, the
Postgres logging settings, the limits and health checks of each process, the TLS origins, and the
setup check that closes a deployment. Every setting named here is listed with its type, default
and range in [configuration](../../reference/configuration.md#assessment-discussion-vektra-assess).

Related operator documents:

- [Secrets and the key matrix](assess-secrets.md)
- [Keys: backup, restore drill and rotation](../assess-keys-runbook.md)
- [Framing: the discussion UI origin and the reverse proxy](assess-framing.md)
- [The LMS service account and the plugin's API keys](../assess-moodle-service-account.md)
- [Pilot go/no-go](assess-pilot-go-no-go.md)

## Worker start check

`vektra_assess.worker.slots.assert_all_bound` refuses to start a worker while any job kind or
reaper has no handler, unless `VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND=true`, which is honoured only with
`VEKTRA_ENV=development`. In the current tree every slot is bound: the `rotate_keys` job kind,
which had no handler and blocked every production start, was removed (revision `0044`, now part of the assess base revision; key
rotation is the `python -m vektra_assess.keys rewrap` command). The guard test
`vektra-app/tests/test_assess_processes.py` composes a fully keyed production worker without the
override and requires every slot bound. See the [pilot go/no-go](assess-pilot-go-no-go.md).

## Processes

The feature adds three containers to the core stack and four secrets-and-settings blocks to the
API. They are defined in `docker-compose.assess.yml` (an overlay, because the API service `vektra`
is always on and Compose has no optional secrets) and gated by the `assess` profile.

| Service | `CMD_TARGET` | What it does | Database role | Network | Limits |
|---|---|---|---|---|---|
| `vektra` (API) | `server` | runs `alembic upgrade platform@head` and, only with `VEKTRA_ASSESS_ENABLED=true`, `alembic upgrade assess@head` (see [the assess schema](#the-assess-schema-a-migration-branch-of-its-own)), then serves `/api/v1/assess` (only when enabled) | `vektra_assess_api` through `VEKTRA_ASSESS_API_DATABASE_URL`, next to the main `VEKTRA_DATABASE_URL` | default | `memory: 3G` (base file) |
| `vektra-assess-db-init` | none (`psql`) | one-shot: sets `LOGIN` and the passwords of the two assess roles (`deploy/postgres/assess-roles.sql`) | the Postgres superuser | default | `restart: "no"` |
| `vektra-assess-worker` | `assess-worker` | the job loop, the ticks, LMS reads, the Start pre-flight, transcripts, SMTP | `vektra_assess_worker` through `VEKTRA_ASSESS_WORKER_DATABASE_URL` | default, no published port | `mem_limit: 512m`, `stop_grace_period: 30s` |
| `vektra-assess-parser` | `assess-parser` | parses submitted files, each in a fresh child process | none | `network_mode: none` | `read_only`, user `1000:1000`, `mem_limit: 768m`, `pids_limit: 64`, `cpus: 1.5`, `cap_drop: [ALL]`, `no-new-privileges`, `/tmp` tmpfs 64m |

**Run one API process.** The event stream broker, the DEK cache of live sessions and the
per-token rate limiters live in the API process's memory (`vektra_assess.api.events`,
`api.material`, `api.housekeeping`): with two API processes behind a balancer, a student's stream
and turns could land on different processes. The runbooks mention "every API instance" for the
rollout order of shared keys; that covers rolling restarts, not parallel instances.

Start order: `postgres` healthy, then `vektra-assess-db-init` completed successfully, then the
API and the worker; the worker also waits for the parser to have started. The parser and the
worker share the `assess_parser_sock` volume (`VEKTRA_ASSESS_PARSER_SOCKET`,
`/run/vektra-assess-parser/parser.sock`); the API must not see that socket and refuses to start if
it does (`assert_no_parser_surface`). Only the API container runs migrations: the assess targets
never do (`docker/entrypoint.sh`).

## Bring the stack up

```bash
make assess-keys                       # writes ./secrets/assess/ (mode 0400, never overwrites)
sudo chown -R 1000:1000 secrets/assess # Linux engines only: see the keys runbook
docker compose -f docker-compose.yml -f docker-compose.assess.yml --profile assess up -d
```

`make assess-keys` runs `python -m vektra_assess.keys generate --out ./secrets/assess
--provider-secret assess_ws_tokens`. It writes every secret of the key matrix with kid `k1`, plus
the credentials file the Moodle LMS provider declares; `assess_ws_tokens` and `assess_smtp` are
written as empty JSON objects for you to fill (see [secrets](assess-secrets.md)). Set
`VEKTRA_ASSESS_SECRETS_PATH` to mount the secrets from another host directory.

For development and CI only, add `-f docker-compose.assess.dev.yml`: it sets
`VEKTRA_ENV=development`, `VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND=true`,
`VEKTRA_ASSESS_LLM_ALLOW_UNACTIVATED=true`, `VEKTRA_ASSESS_PRIVACY_ALLOW_UNSET=true` and
`VEKTRA_ASSESS_SAFEGUARD_MODE=none`. The three boolean overrides are refused
outside development. Every assess development-only switch needs `VEKTRA_ENV=development` set
explicitly: the platform default of `VEKTRA_ENV` is `development`, but assess does not count the
default, so a production `.env` should set `VEKTRA_ENV=production`. `VEKTRA_ASSESS_SAFEGUARD_MODE=none` is accepted outside development only
with a model on the institution's own hardware: see
[the safeguard and the model's locality](#the-safeguard-and-the-models-locality). The default
is `presidio`, which refuses to start when Presidio cannot load.

## The safeguard and the model's locality

`VEKTRA_ASSESS_SAFEGUARD_MODE=none` runs the discussion with a passthrough safeguard. Whether
the API accepts it depends on the environment and on where the model runs
(`VEKTRA_ASSESS_PROVIDER_LOCALITY`, default `cloud`):

| Environment | `VEKTRA_ASSESS_PROVIDER_LOCALITY` | `VEKTRA_ASSESS_SAFEGUARD_MODE=none` |
|---|---|---|
| development (`VEKTRA_ENV=development`, set explicitly) | any | accepted |
| not development, or `VEKTRA_ENV` unset | `onprem` | accepted |
| not development, or `VEKTRA_ENV` unset | `cloud` (the default) | refused at startup |

Declare `onprem` only when the model runs on the institution's hardware. An unset `VEKTRA_ENV`
is not development for assess, even though the platform default is `development`.

**Put these values in the deployment `.env`, not in a shell export.** The API reads them from
`.env` (its `env_file`); a value exported in the shell that runs `docker compose` is not passed
to the API container, so the API would not see it and would start with whatever `.env` says.
The development overlay `docker-compose.assess.dev.yml` sets `VEKTRA_ENV=development` and
`VEKTRA_ASSESS_SAFEGUARD_MODE=none` in the API's `environment`, which wins over `.env`: with that
overlay, `VEKTRA_ASSESS_SAFEGUARD_MODE=presidio` in `.env` has no effect. To run Presidio in a
development stack, add a local Compose override after the overlay that sets the API's
`VEKTRA_ASSESS_SAFEGUARD_MODE` to `presidio`.

With `none` and a cloud model outside development, the API stops at startup with:

```text
[STARTUP ERROR] Step: assess_llm_check
  Detail: VEKTRA_ASSESS_SAFEGUARD_MODE=none with VEKTRA_ASSESS_PROVIDER_LOCALITY=cloud is only honoured with VEKTRA_ENV=development (this process runs with VEKTRA_ENV=production)
  Remediation: with a cloud model the safeguard is required: set VEKTRA_ASSESS_SAFEGUARD_MODE=presidio, or declare VEKTRA_ASSESS_PROVIDER_LOCALITY=onprem if the model runs on the institution's hardware.
```

With `VEKTRA_ENV` unset the parenthesis reads `(this process runs with VEKTRA_ENV unset; the
default 'development' does not count for assess: set VEKTRA_ENV=development explicitly)`; every
other development-only switch is refused with the same parenthesis.

To fix it, in `.env`, either remove `VEKTRA_ASSESS_SAFEGUARD_MODE=none` (the default `presidio`
applies; the image needs the Presidio extra and a spaCy model), or, only if the model really runs
on the institution's hardware, add `VEKTRA_ASSESS_PROVIDER_LOCALITY=onprem`. Then recreate the
API container (`docker compose ... up -d`), which re-reads `.env`.

## Settings the API and the worker must share

The worker has no `env_file`: `docker-compose.assess.yml` passes it the values the API reads from
`.env`, by interpolation. A value set only in `.env` therefore reaches the worker only if the
overlay names it. The settings that both processes read, and that must agree, are passed this
way: `VEKTRA_ASSESS_START_T_S`, `VEKTRA_ASSESS_ADMISSION_K`, `VEKTRA_ASSESS_RENDER_FRESH_S`,
`VEKTRA_ASSESS_REPLY_GRACE_S`, `VEKTRA_ASSESS_SITE_TIMEZONE`, the `VEKTRA_ASSESS_SMTP_*` relay
settings and `VEKTRA_ASSESS_OPERATOR_EMAIL` (the API needs the relay and the operator address for
the SMTP test send). To change a worker-only setting that the overlay does not name, add it to the
worker's `environment` in a local override file.

**Name the LMS provider.** The worker loads its LMS provider by name at startup
(`VEKTRA_ASSESS_LMS_PROVIDER`, ADR-0033); the overlay passes it from `.env`. Unset, the worker
refuses to start and lists the providers installed in the image.

**The image with the provider.** The platform image installs no LMS provider (ADR-0033 D6). The
overlay builds the variant with the Moodle one (`INSTALL_LMS_PROVIDER=moodle`, tagged
`vektra-stack-lms-moodle`) and runs every assess service from it; the released equivalent is
`ghcr.io/vektralabs/vektra:<version>-lms-moodle`. The variant installs the provider's released
wheel, pinned by version and sha256 in `docker/lms-providers/moodle.txt`: the build downloads it
from the provider's GitHub release (the build host needs to reach github.com) and fails if its
hash differs. The provider has its own settings,
`VEKTRA_LMS_MOODLE_*` ([configuration](../../reference/configuration.md#moodle-lms-provider-vektra-lms-moodle));
the overlay does not name them, so set the ones you need in the worker's `environment` of a local
override file.

**Set shared values in `.env`, not as shell exports.** Compose interpolation (`${VAR:-}` in the
overlay) takes a variable from the shell before `.env`, while the API container also reads `.env`
itself (`env_file`). For a setting the overlay passes to the worker only, such as
`VEKTRA_ASSESS_LLM_MODEL`, a shell export reaches the worker and not the API: the API then starts
without a model (refused) or with the `.env` model, and the two processes disagree. Put every
assess setting in `.env` and do not export a different value in the shell that runs
`docker compose`.

Settings a production deployment must set (each is refused or unset by default):

| Setting | Read by | Without it |
|---|---|---|
| `VEKTRA_ASSESS_ENABLED=true` | all | no route is mounted; the assess targets exit with code 2 |
| `VEKTRA_ASSESS_API_DATABASE_URL`, `VEKTRA_ASSESS_WORKER_DATABASE_URL` | API, worker | startup refused (set by the overlay) |
| `VEKTRA_ASSESS_LLM_MODEL` | API, worker | startup refused; no fallback model. Set it in `.env` (see above) so both processes use the same model |
| `VEKTRA_ASSESS_UI_ORIGIN` | API | an `https` origin distinct from every LMS origin, or the API refuses to start outside development |
| `VEKTRA_ASSESS_API_PUBLIC_ORIGIN` | API (setup check) | `ui_not_same_origin` cannot compare the two origins |
| `VEKTRA_ASSESS_SMTP_HOST`, `VEKTRA_ASSESS_SMTP_FROM`, `VEKTRA_ASSESS_OPERATOR_EMAIL` | worker (and API for the test send) | the worker refuses to start |
| `VEKTRA_ASSESS_RECIPIENT_DOMAINS` | worker | every transcript address is refused (`FAILED`, `domain_not_allowed`) |
| `VEKTRA_ASSESS_SAFEGUARD_MODE` | API | the default `presidio` is right; `none` disables the safeguard and is refused outside development unless `VEKTRA_ASSESS_PROVIDER_LOCALITY=onprem` (see [the safeguard and the model's locality](#the-safeguard-and-the-models-locality)) |
| `VEKTRA_ASSESS_PROVIDER_LOCALITY` | API | the default `cloud` is the safe one; declare `onprem` only for a model on the institution's hardware |
| `VEKTRA_ASSESS_ERASURE_MODE` | API | the API refuses to start outside development (OQ-033, no implicit choice) |
| `VEKTRA_ASSESS_DB_BACKUP_RETENTION_DAYS` | worker | a startup warning: the crypto-shredding scope is unknown |
| `VEKTRA_LMS_MOODLE_INGEST_ACCOUNT_USERNAME` | worker (setup check) | the `not_ingestion_account` item is `UNVERIFIABLE`, so the setup check is not green |

The model must also have a passing NFR-023 activation for its `prompt_version`
(`POST /api/v1/assess/admin/model-activations` with the report of `make eval-assess`). Without
one the API starts degraded and the pre-flight refuses every discussion. See the
[API reference](../../reference/api.md#assess-model-activation-nfr-023).

**A reasoning model needs its reasoning turned down, or a larger `VEKTRA_ASSESS_LLM_MAX_TOKENS`.**
The completion budget (default 600) covers the reasoning and the answer. A reasoning model that
spends it all on reasoning returns no text, and every Start is rolled back with
`provider_unavailable`; the worker log line `assess_preflight_provider_outage` then says
`outage_cause=max_tokens_exhausted` (a discussion turn logs `assess_turn_provider_failure` and is
refused, not an OUTAGE). Prefer lowering the reasoning: set `VEKTRA_ASSESS_LLM_EXTRA_BODY` to
`{"reasoning_effort": "low"}` (OpenAI reasoning models) or
`{"chat_template_kwargs": {"enable_thinking": false}}` (Qwen3 on vLLM). Raising the budget instead
lengthens the reasoning, which happens before the first token and counts against
`VEKTRA_ASSESS_OUTAGE_TIMEOUT_S`: see
[Reasoning models (assess)](../../reference/configuration.md#reasoning-models-assess).

Reading `outage_cause`: `http_5xx`, `connection_error` and the `*_timeout` codes of the HTTP
client (`read_timeout`, `connect_timeout`, ...) point at the provider or the network. A
`first_token_timeout` does too, **unless** the model reasons: after a `VEKTRA_ASSESS_LLM_MAX_TOKENS`
raise with reasoning still on, it usually means the reasoning outlasted the first-token deadline.
`auth_error` is the API key refused (401/403) or not configured at all (litellm then reports a
synthetic status 500, so `http_status` is `null`): check `VEKTRA_LLM_API_KEY` in the process
that logged it. `http_4xx` is the provider refusing the request (a wrong model, a rate limit, a
rejected parameter or content). Both are refusals. A 429 ends the turn `provider_refused` and
the session stays ACTIVE. An `auth_error` is the configuration; an `http_4xx` that is not a 429 is
the configuration only when a probe call shaped like a turn is refused too (otherwise it was that
request's, e.g. a content-policy 400: `provider_refused`, the session stays ACTIVE). For the
configuration the turn ends interrupted (`service_misconfigured`), the session pauses in
OUTAGE so the student's time is not counted until you fix it, a Start rolls back
`service_misconfigured`, and the operator gets an email (`provider_config_refused`, at most
once per site, cause and hour). The
outage probe logs them as `assess_outage_probe_failed` with `reason_code=provider_refused` and
leaves the outage open; a 429 is transient (`provider_unavailable`). Both the API and the worker
refuse to start when the model's provider needs an API key and their own environment has none
(see [Reasoning models (assess)](../../reference/configuration.md#reasoning-models-assess)).

## The assess schema: a migration branch of its own

The assess tables, the three `vektra_assess_*` roles and the `SECURITY DEFINER` functions are
one Alembic branch, `assess`, separate from the platform's `platform` branch (ADR-0033 D7;
`vektra-assess/src/vektra_assess/migrations/versions/a0001_assess_base.py`). The `server` and
`migrate` entrypoint targets always run `alembic upgrade platform@head`, and run
`alembic upgrade assess@head` only with `VEKTRA_ASSESS_ENABLED=true`. A deployment that never
enables assess gets none of its tables, roles or functions, and its migration user needs no
`CREATEROLE`. The API's startup check requires the assess branch at its head only when assess
is enabled; a deployment that turns assess off keeps its schema and still starts.

The entrypoint does not parse `VEKTRA_ASSESS_ENABLED` itself: it runs
`python -m vektra_app.assess_switch`, which prints the branches to migrate with the helper the
API uses for its routes and its schema step. The column `query_traces.pipeline` is the
platform's (revision `0009`), so analytics works with assess off.

Enabling assess later is the same: set `VEKTRA_ASSESS_ENABLED=true` and restart the API, whose
entrypoint then applies the branch. On managed Postgres, the migration user's requirements
(the three roles pre-created, membership of `vektra_assess_definer`) are those of the
CHANGELOG upgrade note and apply only from then on. A migration user with `CREATEROLE` but no superuser may
instead let the branch create the roles: it grants itself `vektra_assess_definer` (PostgreSQL
16 gives the creator only `ADMIN` on a new role).

**Development databases migrated by the old chain.** The 36 assess revisions `0009` to `0044` of the
root chain were squashed into the branch's base revision `a0001`, with neutral column names
(`lms_site_uuid`, `lms_origin`, `lms_release`). No released version ever shipped them, and no
assess data is migrated: a development database that holds them is re-created. Drop the
database (or its volume), then start the stack again; `alembic upgrade` refuses a database
recorded at `0044` (`Can't locate revision`), and the API refuses to start on it with the same
advice.

## Database roles and DSNs

The three roles are created NOLOGIN by the assess migration branch, or the two login roles by
`assess-roles.sql` itself if the branch has not run yet; `vektra-assess-db-init` then sets them `LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB
NOBYPASSRLS NOINHERIT` with the passwords from `assess_db_api_password` and
`assess_db_worker_password`. Grants are set by the migrations, not by this script.

The DSNs carry **no password**:

```text
VEKTRA_ASSESS_API_DATABASE_URL=postgresql+asyncpg://vektra_assess_api@postgres:5432/vektra
VEKTRA_ASSESS_WORKER_DATABASE_URL=postgresql+asyncpg://vektra_assess_worker@postgres:5432/vektra
```

Each process adds its own password from its secret file (`vektra_assess.db.roles.dsn_with_password`).
A DSN that carries a password, or an API DSN that logs in as the `VEKTRA_DATABASE_URL` user, is
refused at startup. To rotate a role password, replace its secret file and re-run the one-shot
service:

```bash
docker compose -f docker-compose.yml -f docker-compose.assess.yml --profile assess \
  run --rm vektra-assess-db-init
```

then recreate the service that holds that password (`... up -d --force-recreate vektra` or
`vektra-assess-worker`): Compose bind-mounts the secret file, and a restarted container keeps
the old file if the replacement wrote a new one.

## Row-level security for the API role

Assess serves several LMS sites from one database. Each route already filters on its site, and
the database enforces it as well: the assess revision `a0002`
(`vektra-assess/src/vektra_assess/migrations/versions/a0002_assess_api_rls.py`, ADR-0033 D9.2)
puts a row-level-security policy for `vektra_assess_api` on the 20 site-scoped tables that role
reads. The API sets `app.assess_site_id` at the start of every request transaction, from the
site its credential is bound to (the site in a student or download token, the site of an
`assess:*` key in `assess_site_keys`, or the site in an admin route's path once it has been
checked). A transaction of the API role reads and writes only that site's rows. With the setting
unset or empty, it reads **no rows** and can write none.

- **The worker is not restricted.** `vektra_assess_worker` serves every site by design, and
  `vektra_assess_definer` (the owner of the `SECURITY DEFINER` functions) has its own
  unconditional policy, so the definer functions work as before.
- **The migration user is not restricted.** It owns the tables, and the revision enables RLS
  without `FORCE`, so the migrations see every row.
- **Backups and reports need a role that sees every row.** Only a superuser, the tables' owner
  (the migration user) or a role with `BYPASSRLS` sees every row. For any other role:
  - **`pg_dump` fails.** Run by a role without these rights, it stops with
    `query would be affected by row-level security policy for table "assess_sessions"`.
    (`pg_dump` turns row security off for its session, and PostgreSQL refuses that for such a
    role.)
  - **`pg_dump --enable-row-security` is no workaround.** It completes, but dumps only the rows
    that role's policies show, which for a role with no policy is none. Do not use it for a
    backup.
  - **The recipe.** Dump as the migration user, or create a dedicated backup or report role:
    `CREATE ROLE vektra_backup LOGIN BYPASSRLS PASSWORD '...'`, with `SELECT` on the assess
    tables. The trade-off: `BYPASSRLS` turns off every policy for that role, so it reads every
    site's rows. Keep it read-only, give it to backup and reporting only, and never to an
    application process.
  - **The NFR report needs such a role too.** `scripts/assess_nfr_report.py` refuses a role
    that RLS restricts (exit 3, naming the tables), rather than reporting an empty campaign.
- **The cross-site reads go through definer functions.** A few API reads need no site or cover
  every site, and each is a `SECURITY DEFINER` function returning ids or counts:
  `assess_site_of_key` (resolving a key's site, before anything is bound),
  `assess_live_session_count` (the deployment-wide admission cap), `assess_configured_site_count`
  (`/health/assess`), `assess_pending_replies` (the API's pending-turn poller, which then binds
  each answer to its session's site), `assess_transcript_site` (an operator's transcript
  resend) and `assess_session_exists` (one boolean, so that the audit still records a cross-site
  session probe as an ownership mismatch; the HTTP answer is the same not-found envelope).
- **The assess roles must stay `NOBYPASSRLS`.** A role with `BYPASSRLS`, or a superuser, ignores every
  policy. `vektra-assess-db-init` creates the roles `NOBYPASSRLS`; keep it that way on managed
  Postgres too.
- **Connection poolers.** The setting is transaction-local (`set_config(..., true)`), so it works
  behind a pooler in transaction mode and never carries over to the next transaction on the same
  connection.

A route that forgot to bind its site gets an empty result, not an error: a 404 or an empty list.
The test suite treats that as a bug. Every route test runs under a guard that fails when a
statement reaches a site-scoped table before the site is bound
(`vektra-assess/tests/support/binding_guard.py`).

To check a deployment, as a superuser or the migration user:

```sql
-- 20 tables with RLS on and none forced
SELECT count(*) FILTER (WHERE relrowsecurity), count(*) FILTER (WHERE relforcerowsecurity)
FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relname LIKE 'assess\_%';

-- the two policies on each table: one for the API role, one for the worker and the definer
SELECT tablename, policyname, roles FROM pg_policies WHERE tablename LIKE 'assess\_%' ORDER BY 1, 2;

-- no assess role can bypass RLS
SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname LIKE 'vektra\_assess\_%';
```

And as `vektra_assess_api` (for example with `psql` and the role's password), unbound, then bound
to a site:

```sql
SELECT count(*) FROM assess_sessions;                       -- 0: nothing is bound
BEGIN;
SELECT set_config('app.assess_site_id', '<site uuid>', true);
SELECT count(DISTINCT site_id) FROM assess_sessions;        -- 1 at most: that site only
ROLLBACK;
```

The automated proof is `vektra-assess/tests/integration/test_api_rls.py`: it runs a route's
statement with its `site_id` predicate removed, as the API role bound to site A, and reads 0 rows
of site B.

## Connection budget

Postgres `max_connections` is 100; the shipped defaults use 65 and
`vektra-app/tests/test_connection_budget.py` fails above 85. The table, and how the worker splits
its 5 connections into a job pool of 4 and a liveness engine of 1, is in
[configuration: connection budget](../../reference/configuration.md#connection-budget-assess).
If you raise `VEKTRA_ASSESS_API_POOL_SIZE` or `VEKTRA_ASSESS_WORKER_POOL_SIZE`, recompute the
total against your server's `max_connections`.

## Postgres logging (`log_statement=none`)

The assess tables carry ciphertext, but a logged statement would carry its bind parameters, and
`ALTER ROLE ... PASSWORD` would carry a password. The base `docker-compose.yml` starts Postgres
with:

```text
log_statement=none
log_min_duration_statement=-1
log_parameter_max_length=0
log_parameter_max_length_on_error=0
log_min_error_statement=panic
```

If you run Postgres outside this Compose file, set the same values in `postgresql.conf`. The
setup check item `db_log_statement_none` reads `log_statement` and `log_parameter_max_length` as
the worker's session sees them, and the per-role and per-database overrides of the two assess
roles: it is `MISSING` unless every one is `none` / `0`, and `UNVERIFIABLE` when an override
hides the server value from the worker's session. The other three settings above are not checked
by it.

## Worker and parser limits

- The worker's job concurrency is per pool (`VEKTRA_ASSESS_POOL_<POOL>_CONCURRENCY`), and the
  Start admission number K is `VEKTRA_ASSESS_ADMISSION_K` (API) and the pre-flight pool
  (`VEKTRA_ASSESS_PREFLIGHT_CONCURRENCY`, worker). Both default to 10, a placeholder: K is not
  chosen in code and has not been measured on the target hardware (NFR-020, BLOCKED in the
  [go/no-go](assess-pilot-go-no-go.md)).
- The parser runs at most `VEKTRA_ASSESS_PARSER_POOL` children at once (default 3), each with a
  wall-clock timeout (`VEKTRA_ASSESS_PARSER_TIMEOUT_S`, 10), an address-space limit
  (`VEKTRA_ASSESS_PARSER_RLIMIT_AS_MB`, 256), an output cap (`VEKTRA_ASSESS_PARSER_MAX_CHARS`) and
  a file-size and page cap. The container limits above bound the whole sidecar.
- The parser refuses to start if its secrets directory is not empty or absent, or if any
  `VEKTRA_*` variable that looks like a DSN or a secret is set (`DATABASE_URL`, `DSN`,
  `PASSWORD`, `SECRET`, `TOKEN` in the name). Pass it only the non-secret limits.

## Health checks

| Process | Check | Healthy when |
|---|---|---|
| API | the image `HEALTHCHECK`: `curl http://localhost:8000/health` | the shallow health answers; it does not include assess |
| API, assess readiness | `GET /health/assess` (any valid Bearer token) | `200` with status `healthy` (or `disabled`); `503` otherwise, with `message` `no_heartbeat`, `heartbeat_stale`, `worker_not_ready`, `job_backlog`, `heartbeat_unreadable` or `keys_missing` |
| worker | Compose `healthcheck`: the mtime of `VEKTRA_ASSESS_WORKER_HEARTBEAT_FILE` (tmpfs) is less than 30 s old | the event loop is running |
| parser | Compose `healthcheck`: `python -m vektra_assess.parser_sidecar.ping --deep` | a child process parsed an embedded page under its rlimits |

`/health/assess` is the probe to put behind a load balancer for the assess routes: it is never
`healthy` without a fresh worker heartbeat row. Its `details` and the `vektra_assess_*` series of
`/metrics` are described in the [API reference](../../reference/api.md#get-healthcomponent);
example alert rules are in `deploy/prometheus/assess-alerts.yml.example`.

## Erasure alerts

An erasure request comes from the LMS's privacy API (OQ-033). The LMS cannot leave its own
request failed: its privacy manager catches whatever the plugin's provider raises, tells
the site's DPOs and completes the request anyway. So a request that does not complete on the
Vektra side would go unnoticed there, and Vektra emails the operator instead
(`operator_notify`, to `VEKTRA_ASSESS_OPERATOR_EMAIL` through the transcript relay):

| Alert | When | Sent |
|---|---|---|
| `erasure_verification_failed` | the worker's count after the deletion found rows left: the deletion was rolled back and the request recorded `failed` (`error_code` `verification_failed`) | once per request, in the transaction that records the failure (a run that finds the request already completed by another run records and sends nothing) |
| `erasure_overdue` | the request is still not completed `VEKTRA_ASSESS_ERASURE_ALERT_AFTER_S` (default 3600 s, at least 900 s) after it was made: its job is waiting for work in flight, retrying, or dead | once per request, from the worker's `delivery_retry` tick (every 30 s), marked on the request (`overdue_alerted_at`) in the same transaction, so pruned job rows never bring a second email. At send time it is dropped only if the request has since been **erased**; a request that ended `failed` is still reported |
| `erasure_abandoned` | the request is still open a day after it was made: the daily purge records it `failed` (`error_code` `abandoned`) and drops the ids it carried | once per request, in the purge's transaction; the request was already reported overdue |

The threshold is never below 900 s: an erasure waits up to 15 minutes for a transcript copy that
may be on the wire, and alerting sooner would report a request working as designed. It is at most
12 h, under the purge's one day.

The email and the job carry the site id, the request id (`assess_erasure_records.id`) and the
scope (`subject` or `activity`): never a user id, the subject's hash or the activity. The worker
also logs `assess.erasure.alert` with the same `alert` name; a failure of the overdue sweep
itself is logged `assess_erasure_overdue_sweep_failed` and does not stop the rest of the tick.
The alerts need the relay (`VEKTRA_ASSESS_SMTP_*`) and `VEKTRA_ASSESS_OPERATOR_EMAIL`, like every
operator notification.

What to do:

1. Find the request: `SELECT id, scope, requested_at, completed_at, outcome, error_code FROM
   assess_erasure_records WHERE id = <request id>`, and its job: `SELECT state, attempts,
   last_error_code FROM assess_jobs WHERE kind = 'erasure' AND (args->>'erasure_id')::bigint =
   <request id>`.
2. `erasure_overdue` with a live job: the erasure waits while a session of the scope is live
   or a transcript send is in flight (at most 15 minutes after the send started); check the
   worker's log and `GET /health/assess`. A dead job is queued again by the plugin's retry of
   the same request.
3. `erasure_verification_failed`: nothing was deleted. The worker's `assess.erasure.alert` line
   carries `erasure_id`, `erasure_scope` and `tables_left`, the rows still present per table
   (for example `turns=2,snapshots=1`; only names from the erasure's own table list, anything
   else counts as `other`). If the log line is gone, the request's own record keeps the same
   counts: `SELECT rows_deleted->'left' FROM assess_erasure_records WHERE id = <request id>`.
   Fix the cause, then have the DPO repeat the request from the LMS (a new request; the failed
   one stays recorded). A retention purge that fails its count logs `assess.purge.alert`
   `purge_verification_failed` with the same `tables_left`.
4. `erasure_abandoned`: nothing was deleted and the request will not run again. Fix what kept
   it from running (step 2), then have the DPO repeat the request from the LMS.

## TLS and the two public origins

The deployment has one backend and two public `https` origins on the reverse proxy:

- the API origin (`VEKTRA_ASSESS_API_PUBLIC_ORIGIN`): the plugin's server-side calls, admin and
  every other route, with `X-Frame-Options: DENY`;
- the UI origin (`VEKTRA_ASSESS_UI_ORIGIN`): the discussion page, its assets and the student
  routes the page calls, framed by the LMS.

TLS is terminated at the proxy (NFR-012). The proxy configuration, the distinct-origin rule and
how to verify it are in [framing](assess-framing.md). The worker's connections to the LMS are
`https` verified against the system store or `VEKTRA_LMS_MOODLE_CA_FILE`
(`VEKTRA_LMS_MOODLE_ALLOW_HTTP` is honoured only in development); its SMTP connections use
STARTTLS or implicit TLS with verified certificates (`VEKTRA_ASSESS_SMTP_TLS_MODE`,
`VEKTRA_ASSESS_SMTP_CA_FILE`).

## Register a site

No API route creates a site or binds a key to it: both are rows the operator inserts as the
database owner. The [service-account guide](../assess-moodle-service-account.md#register-the-site-and-mint-the-plugins-keys)
gives the statements, the WS token secret and the four plugin keys.

## Setup check walkthrough

The setup check is the final gate of a deployment, and of every LMS upgrade. A site can enable a
discussion only while its latest finished report for the current LMS release has every item `OK`.

1. Make sure the worker is healthy (`GET /health/assess` answers 200): the API refuses to queue a
   check without a fresh worker heartbeat (`503 ERR-ASSESS-001`).
2. Queue a check, with an `admin` key or the site's own `assess:teacher` key:

   ```bash
   curl -s -X POST -H "Authorization: Bearer $VEKTRA_ADMIN_KEY" \
     https://vektra.uni.example/api/v1/assess/sites/$SITE_ID/setup-check
   # {"report_id": "<uuid>"}
   ```

   One check per site every 5 minutes (`429 ERR-ASSESS-002`, `Retry-After`). The plugin's
   "Verifica configurazione" button calls the same route.
3. Poll the report until `status` is `done` or `failed`:

   ```bash
   curl -s -H "Authorization: Bearer $VEKTRA_ADMIN_KEY" \
     https://vektra.uni.example/api/v1/assess/sites/$SITE_ID/setup-check/$REPORT_ID
   ```

4. Read every item. Only `OK` counts: `MISSING` and `UNVERIFIABLE` both keep the feature off.
   The items, in report order:

   | Item | Fix when not `OK` |
   |---|---|
   | `token_works` | the WS token in `assess_ws_tokens` for this site id, the service, the network path |
   | `required_reads_quiz`, `required_reads_assign`, `required_reads_grade`, `required_reads_grading`, `required_reads_enrol`, `required_reads_files` | the service's function list and the role's capabilities ([service account](../assess-moodle-service-account.md)) |
   | `interactive_login_disabled` | the account's authentication method must be `webservice` |
   | `not_site_admin` | remove the account from the site administrators |
   | `not_ingestion_account` | use a different account than the ingestion one; set `VEKTRA_LMS_MOODLE_INGEST_ACCOUNT_USERNAME` |
   | `denied_function_probe` | the service must have "authorised users only" and only the listed functions |
   | `no_grade_write` | remove every grade-write function from the service |
   | `ws_tls_ok` | `https` to the LMS, with a certificate the worker's CA store accepts |
   | `ui_not_same_origin`, `ui_origin_distinct` | [framing](assess-framing.md) |
   | `parser_sidecar_running` | the parser container and the shared socket volume |
   | `smtp_probe` | the relay settings and the `assess_smtp` secret; `POST /api/v1/assess/ops/smtp-test` sends one test message |
   | `db_log_statement_none` | the Postgres logging settings above |
   | `privacy_config` | `PUT /api/v1/assess/sites/{site_id}/privacy` with the DPO's values (REQ-089) |

   Each item carries a content-free `reason_code` that names the failing condition.

   **Changing the privacy notice.** Any change to what the student is shown before Start
   (`retention_days`, `legal_basis_label`, `legal_basis_text`, `notice_url`, `notice_text`) needs a
   new `notice_version` in the same `PUT`: the same version with different content is refused
   (`400 ERR-ASSESS-028`, field `notice_version`, code `must_change`). The version is what a Start
   compares with the one the student saw, so a student who opened the page before the change is
   asked to reopen it (`409 ERR-ASSESS-029`) instead of starting under a notice they did not read.
5. Re-run the check after every fix, and after every LMS upgrade: a release change invalidates the
   stored capability flags and blocks Starts until a check passes.
