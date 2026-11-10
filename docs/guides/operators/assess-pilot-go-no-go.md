# Assess: pilot go/no-go

The pilot of the assessment discussion feature starts only when **every** criterion below is
`PASS`, each with a link to its evidence and the name of whoever signed it off. A criterion that
cannot be measured yet is `BLOCKED`, with what is missing and the command to run once it exists;
it is never reported `PASS`. A green status endpoint or a `completed` job is not evidence: rows,
counts, mail-sink messages, report files and exit codes are.

Status values: `PASS` (evidence linked), `FAIL`, `BLOCKED` (missing environment or input),
`OPEN` (a decision someone has to take), `NOT BUILT` (the test or tool does not exist yet).

**Verdict at the time of writing (2026-11-10): NO-GO.** One release blocker is in the code (the
worker does not start in production), and most criteria are blocked on the target environment.

## Versions tested together

| Item | Value |
|---|---|
| Vektra release | none yet (the assess feature is on `develop`, planned for `0.9.0`) |
| Plugin release (`plugin_version` sent at mint) | none recorded |
| `VEKTRA_ASSESS_MIN_PLUGIN_VERSION` on the pilot deployment | to record |
| LMS versions | to record (the readers are contract-tested against 4.3 and 5.2) |
| Model and `prompt_version` (with its activation id) | to record (OQ-026) |

## Criteria

### Release blocker in the code

| # | Criterion | Status | Evidence or what is missing |
|---|---|---|---|
| 0 | A production worker starts: no unbound slot | **FAIL** | `job:rotate_keys` has no handler; `assert_all_bound` refuses to start without the development override. Pinned by `vektra-app/tests/test_assess_processes.py` (`PENDING_OWNER_PLANS`). Fix: bind a handler or remove the kind (migration on `ck_assess_jobs_kind`). See [deployment](assess-deployment.md#current-release-blocker) |

### Robustness (REQ-067, REQ-071, REQ-075, ARCH-068)

| # | Criterion | Status | Evidence or what is missing |
|---|---|---|---|
| 1 | Chaos matrix: 0 invariant violations on two consecutive nightly runs and on the release tag | **BLOCKED** | The gate declares 71 cases and 27 scenarios; the PR job "Assess chaos" passed on PR #195 ([run 42906139672](https://github.com/vektralabs/vektra-stack/actions/runs/42906139672)). One PR run is not the criterion. The nightly has **never run**: `.github/workflows/chaos-assess.yml` exists only on `develop`, and GitHub runs a `schedule` trigger only from the default branch (`gh run list --workflow chaos-assess.yml` answers 404). Needs the workflow on `main` (or a manual `workflow_dispatch` series), then two consecutive green runs and the tag run; keep each run's `invariants.md` |
| 2 | SMTP outage and mid-SMTP kill: exactly one SENT row per recipient, at most one audited duplicate | **BLOCKED** (on 1) | Covered by the chaos scenarios `test_chaos_mid_smtp_kill.py` and `test_chaos_postgres_restart.py` (mail sink holding DATA); evidence is the same release runs as 1 |

### Pilot site and decisions

| # | Criterion | Status | Evidence or what is missing |
|---|---|---|---|
| 3 | Setup check on the pilot site: every item `OK`, including `ui_origin_distinct` and `privacy_config` (REQ-079) | **BLOCKED** | No pilot site is registered. Run the [walkthrough](assess-deployment.md#setup-check-walkthrough) and link the report (`GET .../setup-check/{report_id}`) |
| 4 | DPO values set (OQ-020) and erasure mode chosen (OQ-033) | **OPEN** | DPO decisions pending: retention days, legal basis, notice (`PUT /api/v1/assess/sites/{site_id}/privacy`) and `VEKTRA_ASSESS_ERASURE_MODE` (`erase` or `retain_exam_evidence`) |
| 5 | OQ-029 reading confirmed by product and QA ("exactly one SENT record per recipient, at most one audited duplicate on a crash") | **OPEN** | The code implements the design default; confirmation not recorded |
| 6 | K and hardware decided (OQ-032, NFR-020) with NFR-018 evidence | **BLOCKED** | Needs the target VM (or HPC) and the reference model (OQ-026). Run `scripts/assess_burst.py` at K = 5, 10, 15, 20 with bursts of 20/30/40 Starts, then `VEKTRA_NFR_ASSESS_DSN=... VEKTRA_NFR_ASSESS_SINCE=... python scripts/assess_nfr_report.py`; write `docs/guides/operators/assess-sizing.md` with the table. Until then K = 10 is a placeholder |
| 7 | RAG for discussions confirmed OFF | **PASS** (code) | `VEKTRA_ASSESS_RAG_ENABLED=true` is refused at startup and `CHECK (rag_enabled = false)` makes it unrepresentable (CHANGELOG `[Unreleased]`, teacher configuration). Re-confirm on the pilot deployment's settings |

### NFR campaign (all on the target environment)

| # | Criterion | Status | Evidence or what is missing |
|---|---|---|---|
| 8 | NFR-018 latency at the chosen K, per provider and model; breaches listed | **BLOCKED** | Target VM and reference model (OQ-026). `scripts/assess_nfr_report.py` reports commit latency and full reply from rows; "first visible output" is in no row and is always reported BLOCKED by the script |
| 9 | NFR-019: 24 h soak with a 1-minute probe; commit rate and service_failure rate | **BLOCKED** | Target VM. Same report script over the soak window. The "admitted" definition (excluding `capacity`, `part_b_rejected`, `terminated_by_teacher`) is an assumption to confirm with QA |
| 10 | NFR-021 on the target VM: generation p99 ≤ 60 s, SENT p95 ≤ 5 min | **BLOCKED** | Target VM and a production-like relay. Run `scripts/assess_transcript_timing.py` |
| 11 | NFR-014 / INT-001: 0 cross-student reads on LMS 4.3 and 5.2, every rejection audited | **NOT BUILT** / **BLOCKED** | The scripted pen matrix (`tests/pen/assess/`) does not exist yet; it also needs live LMS 4.3 and 5.2 sites with login-as. Per OQ-031 the login-as case is "accepted residual risk, trust the plugin" (no LMS function exposes the log) |
| 12 | NFR-015 / NFR-017: canary suites on the target model and provider, 0 canaries in request bytes and outputs, RAG OFF → 0 retrieval calls; run id recorded | **BLOCKED** | Reference model (OQ-026). Suites from plans 6 and 7 ([canary matrix](../contributors/assess-canary-matrix.md)) |
| 13 | NFR-016: accessibility pass on the UI, the plugin embedding and the transcript, both LMS versions | **BLOCKED** | Automated part exists (`make test-assess-e2e`: axe, keyboard, reflow, 0 WCAG 2.1 A/AA violations on the UI states). The manual NVDA / VoiceOver pass needs a person with a screen reader and the plugin pages |
| 14 | REQ-080: canary grep of the target logs, mail logs and `/metrics` returns 0 | **BLOCKED** | A deployment with an LMS site, an activated model and a test relay. Run `tests/nfr/assess_canary_grep.sh` over the stack's logs, mail log and `/metrics` |
| 15 | NFR-022 / NFR-023 quality gates reported by their owner | **OPEN** / **BLOCKED** | Owner not named (OQ-027); the activation itself is gated by `POST /api/v1/assess/admin/model-activations` with the `make eval-assess` report |

### Operations

| # | Criterion | Status | Evidence or what is missing |
|---|---|---|---|
| 16 | Restore drill completed within the last 30 days: transcript sha256 equal after restore, purged data unrecoverable, `keys verify` exit 0 (ARCH-079) | **BLOCKED** | No clean host. Procedure: [keys runbook, restore drill](../assess-keys-runbook.md#restore-drill); record it below |
| 17 | Alerts wired to an on-call recipient | **BLOCKED** | Example rules in `deploy/prometheus/assess-alerts.yml.example`; `VEKTRA_ASSESS_OPERATOR_EMAIL` receives the worker's alert mail. The pilot's Prometheus, Alertmanager route and on-call recipient are not set up |
| 18 | Key rotation rehearsed for the KEKs | **BLOCKED** | Procedure in the [keys runbook](../assess-keys-runbook.md#key-rotation). Known gaps: no command for `reseal_teacher_configs` (the teacher-config private kid cannot be retired) or `reseal_handoffs` (waiting is the supported path) |

## Restore drill record

| Date | Operator | Image version | Dump / key backup ids | `verify` worker / api | Fingerprints match | Transcript id, sha256 before / after | Purge counts (4 queries) | Result |
|---|---|---|---|---|---|---|---|---|
| none yet | | | | | | | | |

## Sign-off

| # | Signed off by | Date |
|---|---|---|
| 0-18 | | |
