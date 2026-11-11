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
which had no handler and blocked every production start, was removed (revision `0044`; key
rotation is the `python -m vektra_assess.keys rewrap` command). The guard test
`vektra-app/tests/test_assess_processes.py` composes a fully keyed production worker without the
override and requires every slot bound. See the [pilot go/no-go](assess-pilot-go-no-go.md).

## Processes

The feature adds three containers to the core stack and four secrets-and-settings blocks to the
API. They are defined in `docker-compose.assess.yml` (an overlay, because the API service `vektra`
is always on and Compose has no optional secrets) and gated by the `assess` profile.

| Service | `CMD_TARGET` | What it does | Database role | Network | Limits |
|---|---|---|---|---|---|
| `vektra` (API) | `server` | runs `alembic upgrade head`, then serves `/api/v1/assess` (only with `VEKTRA_ASSESS_ENABLED=true`) | `vektra_assess_api` through `VEKTRA_ASSESS_API_DATABASE_URL`, next to the main `VEKTRA_DATABASE_URL` | default | `memory: 3G` (base file) |
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

`make assess-keys` runs `python -m vektra_assess.keys generate --out ./secrets/assess`. It writes
every secret of the key matrix with kid `k1`; `assess_ws_tokens` and `assess_smtp` are written as
empty JSON objects for you to fill (see [secrets](assess-secrets.md)). Set
`VEKTRA_ASSESS_SECRETS_PATH` to mount the secrets from another host directory.

For development and CI only, add `-f docker-compose.assess.dev.yml`: it sets
`VEKTRA_ENV=development`, `VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND=true`,
`VEKTRA_ASSESS_LLM_ALLOW_UNACTIVATED=true`, `VEKTRA_ASSESS_PRIVACY_ALLOW_UNSET=true` and
`VEKTRA_ASSESS_SAFEGUARD_MODE=none`. The three boolean overrides are refused
outside development. **`VEKTRA_ASSESS_SAFEGUARD_MODE=none` is not**: the
API accepts it in any environment and runs the discussion with a passthrough safeguard, so a
production `.env` must not carry it (the default is `presidio`, which refuses to start when
Presidio cannot load).

## Settings the API and the worker must share

The worker has no `env_file`: `docker-compose.assess.yml` passes it the values the API reads from
`.env`, by interpolation. A value set only in `.env` therefore reaches the worker only if the
overlay names it. The settings that both processes read, and that must agree, are passed this
way: `VEKTRA_ASSESS_START_T_S`, `VEKTRA_ASSESS_ADMISSION_K`, `VEKTRA_ASSESS_RENDER_FRESH_S`,
`VEKTRA_ASSESS_REPLY_GRACE_S`, `VEKTRA_ASSESS_SITE_TIMEZONE`, the `VEKTRA_ASSESS_SMTP_*` relay
settings and `VEKTRA_ASSESS_OPERATOR_EMAIL` (the API needs the relay and the operator address for
the SMTP test send). To change a worker-only setting that the overlay does not name, add it to the
worker's `environment` in a local override file.

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
| `VEKTRA_ASSESS_SAFEGUARD_MODE` | API | the default `presidio` is right; `none` is accepted and disables the safeguard (see above) |
| `VEKTRA_ASSESS_ERASURE_MODE` | API | the API refuses to start outside development (OQ-033, no implicit choice) |
| `VEKTRA_ASSESS_DB_BACKUP_RETENTION_DAYS` | worker | a startup warning: the crypto-shredding scope is unknown |
| `VEKTRA_ASSESS_INGEST_ACCOUNT_USERNAME` | worker (setup check) | the `not_ingestion_account` item is `UNVERIFIABLE`, so the setup check is not green |

The model must also have a passing NFR-023 activation for its `prompt_version`
(`POST /api/v1/assess/admin/model-activations` with the report of `make eval-assess`). Without
one the API starts degraded and the pre-flight refuses every discussion. See the
[API reference](../../reference/api.md#assess-model-activation-nfr-023).

## Database roles and DSNs

The two roles are created NOLOGIN by `deploy/postgres/init.sql` or migration `0009`, or by
`assess-roles.sql` itself if neither has run yet; `vektra-assess-db-init` then sets them `LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB
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

## TLS and the two public origins

The deployment has one backend and two public `https` origins on the reverse proxy:

- the API origin (`VEKTRA_ASSESS_API_PUBLIC_ORIGIN`): the plugin's server-side calls, admin and
  every other route, with `X-Frame-Options: DENY`;
- the UI origin (`VEKTRA_ASSESS_UI_ORIGIN`): the discussion page, its assets and the student
  routes the page calls, framed by the LMS.

TLS is terminated at the proxy (NFR-012). The proxy configuration, the distinct-origin rule and
how to verify it are in [framing](assess-framing.md). The worker's connections to the LMS are
`https` verified against the system store or `VEKTRA_ASSESS_MOODLE_CA_FILE`
(`VEKTRA_ASSESS_MOODLE_ALLOW_HTTP` is honoured only in development); its SMTP connections use
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
   | `not_ingestion_account` | use a different account than the ingestion one; set `VEKTRA_ASSESS_INGEST_ACCOUNT_USERNAME` |
   | `denied_function_probe` | the service must have "authorised users only" and only the listed functions |
   | `no_grade_write` | remove every grade-write function from the service |
   | `ws_tls_ok` | `https` to the LMS, with a certificate the worker's CA store accepts |
   | `ui_not_same_origin`, `ui_origin_distinct` | [framing](assess-framing.md) |
   | `parser_sidecar_running` | the parser container and the shared socket volume |
   | `smtp_probe` | the relay settings and the `assess_smtp` secret; `POST /api/v1/assess/ops/smtp-test` sends one test message |
   | `db_log_statement_none` | the Postgres logging settings above |
   | `privacy_config` | `PUT /api/v1/assess/sites/{site_id}/privacy` with the DPO's values (REQ-089) |

   Each item carries a content-free `reason_code` that names the failing condition.
5. Re-run the check after every fix, and after every LMS upgrade: a release change invalidates the
   stored capability flags and blocks Starts until a check passes.
