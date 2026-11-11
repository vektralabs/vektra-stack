# Assessment discussion chatbot: implementation plan index

**Generated**: 2026-11-01
**Updated**: 2026-11-01
**Approved**: 2026-11-01 (coordinator)
**Total plans**: 16, with 490 tasks in total: 13 backend plans (425 tasks) in this repo, and 3 Moodle plugin plans (65 tasks) in `vektralabs/vektra-moodle-discuss`.
**Status legend**: `pending` | `in_progress` | `completed`

This file is the single source of truth for progress across BOTH repositories. When a plugin plan
changes status, update its plan file in vektra-moodle-discuss AND its row here.

---

## How a wave is run

Development is done by Claude Code in **loop mode**, one wave at a time:

1. **Branches** (changed after W1, which was one 534-file PR that the review bot refused: it caps
   at 100 files). From W2 on, **one branch and one PR per plan**, off `develop`:
   `feat/assess-<plan-slug>` in vektra-stack (for example `feat/assess-job-loop`). A plan that
   depends on an unmerged plan waits for that merge; independent plans run in any order. If a plan
   is heading past about 100 changed files, split it before opening the PR (recorded fixtures and
   datasets go in their own commit). W1 stays as it was run.
   **The Moodle repos are not touched by this loop.** vektra-moodle and vektra-moodle-discuss are
   changed only by the user's other session (the one working in those repos; it was
   `vektra-moodle-cf` on 2026-11-03). When a task needs a change there, the loop writes the
   request (repo, file, what and why) in the plan's `## Notes`, reports it to the user, and
   carries on with tasks that do not depend on it. Plugin plans (P2, P3) are run by that session,
   not by this loop.
2. **Loop**: `/loop` with the wave prompt below. Each iteration does one task (or a small group of
   tightly coupled tasks), tests it, commits it, and updates the plan file in the same commit.
3. **End of each plan** (inside the loop): run `/code-review` on that plan's diff, fix the findings,
   **re-run the plan's full test suite** (a fix that breaks something goes back to step 2), then set
   the plan to `completed` here and in the plan file and move on to the next plan.
4. **End of each plan**: after step 3, open the plan's PR to `develop` **with the
   `/github-workflow:github-pr-creation` skill** (it asks the user to confirm target and content,
   so the loop waits there), handle the review (`/github-workflow:github-pr-review`), and merge only on the user's decision
   (`/github-workflow:github-pr-merge`). The branch is deleted on merge. **End of wave**: when every
   plan of the wave is merged, run the wave gate on `develop`; if it fails, fix on a new branch,
   re-run the affected tests and **re-run the whole gate** until it passes. Write the evidence to
   `.s2s/waves/assess-wN-review.md` (based on `.s2s/waves/checklist-template.md`) in a small docs PR.
5. **Between waves**: `/compact`, then start the next wave from `develop`.

`develop` therefore only ever receives complete, gate-verified waves. As a second line of defence,
merged code stays dormant: with `VEKTRA_ASSESS_ENABLED=false` (the default) no `/api/v1/assess`
route is mounted and no assess service runs (plan assess-foundations, task 6.1b).

### Wave loop prompt

Start a wave with `/loop` and this prompt, replacing `N`:

> Wave WN of `.s2s/plans/INDEX-ASSESS.md`. Work in vektra-stack only: one branch and one PR per plan, `feat/assess-<plan-slug>` off `develop` (create it if missing). Never edit `../vektra-moodle` or `../vektra-moodle-discuss`: another session owns them; a needed change there goes in the plan's `## Notes` as a request and is reported to the user. Read INDEX-ASSESS, pick the `in_progress` plan of wave WN, or the first `pending` one whose dependencies are `completed` and merged, and read the full plan file. If tasks are already `[x]`, check the code that implements them before continuing. Do the first unchecked task: implement, run its tests, check it off, commit (Conventional Commits, plan file in the same commit). If a test fails, fix and re-run until green. When the plan's tasks and acceptance criteria are all done, run `/code-review` on the plan's diff, fix the findings, re-run the plan's full test suite until green, and mark the plan `completed` in the plan file and in INDEX-ASSESS. Check the plan's diff stays under about 100 files (split before opening the PR if not), push, create the plan's PR to `develop` with the `/github-workflow:github-pr-creation` skill, and stop the loop for that plan; the user merges it, then restart the loop for the next plan. After the last plan of the wave is merged, run the wave gate on `develop`, write `.s2s/waves/assess-wN-review.md` and open it as a small docs PR. Obey the stop rules in INDEX-ASSESS.

### Stop rules

The loop **stops and reports to the user** (it does not work around the problem) when:

- **A test fails** and is not fixed after a few focused attempts. Never skip, disable, weaken or
  `xfail` a test to make progress.
- **A test cannot run** (Docker, Moodle env, Postgres, LLM endpoint unavailable). It is reported as
  BLOCKED, never as passed or skipped-and-fine.
- **A decision is needed**: an open question without a recorded default, a conflict between plan
  and code that changes the design, or a change to an accepted ADR or requirement.
- **Something destructive or outward-facing** is required beyond the repo: deleting data outside
  the test containers, touching a real Moodle or SMTP server, publishing anything.
- **The environment is at risk**: disk nearly full (the dev machine was at 94%), a container that
  will not start, signed commits failing.
- **A plan is complete**: its PR is open; or **the wave is complete**: the gate evidence is written.

On every stop, the plan's `## Notes` gets the current state and the reason, and is committed, so
the next run resumes from there.

### Rules while working

- **Verify empirically** (`.claude/CLAUDE.md`): an acceptance criterion is met when rows, points or
  request bytes have been counted, never because a status field or a job says `completed`.
- **A dormant capability is not harmless**: whenever a task makes a path live, ask what it can now
  destroy, and test that before moving on.
- Sub-tasks discovered during work are added to the plan as `[ ]`. A design change goes to
  architecture.md / the ADRs first, and that is a stop.
- Commits are signed (branch protection); CHANGELOG `[Unreleased]` is updated for user-visible
  changes.

---

## Execution order

`→` inside a wave means "after" (dependency); plans without an arrow between them can run in parallel.

### W1: Foundations

foundations → (moodle-ws ∥ data-crypto ∥ parser); P1 after the moodle-ws dev envs.
moodle-ws dev envs and fixture seeding can start on day one (they do not need the package).

| # | Plan | Title | Tasks | Depends on | Status |
|---|------|-------|-------|------------|--------|
| 1 | [assess-foundations](20261101-154950-assess-foundations.md) | vektra_assess package, scopes, DB roles, secrets, worker entry, deps | 40 | none | completed |
| 4 | [assess-moodle-ws](20261101-154950-assess-moodle-ws.md) | Moodle 4.3/5.2 envs, WS client, capability matrix, setup check | 43 | 1 (package scaffold only) | completed |
| 2 | [assess-data-crypto](20261101-154950-assess-data-crypto.md) | assess_* schema, grants, SECURITY DEFINER, envelope crypto | 38 | 1 | completed |
| 5 | [assess-parser](20261101-154950-assess-parser.md) | Network-less parser sidecar | 26 | 1 | completed |
| P1 | [discuss-foundation](../../../vektra-moodle-discuss/.s2s/plans/20261101-154950-discuss-foundation.md) | local_vektradiscuss scaffold, client, settings, CI | 19 | 4 (dev envs) | completed |

**Working at the end**: package and contracts, DB roles and grants, envelope crypto, typed WS readers
on Moodle 4.3 and 5.2, parser sidecar, installable plugin.

**Gate**:
- import-linter contracts green (worker import ban included);
- with `VEKTRA_ASSESS_ENABLED=false` no assess route is mounted (route-table assertion);
- BUG-027 qemu baseline passes with `cryptography` (a FAIL is a blocker: no fallback);
- grant matrix asserted against pg_catalog; the hand-off row is deleted on every CLOSED path (row counts);
- readers pass on 4.3 and 5.2 fixtures, including the five OQ-025 spike traps; the setup check rejects the negative accounts;
- the malicious-file corpus is contained by the sidecar;
- the plugin installs on 4.3/PHP 8.2 and 5.2/PHP 8.4 in CI, and the release zip contains no dev artefacts.

### W2: Engine

(job-loop ∥ context) → llm-runner.

| # | Plan | Title | Tasks | Depends on | Status |
|---|------|-------|-------|------------|--------|
| 3 | [assess-job-loop](20261101-154950-assess-job-loop.md) | Postgres-native leased job loop, ticks, heartbeat | 29 | 2 | completed (2 criteria verified by the first CI run, see plan) |
| 6 | [assess-context](20261101-154950-assess-context.md) | Snapshot capture, assemble_context, canary suite | 34 | 2, 4, 5 | completed (golden review owed, see plan) |
| 7 | [assess-llm-runner](20261101-154950-assess-llm-runner.md) | DiscussionTurnRunner, post-check, prompts, eval gate | 47 | 6 | completed (eval PASS blocked on OQ-026/027, see plan) |

**Working at the end**: the worker captures a snapshot from a seeded Moodle and the runner holds a
discussion on that real context (no UI yet; driven by tests and the eval adapter).

**Gate**:
- job-loop chaos test (kill -9 mid-handler, Postgres restart) verified by row counts;
- BR-011 / NFR-017 canaries absent from the exact LLM request bytes, logs, traces and audit;
- NFR-022 targets measured, NFR-023 regression gate running per model/provider;
- no byte reaches the client before the post-check; worker idle RSS within its ceiling.

### W3: Session end to end

session-lifecycle → (discussion-ui ∥ transcript-delivery) → P2.
The 40-Start burst test runs EARLY, at session-lifecycle milestone M1, not at the gate.

| # | Plan | Title | Tasks | Depends on | Status |
|---|------|-------|-------|------------|--------|
| 8 | [assess-session-lifecycle](20261101-154950-assess-session-lifecycle.md) | Tokens, gates, Start/commit/rollback, timing, outage, limits | 47 | 3, 7 | completed (PR #153 + 8b; live IDOR and REQ-073 PASS on the dev sites 2026-11-08; real-model burst BLOCKED, see plan) |
| 9 | [assess-discussion-ui](20261101-154950-assess-discussion-ui.md) | Cross-origin iframe UI, fetch streaming, CSP | 29 | 8 | completed (download BLOCKED on plan 10; item picker SHOULD deferred; /state selected_attempt/files follow-up, see plan) |
| 10 | [assess-transcript-delivery](20261101-154950-assess-transcript-delivery.md) | Transcript rendering, outbox SMTP, download tokens | 26 | 3, 8 | completed (10a PR #159 merged; 10b SMTP delivery on `feat/assess-transcript-delivery-smtp`: recipients from the teacher's sealed activity config + `VEKTRA_ASSESS_RECIPIENT_DOMAINS`, REQ-082 amended 2026-11-07; NFR-021 run BLOCKED, OQ-029 confirmation and NVDA read-through open. 2026-11-09 `feat/assess-notify-smtp`: `teacher_notify` / `operator_notify` bound, by email over the same relay, `VEKTRA_ASSESS_OPERATOR_EMAIL` required by the worker, revision 0039) |
| P2 | [discuss-student](../../../vektra-moodle-discuss/.s2s/plans/20261101-154950-discuss-student.md) | Button, pre-session screen, mint, iframe, own transcript | 24 | P1, 8, 9, 10 | in_progress (P2a merged, vektra-moodle-discuss#2; P2b in progress, #3 draft) |

**Working at the end**: a student, from the Moodle results page, starts a discussion, talks to the
bot, finishes, and the transcript is delivered to the graders and downloadable by the student.

**Gate**:
- burst test repeated on the integrated stack;
- all six close reasons plus rollback verified by row counts; IDOR negative tests; no token accepted in a query string;
- login-as refused end to end;
- NFR-016 accessibility (axe); the transcript post-render allowlist rejects every listed vector;
- mail sink receives exactly one SENT per recipient; NFR-021 timing;
- plugin Behat green on 4.3 and 5.2.

### W4: Teacher, privacy, release

(teacher-ops ∥ privacy-retention) → P3 → hardening-release.

| # | Plan | Title | Tasks | Depends on | Status |
|---|------|-------|-------|------------|--------|
| 11 | [assess-teacher-ops](20261101-154950-assess-teacher-ops.md) | Teacher config (write-only), list, reset, terminate, extra time | 21 | 8, 10 | completed (11a merged, PR #170; 11b on `feat/assess-teacher-ops-11b`: list, reset, terminate, extra time, opening fallback, IDOR matrix. Annotated: REQ-087 continuity deferred by the product owner 2026-11-08 (no previous-turns block in the context; plan 13 or a dedicated plan); REQ-092 out of v1, its flag refused at startup; live LMS reads of editors / override managers BLOCKED (dev envs). Migration 0038 waits for 0037 to rebase) |
| 12 | [assess-privacy-retention](20261101-154950-assess-privacy-retention.md) | Retention, crypto-shred purge, erasure, export, monitoring | 27 | 10 | completed (12a merged, PR #191: privacy settings, retention purge with tombstones, subject and activity erasure, export; 12b on `feat/assess-privacy-retention-12b`: settings bounds, /health/assess readiness (503) with details, content-free /metrics (revision 0041) and alert rules, operator SMTP test send (revision 0042), keys verify CLI and key inventory, notify-chain safety net, abandoned setup reports. BLOCKED: the compose canary grep over a seeded discussion (no LMS, model or relay sink in compose), moved to plan 13) |
| P3 | [discuss-teacher-privacy](../../../vektra-moodle-discuss/.s2s/plans/20261101-154950-discuss-teacher-privacy.md) | Settings section, teacher pages, download proxy, Privacy API | 22 | P1, 10, 11, 12 | pending |
| 13 | [assess-hardening-release](20261101-154950-assess-hardening-release.md) | Chaos suite, NFR campaign, runbooks, pilot go/no-go | 24 | all | in_progress (13a merged (#195): chaos consolidation, NFR report script, image boundary, deferred items; 13b on `docs/assess-hardening-release-13b`: runbooks, reference docs, release checklist, go/no-go (NO-GO). Open: `job:rotate_keys` unbound (production worker refuses to start), NFR campaign and restore drill BLOCKED on the target environment) |

**Working at the end**: the complete feature, ready for the pilot.

**Gate**:
- purge and erasure leave 0 rows for the subject, other subjects unchanged (counts);
- the API role cannot SELECT sealed teacher config;
- Privacy API export and delete end to end; an unhandled delete never succeeds silently;
- download proxy streams with attachment + nosniff + CSP sandbox + no-store;
- the pilot go/no-go criteria in plan 13.

---

## Dependency graph (compact)

```
W1  foundations ─┬─► moodle-ws ──────────► P1 discuss-foundation
                 ├─► data-crypto
                 └─► parser
W2  data-crypto ─► job-loop
    data-crypto + moodle-ws + parser ─► context ─► llm-runner
W3  job-loop + llm-runner ─► session-lifecycle ─┬─► discussion-ui ─┐
                                                 └─► transcript-delivery ─┴─► P2 discuss-student (+P1)
W4  session-lifecycle + transcript-delivery ─► teacher-ops ─┐
    transcript-delivery ─► privacy-retention ───────────────┴─► P3 discuss-teacher-privacy (+P1)
    everything ─► hardening-release
```

---

## Open questions that can affect plans

| ID | Topic | Default in plans | Owner |
|---|---|---|---|
| OQ-035 | Rubric descriptors in model context; pilot Moodle version | Descriptors excluded; on 4.3 REQ-091 for overridden quizzes | product / coordinator |
| OQ-020 | DPO: legal basis, retention, LLM data egress | Fail closed until configured | DPO |
| OQ-033 | Erasure vs exam-evidence retention | Erasure endpoint, explicit mode | DPO |
| OQ-029 | SMTP exactly-once reading | At most one audited duplicate on crash | product / QA |
| OQ-026, OQ-027 | Reference model; eval-suite owner | Measured in W2/W3 | product / ops |
| OQ-025 (rest) | Math/image fidelity, other question types | 5 question types verified | W1 (assess-moodle-ws) |

---

## Phase references

Phase 1: [INDEX-PHASE1.md](INDEX-PHASE1.md). Phase 2: [INDEX-PHASE2.md](INDEX-PHASE2.md).

### Decisions 2026-11-02 (user)
- Plan 2 (assess-data-crypto) runs before plan 4 (assess-moodle-ws): `assess_sites` is created by plan 2 task 1.1, not by foundations.
- Moodle dev environments (plan 4 group A) live in the `vektra-moodle` repo on `feat/assess-w1` (created from `develop`), one extra PR for the wave; layout verified before creating files. Dev environment runs on the user's machine.

### Follow-ups (not in any wave PR)
- Update `.s2s/requirements.md`: OQ-025/OQ-031 status text and the REQ-101 question-type list, after the private findings document (`vektra-internal/stack/20261101-oq025-oq031-moodle-ws-spike-findings.md`, written, not yet committed) has been reviewed.
- REQ-082 recipients: the service account gets no grader email from Moodle; decide the source (plan 10 / teacher `recipients_extra`).
- vektra-moodle: the Moodle 5.1 `MOODLE_TGZ_SHA256` in `docker/Dockerfile` pins a moving `moodle-latest-501.tgz` and fails today; and `docker-moodle43/` is an orphan local directory.
- ~~`.s2s/BACKLOG.md` contains the university project name; clean it (public repo rule).~~ Done 2026-11-03 (DEBT-040); history not rewritten, old `block_vektra` release assets kept (decision of the user).
- FU-DISCUSS-1 (vektra-moodle-discuss, LOW, hardening), deferred from the review of PR #1: cap the buffered response body in `classes/local/http/curl_transport.php::send()` (used by `assess_client::request()`). `stream()` already has `MAX_STREAM_BYTES` (20 MB) via `CURLOPT_WRITEFUNCTION`; `send()` buffers everything through `\curl->get()/post()`, so a huge body can exhaust `memory_limit`. Fix: a `CURLOPT_WRITEFUNCTION` that accumulates and aborts over a threshold (1-5 MB) with error code `too_large`, like `stream()`; a check after `send()` does not help, the memory is already allocated. Mitigations today: 5 s total timeout, trusted server, small JSON endpoints. Acceptance: an integration test (local server serving a large body) plus PHPUnit on an installed Moodle; the plugin unit tests use a fake transport and cannot cover the real curl path. Natural home: plan discuss-foundation follow-up or the first plugin plan that touches the transport.
- FU-DISCUSS-2 (vektra-moodle-discuss, LOW, process decision: decided, option c), deferred from the review of PR #1: the tag-triggered `release.yml` (`push` tag `v*`) does not wait for the Moodle matrix; `moodle-plugin-ci.yml` runs only on push to main/develop and on PRs, not on tags. A tag publishes the zip even if the Moodle 4.3/5.2 matrix and lint did not pass on that commit; the release job only checks tag == `v$plugin->release` and the zip content. As a result the strict mode of `checkout-block-vektra.sh` for `refs/tags` is effectively unused. Options: (a) `release.yml` depends on the CI workflow via `workflow_call`/`needs`; (b) check through the API that the tagged commit's checks are green before publishing; (c) leave it and document that tags are cut from main with green CI. (a) and (b) add complexity and couple two workflows; the review tool itself called it a poor tradeoff. Decision 2026-11-03 (user): option (c). Tags are cut from `main` with green CI; nothing in the workflows changes. To do in the plugin repo: one line in its release notes or README saying so (not yet written). Revisit when the first real tag is cut.
