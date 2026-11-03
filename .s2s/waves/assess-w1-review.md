# Wave W1 review: foundations (assessment discussion bot)

Date: 2026-11-02. Branches: `feat/assess-w1` (vektra-stack, 60+ commits), `feat/assess-w1` and
`fix/release-zip-docker` (vektra-moodle), `feat/discuss-w1` (vektra-moodle-discuss).

## Plans

| Plan | Result | Review |
|------|--------|--------|
| assess-foundations (40 tasks) | completed | `/code-review` high: 10 findings, 8 fixed, 2 documented |
| assess-data-crypto (38) | completed | 10 findings, 9 fixed, 1 documented (coverage omit for DB-only modules) |
| assess-moodle-ws (43) | completed | 9 findings, 8 fixed, 1 documented (double reader calls) |
| assess-parser (26) | completed | 10 findings, all fixed; one flaky container test diagnosed as a test bug |
| discuss-foundation (19, plugin repo) | completed with 3 explicit deferrals | 10 findings, all fixed |

## Gate

| Gate item | Evidence |
|-----------|----------|
| import-linter contracts | 14 kept, 0 broken (`make lint`: ruff, mypy strict, import-linter) |
| `VEKTRA_ASSESS_ENABLED=false` mounts no assess route; workers refuse | route-table test (vektra-app), entrypoint tests, API refuses to start with `assess_ws_tokens` mounted (container run) |
| BUG-027 qemu baseline with `cryptography` | local PASS recorded in plan 1; the CI qemu step also parses a PDF now. NOT re-run locally (CI only) |
| grant matrix vs `pg_catalog`, hand-off row deleted on every CLOSED path | integration tests on real roles; latest-grants-revision test; row counts |
| readers on 4.3 and 5.2 incl. the five spike traps; setup check rejects negative accounts | 68 live `-m moodle` tests (34 per version) against the seeded dev Moodles, run by the loop |
| malicious-file corpus contained by the sidecar | 55-file corpus: every item ends in exactly one status, parent survives; 10 container tests on a rebuilt image |
| plugin installs on 4.3/PHP 8.2 and 5.2/PHP 8.4, release zip has no dev artefacts | Moodle upgrade/uninstall CLI on both live Moodles; PHPUnit 82 tests on both; zip: 0 forbidden entries (was 29). PHP 8.4 / CI not run locally |

Test totals at the gate (this machine): vektra-assess unit 881 passed (coverage 92%); other packages unit all green
(shared 226, admin 115, core 203, ingest 130, index 117, analytics 27, learn 85, app 75); `tests/assess` against a
freshly rebuilt isolated stack 20 passed; package integration: see the run recorded in the PR.

## Deliberately open (not blockers of W1)

- Plugin F3 and the live `assess:mint` 403/2xx pair: no mint endpoint until plan assess-session-lifecycle (W3). The equivalent exact-scope property is asserted on the setup-check route.
- GitHub Actions workflows (`assess-moodle-contract.yml`, plugin CI, release tag assertion) have never run: first PR run is the evidence. Behat, phpdoc and PHP 8.4 not run locally.
- Worker handler of the setup check and the daily tick: carried to plan assess-job-loop (W2).
- Private findings document is written but uncommitted in `vektra-internal` (unrelated uncommitted changes on another branch there).
- Requirements OQ-025/OQ-031 text update: follow-up after the findings are reviewed.
- Moodle review HTML math/image fidelity not measured; grader email source (REQ-082) undecided.

## Findings worth knowing

- Moodle returns a "no access" warning with HTTP 200 for `mod_assign_get_user_flags` with the minimal role (the spike had it readable); extensions come from the submission status.
- Postgres in the compose file could not start at one point (a `-c` was missing for `log_min_error_statement`), found by the parser agent on a real stack; fixed.
- A socket volume created by an older image keeps mode 0755 and the parser refuses it: on upgrade remove `assess_parser_sock`.
- The 5.1 default `MOODLE_TGZ_SHA256` in vektra-moodle's Dockerfile pins a moving tarball and fails today (not from this wave).
- `.s2s/BACKLOG.md` contains the university project name in a public repo (pre-existing).
- The parser container's memory peak under the worst-case flood is ~709 MiB of 768 MiB (8% margin): `VEKTRA_ASSESS_PARSER_RLIMIT_AS_MB` is the lever.
