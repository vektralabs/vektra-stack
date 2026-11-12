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
| `assess_ws_tokens` | credentials (JSON object) | forbidden | required | forbidden | the Moodle provider's web-service tokens: only the worker reads the LMS. Not in `KEY_MATRIX`: the provider declares it (`required_secrets`, ADR-0033 D5) and the worker requires and loads it |
| `assess_smtp` | credentials (JSON object) | forbidden | required | forbidden | the relay account: only the worker sends mail |
| `assess_db_api_password` | password (one line) | required | forbidden | forbidden | the API's database role |
| `assess_db_worker_password` | password (one line) | forbidden | required | forbidden | the worker's database role |

Additional rules applied by `enforce_key_matrix`:

- The parser holds nothing: its secrets directory must be absent or empty, and no `VEKTRA_*`
  variable whose name contains `DATABASE_URL`, `DSN`, `PASSWORD`, `SECRET` or `TOKEN` may be set
  (`VEKTRA_ASSESS_SECRETS_DIR` is exempt: it is a path).
- Any `assess_*` file the matrix does not grant the role (the LMS provider's credentials, a
  per-site token file, whatever their name) is forbidden for the API and the parser: they load
  no provider, so they cannot know the names it declares, and a provider's secret names must
  therefore start with `assess_`.
- The API applies the forbidden column even with `VEKTRA_ASSESS_ENABLED=false`: an API container
  that holds the KEK or the LMS credentials is refused whether or not the feature is on.
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
remap the owner of a file secret, so once the credentials are filled run
`sudo chown -R 1000:1000 secrets/assess` (recursive: the files, not only the directory). From then
on, change a file only with [the replacement procedure](#change-a-credentials-file), which keeps
that owner and the mode.

Every secret except `assess_ws_tokens` is loaded when the process starts. `assess_ws_tokens` is
re-read on every LMS call, but that does not make a change safe without a restart: Compose
bind-mounts each secret file, and a replacement that writes a new file (an editor's save, `mv`,
`rm` and recreate) leaves the running container on the old one. After changing any secret,
recreate the service that holds it
(`docker compose -f docker-compose.yml -f docker-compose.assess.yml --profile assess up -d --force-recreate <service>`).

## Generate a set

```bash
make assess-keys
# = uv run python -m vektra_assess.keys generate --out ./secrets/assess --provider-secret assess_ws_tokens
```

It writes all eleven files with mode `0400`: random symmetric keys, matching X25519 pairs for the
hand-off and the teacher configuration, random role passwords, and `{}` for the two credentials
files. Every keyring gets the kid `k1`. It writes nothing if any of the files already exists
(all or nothing), and it prints file names only.

Then fill the two credentials files, with the procedure below.

### Change a credentials file

Use the same procedure for the first fill and for every later change (a new site's token, a
token rotation, a new relay account). It never edits a file in place: it writes the complete new
file next to the old one with its final mode (`0400`) and owner, then renames it over the old one.
A worker reading the file meanwhile sees either the old or the new file, never a half-written or
`0600` one (which the loader refuses: the worker would answer `no_token` without calling the
LMS). It **merges** into `assess_ws_tokens`: the other sites' tokens are kept. The values go
through `json.dump`, so a `"` or a `\` cannot break the file, and they are typed at prompts so
they stay out of the shell history.

```bash
# assess_ws_tokens: add or replace ONE site's token, keep the others
read -r SITE_ID; read -rs WS_TOKEN; export SITE_ID WS_TOKEN
sudo --preserve-env=SITE_ID,WS_TOKEN python3 - secrets/assess/assess_ws_tokens <<'PY'
import json, os, sys, tempfile
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
except FileNotFoundError:
    doc = {}
doc[os.environ["SITE_ID"]] = os.environ["WS_TOKEN"]
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".assess_ws_tokens.")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(doc, f)
os.chmod(tmp, 0o400)
if os.geteuid() == 0:
    os.chown(tmp, 1000, 1000)   # the containers' uid (Linux engines)
os.replace(tmp, path)
PY
unset WS_TOKEN
```

For `assess_smtp` the same script writes `{"username": ..., "password": ...}` as a whole (read
`SMTP_USER` and `SMTP_PASSWORD` the same way, and replace the `doc` lines with
`doc = {"username": os.environ["SMTP_USER"], "password": os.environ["SMTP_PASSWORD"]}`; `{}` for
a relay without AUTH).

- On Docker Desktop, where the files belong to you, drop the `sudo --preserve-env=...` prefix.
- On a Linux engine run it as root, as shown: after `chown -R 1000:1000 secrets/assess` the
  files are no longer yours to read or write.
- The rename gives the file a new inode, so the running container still sees the old one through
  its bind mount: recreate the service that holds the secret afterwards (below), which for these
  two files is the worker.

Keep `secrets/` out of version control (it is gitignored) and out of every database dump.

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
| `<name>: mode <mode> is too open (at most 0440)` | any permission bit outside `0440`: owner-writable (`0600`), group-writable, or any bit for others |
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
