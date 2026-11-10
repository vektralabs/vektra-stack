# Assess: secrets and the key matrix

Every secret of the assessment discussion feature is a file mounted by Docker under
`/run/secrets/` (`VEKTRA_ASSESS_SECRETS_DIR`). None is ever an environment variable, a database
row or a line in `.env`. This guide lists the secrets, which process must and must not hold each
one (ARCH-071), how to generate and fill them, and what each process prints when the rule is
broken. Backup, the restore drill and rotation are in the
[keys runbook](../assess-keys-runbook.md).

The source of truth is `KEY_MATRIX` in `vektra-assess/src/vektra_assess/keys.py`.
`docker-compose.assess.yml` mounts exactly what it allows, and
`vektra-app/tests/test_compose_assess_profile.py` checks the rendered Compose configuration against it.

## The key matrix

`required`: the process refuses to start without it. `forbidden`: the process refuses to start
if it is mounted **or** if an environment variable carries its name. `allowed`: mounted or not,
both accepted.

| Secret | Kind | API (`vektra`) | Worker | Parser | Why |
|---|---|---|---|---|---|
| `assess_token_signing` | 32-byte symmetric | required | forbidden | forbidden | the API signs and verifies the discussion and download tokens; no other process issues tokens |
| `assess_handoff_private` | X25519 private | required | forbidden | forbidden | opens the DEK of a LIVE session, so the API can serve turns without the worker; the worker never holds it |
| `assess_handoff_public` | X25519 public | allowed | required | forbidden | the worker seals each live session's DEK to the API |
| `assess_transcript_kek` | 32-byte symmetric | required | required | forbidden | the worker wraps transcript DEKs; the API opens transcripts for download and writes the teachers' reset and termination reasons |
| `assess_teacher_config_public` | X25519 public | required | allowed | forbidden | the API seals the solution, the pasted rubric and the recipients; it can write them, never read them |
| `assess_teacher_config_private` | X25519 private | forbidden | required | forbidden | only the worker opens the sealed teacher configuration |
| `assess_kek` | 32-byte symmetric | forbidden | required | forbidden | wraps every session DEK (snapshot and turns); the API has no path to it |
| `assess_ws_tokens` | credentials (JSON object) | forbidden | required | forbidden | the LMS web-service tokens: only the worker reads the LMS |
| `assess_smtp` | credentials (JSON object) | forbidden | required | forbidden | the relay account: only the worker sends mail |
| `assess_db_api_password` | password (one line) | required | forbidden | forbidden | the API's database role |
| `assess_db_worker_password` | password (one line) | forbidden | required | forbidden | the worker's database role |

Additional rules applied by `enforce_key_matrix`:

- The parser holds nothing: its secrets directory must be absent or empty, and no `VEKTRA_*`
  variable whose name contains `DATABASE_URL`, `DSN`, `PASSWORD`, `SECRET` or `TOKEN` may be set
  (`VEKTRA_ASSESS_SECRETS_DIR` is exempt: it is a path).
- Any file named `assess_ws_token*` other than `assess_ws_tokens` (a per-site token file) is
  forbidden for the API and the parser.
- The API applies the forbidden column even with `VEKTRA_ASSESS_ENABLED=false`: an API container
  that holds the KEK or the WS tokens is refused whether or not the feature is on.
- An environment variable "carries" a secret when its name, upper-cased, is exactly the secret
  name, `VEKTRA_` plus it, or either of those with `_FILE` appended (for example
  `ASSESS_KEK`, `VEKTRA_ASSESS_KEK`, `ASSESS_KEK_FILE`). Substrings do not count.
- The one-shot `vektra-assess-db-init` service holds only the two role passwords
  (`DB_INIT_SECRETS`).

There is no subject-key secret: the subject reference of a tombstone or an erasure is an unkeyed
`sha256(site|userid)`.

## File formats

| Kind | Content |
|---|---|
| symmetric, X25519 | a JSON keyring: `{"active": "<kid>", "keys": {"<kid>": "<base64 of exactly 32 bytes>"}}`. `active` must be one of the kids; the kid `purged` is reserved and refused |
| credentials | a JSON object. `assess_ws_tokens`: `{"<site id>": "<token>", ...}`, keyed by the `assess_sites.id` of each site. `assess_smtp`: `{"username": "...", "password": "..."}`, or `{}` for a relay that takes no AUTH; one of the two alone is refused |
| password | one line of at least 24 characters with no whitespace (a trailing newline is ignored) |

Every file must be a regular file (a symlink is refused), owned by the process uid or by root,
with mode `0440` or stricter. The containers run as uid 1000; on a Linux engine Compose does not
remap the owner of a file secret, so `chown` the directory to `1000:1000` (see the keys runbook).

`assess_ws_tokens` is re-read on every LMS call, so a token change needs no restart. Every other
secret is loaded when the process starts: restart the process after changing it.

## Generate a set

```bash
make assess-keys
# = uv run python -m vektra_assess.keys generate --out ./secrets/assess
```

It writes all eleven files with mode `0400`: random symmetric keys, matching X25519 pairs for the
hand-off and the teacher configuration, random role passwords, and `{}` for the two credentials
files. Every keyring gets the kid `k1`. It writes nothing if any of the files already exists
(all or nothing), and it prints file names only.

Then fill the two credentials files:

```bash
# one entry per registered site; the key is the site's assess_sites.id
printf '{"%s": "%s"}' "$SITE_ID" "$WS_TOKEN" > secrets/assess/assess_ws_tokens
printf '{"username": "%s", "password": "%s"}' "$SMTP_USER" "$SMTP_PASSWORD" > secrets/assess/assess_smtp
chmod 0400 secrets/assess/assess_ws_tokens secrets/assess/assess_smtp
```

The generated files are mode `0400`, so remove or `chmod u+w` them before overwriting. Keep
`secrets/` out of version control (it is gitignored) and out of every database dump.

## Startup refusals

A violated rule stops the process at start, before it serves or claims anything, with a structured
`StartupValidationError`. The step is `assess_check` (API), `assess_worker_check` (worker) or
`assess_parser_check` (parser). The detail joins every problem found with `; `, so one start
reports all of them:

```text
[STARTUP ERROR] Step: assess_worker_check
  Detail: assess_kek: required for worker but missing in /run/secrets; assess_token_signing: forbidden for worker but mounted at /run/secrets/assess_token_signing
  Remediation: Mount exactly the secrets the worker role needs (KEY_MATRIX in vektra_assess/keys.py, docs/reference/configuration.md): required keys present, mode <= 0440, forbidden keys neither mounted nor in env.
```

The messages, with `<name>` the secret and `<role>` the process:

| Message | Cause |
|---|---|
| `<name>: required for <role> but missing in <dir>` | a required file is not mounted |
| `<name>: forbidden for <role> but mounted at <path>` | a forbidden file is mounted |
| `<name>: forbidden for <role> but carried by env var(s) [...]` | an environment variable carries a forbidden secret's name |
| `<file>: forbidden for <role> (WS tokens belong to the worker) but mounted at <path>` | a per-site WS token file in the API or the parser |
| `<name>: not a regular file (symlinks are refused)` | a symlink or a directory |
| `<name>: owned by uid <n>, not by this process or root` | wrong owner (see the `chown` above) |
| `<name>: mode <mode> is too open (at most 0440)` | group-writable or world-readable file |
| `<name>: not valid JSON` / `not valid UTF-8` / `expected a JSON object` | a malformed file |
| `<name>: expected {'active': kid, 'keys': {kid: base64}}` | a keyring without `active` or `keys` |
| `<name>: active kid '<kid>' is not in keys` | `active` names a kid the file does not hold |
| `<name>: kid 'purged' is reserved` | the reserved kid is used |
| `<name>: kid '<kid>' is not valid base64` / `is <n> bytes, expected 32` | a damaged key |
| `<name>: must be one line of at least 24 non-whitespace characters` | a weak or multi-line role password |
| `parser: secrets dir <dir> must be absent or empty` | something is mounted into the parser |
| `parser: env var <name> must not be set (no DSN or secret)` | a DSN- or secret-like variable in the parser |

Other startup refusals that concern secrets:

- worker: `assess_smtp must hold both 'username' and 'password' (strings) or neither`;
- API and worker: a DSN that carries a password, or an API DSN that logs in as the main
  `VEKTRA_DATABASE_URL` user;
- `python -m vektra_assess.keys verify` and `... rewrap` run the same matrix check and exit with
  code 2, printing `cannot run: ...`, when it fails.

Key bytes are never printed, logged or shown in a `repr`; the `verify` command prints only a
fingerprint (`sha256(key)[:12]`) per kid.
