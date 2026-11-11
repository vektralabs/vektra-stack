# Wave W4 review: teacher, privacy, release (assessment discussion bot)

Date: 2026-11-11. Gate run on `develop` at `87f22d0a` (merge of PR #196). PRs: #170 and #179 (teacher ops,
split 11a config and 11b list/reset/terminate/extra time), #190 (teacher and operator notifications), #191 and
#193 (privacy and retention, split 12a and 12b), #195 (13a chaos, NFR report, row budget, keys rewrap), #197
(the `rotate_keys` job kind dropped, revision 0044), #196 (13b runbooks and go/no-go), and the CI fixes #189,
#192, #194. Plugin side (vektra-moodle-discuss, owned by the other session): P3 has not started.

## Plans

| Plan | Result | Review |
|------|--------|--------|
| assess-teacher-ops (21 tasks) | completed, PR #170 (11a) + PR #179 (11b) | `/code-review` high on 11a and 11b, findings fixed (plan Notes). CodeRabbit #170 4 rounds (15 actionable), #179 5 rounds (19 actionable) |
| assess-privacy-retention (27) | completed, PR #191 (12a) + PR #193 (12b); notifications PR #190 | `/code-review` high on 12a (10 findings) and on 12b (10 findings), all fixed with a failing-first test. CodeRabbit #191 4 rounds (16 actionable + 2 outside the diff), #193 3 rounds (11), #190 2 rounds (9) |
| assess-hardening-release (24) | **in_progress**: 13a PR #195, fix PR #197, 13b PR #196, all merged | `/code-review` high on 13a (8 findings, all fixed). CodeRabbit #195 1 round (1), #197 1 round (0), #196 3 rounds (34 actionable in the two review summaries, 66 CodeRabbit comments counting replies). It stays `in_progress`: its acceptance criteria are BLOCKED on the target environment |
| CI fixes | #189 (canary matcher), #192 + #194 (HF model cache) | CodeRabbit #189 1 round (1), #192 1 round (2), #194 none |
| P3 discuss-teacher-privacy (plugin) | **pending**, not started | Read-only from vektra-moodle-discuss `origin/develop` `4cee292`: plan status `active`, 0 of 37 checkboxes ticked, no branch and no PR for P3; the plan file was last changed on 2026-11-01. P2b is still draft PR #3 |

Rounds count the CodeRabbit reviews with a summary on distinct head commits (`gh api .../pulls/N/reviews`).

## Gate

| Gate item | Result | Evidence |
|-----------|--------|----------|
| Purge and erasure leave 0 rows for the subject, other subjects unchanged (counts) | PASS | `vektra-assess/tests/integration/test_privacy_retention.py`: `::test_the_purge_removes_every_content_row_and_keeps_the_control` (every content table of the expired session at 0, the control session's counts unchanged), `::test_crypto_shredding_leaves_no_key_for_a_saved_ciphertext`, `::test_erasure_removes_the_subject_and_only_the_subject` (`subject_counts` all 0 after, `content_counts` of the control student equal to before, queued jobs left only for the control), `::test_activity_listing_and_erasure_stay_inside_the_activity`, `::test_retain_mode_deletes_nothing_and_says_so`, `::test_a_generation_racing_the_purge_inserts_nothing`. Whole file 21/21 on this machine. The chaos suite has no purge or erasure scenario; `tests/chaos/assess/test_invariants.py::test_a_purged_session_is_exempt_from_the_content_invariants` only keeps the invariants consistent with a purge |
| The API role cannot SELECT sealed teacher config | PASS | Against the Postgres catalog: `test_grant_matrix.py::test_api_role_is_denied_with_sqlstate_42501` (`SELECT sealed_blob`, `refs`, `kid`, `*` from `assess_activity_config_sealed` and `DELETE` all fail with 42501) and `::test_matrix_matches_the_catalog_both_ways`; `test_teacher_config_schema.py` (7, including a predicate on `sealed_blob` counted as a read); `test_teacher_config_api.py` (the API writes the config and cannot read it back). Static: `vektra-assess/tests/test_grants.py::test_api_cannot_read_the_sealed_teacher_config` |
| Privacy API export and delete end to end; an unhandled delete never succeeds silently | Vektra side PASS, plugin side NOT BUILT, joined run **BLOCKED** | Vektra: `test_privacy_retention.py::test_export_returns_the_exact_bytes_whatever_the_enrolment`, `::test_an_unfinished_erasure_is_a_503_never_a_success` (no worker: 503 `ERR-ASSESS-027` `reason: pending` with `Retry-After`, and the retry does not queue a second row), `::test_two_identical_erasure_requests_queue_one`, `::test_privacy_routes_take_only_their_own_site_bound_key`, `::test_settings_are_written_by_the_operator_and_gate_everything`; `vektra-assess/tests/test_privacy_unit.py`. The plugin's Moodle Privacy API provider (export, `delete_data_for_user`, the handling of a 503 so that Moodle's delete does not report success) is P3 and does not exist. A run from Moodle's privacy tool through the plugin to Vektra needs P3 and the F1 stack |
| Download proxy streams with attachment + nosniff + CSP sandbox + no-store | Vektra-served headers PASS, plugin proxy NOT BUILT, joined run **BLOCKED** | `test_transcript_api.py::test_the_owner_downloads_the_exact_bytes_once` asserts `Content-Disposition: attachment; filename=...`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: sandbox; default-src 'none'` and `Cache-Control: no-store` on the download, and `no-store` on the token mint; `::test_a_grader_downloads_the_same_bytes_and_a_non_grader_is_refused`. Whole file 12/12. The grader download proxy in the plugin (it must stream those bytes and headers unchanged) is P3 and does not exist |
| The pilot go/no-go criteria in plan 13 | **NO-GO** | `docs/guides/operators/assess-pilot-go-no-go.md`, 19 criteria (0-18): 2 PASS (0 production worker starts, 7 RAG OFF in code), 0 FAIL, 12 BLOCKED (1, 2, 3, 6, 8, 10, 12, 13, 14, 16, 17, 18), 2 OPEN (4 DPO values, 5 OQ-029 confirmation), 3 mixed (9 BLOCKED / NOT BUILT, 11 NOT BUILT / BLOCKED, 15 OPEN / BLOCKED). No sign-off. The code release blocker (criterion 0) is closed by #197 |

The named gate tests above, run together on this machine (`uv run pytest ... -m "integration or not integration"`):
86 passed, 0 skipped. `vektra-app/tests/test_assess_processes.py` is in that run (criterion 0: a fully keyed
production worker composes without the development override, every slot bound).

## Test totals

On `develop` `87f22d0a`, this machine:
- `make lint`: clean (ruff check, ruff format 722 files, mypy 295 source files, 21 import contracts kept, 0 broken).
- `make test`: 4057 passed, 78 skipped, 867 deselected.
- `uv run pytest vektra-assess/tests vektra-app/tests -m integration` against Postgres (testcontainers): 737 passed,
  84 skipped. All 84 skips are the live-Moodle subset (`test_moodle_live` 48, `test_moodle_setup_live` 18,
  `test_context_live` 12, `test_req073_live` 4, `test_idor_live` 2), as in CI; it runs in
  `assess-moodle-contract.yml` against the dev sites and was not run for this gate.
- `make test-chaos-assess`: 71 passed, gate PASS (71 cases declared, 49 invariant reports for the 27 required,
  0 violations).

CI on the merge commit `87f22d0a`: Lint run 42986039283 success, Unit tests run 42986039209 success (all package
jobs and the CI gate), CodeQL 42986038767 success. Integration tests run 42986039341 success: Assess chaos, Assess UI e2e, Integration tests (qdrant), Integration
tests (pgvector), Package integration tests (all packages), Assess stack, and Integration tests + NFR gates, all
green.

## "Working at the end"

Not yet, end to end. On the Vektra side the feature is code complete for the pilot scope: teacher configuration
(write-only, sealed), list, reset, terminate and extra time; retention purge with crypto-shredding, subject and
activity erasure, export; health, metrics, alert rules and operator mail; the chaos suite and the NFR report
script; the runbooks, reference docs, release checklist and go/no-go document. A production worker starts.

No pilot run has happened, and none can yet. It needs:
- the target VM (or HPC) for the NFR campaign, the soak, the burst and the restore drill;
- the reference model (OQ-026) and an eval-suite owner (OQ-027);
- a production-like relay for NFR-021 and the REQ-080 grep;
- plugin P3 (teacher pages, settings section, download proxy, Privacy API provider), not started;
- the F1 stack (https UI origin, `moodle_origin`) for any joined plugin + Vektra run.

## Deliberately open

- **BLOCKED NFRs**: NFR-018 latency, NFR-019 24 h soak (the 1-minute probe is also not built), NFR-020 K and
  hardware (OQ-032), NFR-021 transcript timing. All on the target environment; the commands are in the go/no-go
  document.
- **NFR-014 pen matrix not built**: `tests/pen/assess/` does not exist; it also needs live LMS 4.3/5.2 with
  login-as (OQ-031: login-as is accepted residual risk).
- **NFR-015 / NFR-017** canary suites to re-run on the target model and provider (OQ-026).
- **NFR-016** manual NVDA / VoiceOver pass: needs a person with a screen reader and the plugin pages.
- **REQ-080** canary grep of the target logs, mail log and `/metrics` (`tests/nfr/assess_canary_grep.sh`): needs a
  deployment with an LMS site, an activated model and a test relay.
- **Restore drill** (ARCH-079): no clean host; the record table in the go/no-go document is empty.
- **DPO values OQ-020 / OQ-033**: retention days, legal basis, notice, and `VEKTRA_ASSESS_ERASURE_MODE`.
- **REQ-087** continuity deferred by the product owner (2026-11-08). **REQ-092** out of v1, its flag refused at
  startup.
- Code gaps:
  - no command wraps `reseal_teacher_configs` or `reseal_handoffs` (the functions have no caller), so the
    teacher-config private kid cannot be retired and a hand-off kid only by waiting;
  - no route registers a site: nothing in `vektra-assess/src` inserts into `assess_sites`;
  - single API process only (`docs/guides/operators/assess-deployment.md`: the event stream broker and the DEK
    cache of live sessions are per process);
  - `VEKTRA_ASSESS_SAFEGUARD_MODE=none` is accepted in production (`config.py`, `Literal["presidio", "none"]`, no
    environment check);
  - `chaos-assess.yml` and `assess-moodle-contract.yml` are not on `main` (`git ls-tree origin/main
    .github/workflows/`), so their `schedule` and `workflow_dispatch` triggers never run: no nightly chaos, no
    scheduled live contract run.

## Findings worth knowing

- **The HF reranker made the startup gates measure the network** (#192, #194). The API downloads the embedding
  model and the 2 GB `BAAI/bge-reranker-v2-m3` at startup; in CI `provider_registration` took 34.8 s against
  15 s locally, so NFR-004 and the 120 s assess readiness wait failed at random. #192 caches the models; #194
  fixed the seed step, which had never run on a cache miss until #193 changed the cache key (`cp -a` as the image
  user could not set times on the runner's directory). A cache step is untested until its key changes.
- **The canary matcher had a false positive** (#189): a numeric hidden-mark canary (`7.31411`) matched inside a
  latency float in the query trace (`17.3141123`). Not a leak; numeric canaries now match only as whole numbers,
  with a test that fails on the old matcher.
- **The production worker did not start, and only writing the runbook found it** (#197). `rotate_keys` had a slot
  but no handler, and `assert_all_bound` fails closed; no test composed a production worker without
  `VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND`. The fix
  removed the job kind (revision 0044) and `test_assess_processes.py` now composes a fully keyed production worker
  without the override. A dormant slot is a startup failure waiting for production.
- **CodeRabbit on a docs PR finds real code/doc mismatches**, over several rounds (#196: 3 rounds, about 50
  comments). One was code: the nginx example proxied the whole `/transcripts/` prefix on the UI origin, so the
  token mint and resend POSTs were reachable there; fixed to GET `/transcripts/{tid}` only in `1be4292c`.
- **The `_locations` nginx parser broke on nested blocks**: in
  `vektra-shared/tests/test_image_content_boundary.py` it stopped at the first `}`, which was the nested
  `limit_except GET` block of the new location. It now balances braces (`3c4a84a2`).
