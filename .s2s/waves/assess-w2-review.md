# Wave W2 review: engine (assessment discussion bot)

Date: 2026-11-06. Gate run on `develop` at `57d1fab` (merge of PR #151). PRs: #148 (job loop),
#149 (context), #150 and #151 (LLM runner, split in two to stay under the CodeRabbit file limit).

## Plans

| Plan | Result | Review |
|------|--------|--------|
| assess-job-loop (29 tasks) | completed, PR #148 | `/code-review` high: 10 findings, all addressed or recorded; 2 criteria verified by the first CI run |
| assess-context (34) | completed, PR #149 | `/code-review`: 10 findings, all fixed; CodeRabbit + CodeQL findings fixed; golden Part A review owed by the product owner |
| assess-llm-runner (47) | completed, PR #150 (runner) + PR #151 (eval suite, NFR-023 gate) | `/code-review` on the full diff: 10 findings, all fixed. CodeRabbit: #150 3 rounds, ~45 findings (2 left by design, in the plan); #151 3 rounds, 22 findings (1 deferred to OQ-027, 1 to plan 8) |

## Gate

| Gate item | Result | Evidence |
|-----------|--------|----------|
| job-loop chaos test (kill -9 mid-handler, Postgres restart) by row counts | PASS | CI job "Assess chaos (kill, restart, half-open, refusals, idle RSS)", last run on PR #151 (run 42183310009); not re-run locally |
| BR-011 / NFR-017 canaries absent from LLM request bytes, logs, traces, audit | PASS | canary matrix through the real `DiscussionTurnRunner` (echo model) and the release-discipline tests on `develop`: 299 passed, 9 skipped (live-Moodle subset, not run in this gate) |
| NFR-022 targets measured, NFR-023 regression gate running per model/provider | **PARTIAL: measurement BLOCKED** | The gate runs: `make eval-assess` produces a hashed PASS/FAIL/BLOCKED report (CI tier tested), revision 0021 + `POST /api/v1/assess/admin/model-activations` refuse any report short of the full NFR-022 contract, startup records an unactivated triple and starts degraded. **No NFR-022 target is measured**: that needs the reference model (OQ-026) and the judge's human labels (OQ-027). Every staging run is BLOCKED until then. |
| no byte reaches the client before the post-check; worker idle RSS within its ceiling | PASS | `test_llm_release.py` and the runner stream-edge tests (stop, stall, empty stream); CI job "Assess stack (trust domains, worker RSS)" on PR #151 |

Test totals at the gate on `develop` (this machine): `make lint` clean (ruff, mypy, 20 import contracts kept);
`make test` 3163 passed, 78 skipped; `-m integration` for vektra-assess and vektra-app against Postgres 156 passed,
78 skipped (the same set CI skips). CI on the merge commit: unit, lint and push workflows green; the integration
workflow on `develop` is recorded in this PR's checks.

## "Working at the end"

Partly. The worker captures a snapshot (plan 6, live subset run locally 12/12 on the seeded Moodles) and the
runner holds a discussion on a `ContextBundle` through the whole post-check, driven by tests and the eval
adapter with a scripted or echo model. A discussion with a real model on real context has not been run: there
is no reference model yet (OQ-026).

## Deliberately open

- **NFR-022 measurement and NFR-023 activation** (gate item 3): blocked on OQ-026 (reference model and its
  hardware) and OQ-027 (owner of the eval suite: human labels in `tests/eval/assess/calibration/human_labels.jsonl`,
  dataset sign-off, the minimum number of human-labelled replies, which NFR-022 does not fix).
- **Startup without activation (coordinator decision 2026-11-06)**: the API starts degraded instead of refusing,
  because the activation endpoint lives in it. Serving is gated only by plan 8's pre-flight (M1.5), which must
  call `is_activated` before every session start; the requirement and its test are in plan 8's notes.
- **Prompt version**: the PR #150 review fixes changed the lexicon, so any activation must be run on the merged
  `prompt_version`.
- **Line structure of released text**: segments are stripped sentences joined with spaces; keeping the model's
  line breaks is deferred to the discussion UI plan (plan 9).
- **Golden Part A text** (plan 6): full review owed by the product owner.
- **Requests to the vektra-moodle session**: graded recordings for match / gapselect / multianswer / ddwtos;
  grade and release the `files` assignment for stu1, then re-record.
- CodeRabbit's docstring-coverage pre-merge check (40% vs 80%) was not addressed in either runner PR.

## Findings worth knowing

- CodeRabbit's free OSS plan allows one review per hour; a PR above 100 files is skipped outright. Split before
  opening (as for plan 7) and expect rate-limit pauses between rounds.
- The package integration job caught an ORM-mirror gap (`assess_model_activations` without a model) that the
  local unit suite cannot see: run `-m integration` for vektra-assess locally before pushing a migration.
- A contest marker on its own line is how `system.j2` asks the model to record a point; post-check changes must
  keep that line recorded (a CodeRabbit suggestion to attach it to the next sentence would have dropped it).
- A teacher quote opened after a quote lead is now held until it closes, without the 300-character cap (fail
  closed, bounded by `llm_max_tokens`).
