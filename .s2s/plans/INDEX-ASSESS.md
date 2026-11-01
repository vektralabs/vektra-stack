# Assessment discussion chatbot: implementation plan index

**Generated**: 2026-11-01
**Updated**: 2026-11-01
**Approved**: 2026-11-01 (coordinator)
**Total plans**: 16, with 490 tasks in total: 13 backend plans (425 tasks) in this repo, and 3 Moodle plugin plans (65 tasks) in `vektralabs/vektra-moodle-discuss`.
**Status legend**: `pending` | `in_progress` | `completed`

This file is the single source of truth for progress across BOTH repositories. When a plugin plan
changes status, update its plan file in vektra-moodle-discuss AND its row here.

---

## Session start prompt

Copy-paste this as the first message in every implementation session:

> Read `.s2s/plans/INDEX-ASSESS.md`. Find the `in_progress` plan, or the first `pending` plan in the lowest-numbered wave whose dependencies are `completed`. Read the full plan file (plugin plans live in `../vektra-moodle-discuss/.s2s/plans/`). If any tasks are already checked `[x]`, read the corresponding source files to understand what was actually implemented before continuing. Then proceed with the first unchecked `[ ]` task.

Design context: `.s2s/architecture.md` Appendix B (ARCH-065..079, COMP-001..008, INT-001..003),
ADR-0027..0032, requirements REQ-067..103 / BR-006..016 / NFR-014..023, and the OQ-025 spike
report (`../vektra-moodle-discuss/spike/oq025/REPORT.md`).

---

## How to work with these plans

### Starting a plan

1. Check that every plan in its **Depends on** column is `completed`.
2. Read the full plan file (scope, Design Notes, acceptance criteria) before writing any code.
3. Create the plan's branch **from `develop`** (`feat/assess-<slug>` here, `feat/discuss-<slug>` in the
   plugin repo). There is no long-lived integration branch: every plan merges into `develop` through
   its own PR (CONTRIBUTING.md branch strategy). Merged-but-unfinished code must stay dormant: with
   `VEKTRA_ASSESS_ENABLED=false` (the default) no `/api/v1/assess` route is mounted and no assess
   service runs (plan 1 acceptance criterion).
4. Set `**Status**` in the plan file to `in_progress`, and update the row in this file to match.

### Working through tasks

- Check off tasks as you complete them: `- [ ]` -> `- [x]`.
- Do not move on until the current task is testable and verified.
- Project rule (`.claude/CLAUDE.md`): **verify empirically, do not trust the reports**. An acceptance criterion is met when rows, points or request bytes have been counted, not when a status field says `completed`.
- A test that cannot run (missing Docker, Moodle env, LLM) is reported as SKIPPED/BLOCKED, never as passed (each plan's "False-pass guard").

### When context runs out mid-plan

1. Add a note in the plan's `## Notes`: what was done, what is next, open issues.
2. Commit the plan file with the checked tasks and the note.

To resume: read this file, find the `in_progress` plan, go to its `## Notes`, and continue from the first unchecked task.

### Completing a plan

1. All tasks are `[x]` and all acceptance criteria are verified (not only the tasks).
2. Tests are written and passing; CI is green.
3. Open **one PR per plan**. Review it (CodeRabbit + `/code-review`), then merge.
4. Set `**Status**` to `completed` in the plan file and in this table. Commit.

### Extending plans

Sub-tasks discovered during implementation are added to the existing plan as `[ ]` tasks. A new plan
is created only for a distinct deliverable (more than ~5 new tasks). A change to the design is
recorded in architecture.md / the ADRs first.

---

## Wave process

A wave is a **milestone that ends with something working end to end**, not a dependency layer.
Plans inside a wave are done in dependency order (independent ones in parallel), each with its own
PR. The next wave starts only after the wave gate passes. Four waves, W1-W4.

1. **Develop**: one branch and one PR per plan. Large plans (llm-runner, session-lifecycle) are
   reviewed at their internal milestones, not only at the end, because wave gates are far apart.
2. **Wave gate**, when every plan in the wave is `completed`:
   - Fill in `.s2s/waves/checklist-template.md` and save it as `.s2s/waves/assess-wave-N-review.md` (versioned).
   - Run the integration tests across the wave's plans (not only each plan's own suite).
   - Run the wave-specific gate in the tables below and record the evidence (counts, bytes, logs).
   - Run the dormant-capability check (`.claude/CLAUDE.md`): for every path the wave made live, ask what it can now destroy.
3. **Transition**: commit the review, update this file, run `/compact`, then start the next wave.

---

## Execution order

`→` inside a wave means "after" (dependency); plans without an arrow between them can run in parallel.

### W1: Foundations

foundations → (moodle-ws ∥ data-crypto ∥ parser); P1 after the moodle-ws dev envs.
moodle-ws dev envs and fixture seeding can start on day one (they do not need the package).

| # | Plan | Title | Tasks | Depends on | Status |
|---|------|-------|-------|------------|--------|
| 1 | [assess-foundations](20261101-154950-assess-foundations.md) | vektra_assess package, scopes, DB roles, secrets, worker entry, deps | 40 | none | pending |
| 4 | [assess-moodle-ws](20261101-154950-assess-moodle-ws.md) | Moodle 4.3/5.2 envs, WS client, capability matrix, setup check | 43 | 1 (package scaffold only) | pending |
| 2 | [assess-data-crypto](20261101-154950-assess-data-crypto.md) | assess_* schema, grants, SECURITY DEFINER, envelope crypto | 38 | 1 | pending |
| 5 | [assess-parser](20261101-154950-assess-parser.md) | Network-less parser sidecar | 26 | 1 | pending |
| P1 | [discuss-foundation](../../../vektra-moodle-discuss/.s2s/plans/20261101-154950-discuss-foundation.md) | local_vektradiscuss scaffold, client, settings, CI | 19 | 4 (dev envs) | pending |

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
| 3 | [assess-job-loop](20261101-154950-assess-job-loop.md) | Postgres-native leased job loop, ticks, heartbeat | 29 | 2 | pending |
| 6 | [assess-context](20261101-154950-assess-context.md) | Snapshot capture, assemble_context, canary suite | 34 | 2, 4, 5 | pending |
| 7 | [assess-llm-runner](20261101-154950-assess-llm-runner.md) | DiscussionTurnRunner, post-check, prompts, eval gate | 44 | 6 | pending |

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
| 8 | [assess-session-lifecycle](20261101-154950-assess-session-lifecycle.md) | Tokens, gates, Start/commit/rollback, timing, outage, limits | 47 | 3, 7 | pending |
| 9 | [assess-discussion-ui](20261101-154950-assess-discussion-ui.md) | Cross-origin iframe UI, fetch streaming, CSP | 26 | 8 | pending |
| 10 | [assess-transcript-delivery](20261101-154950-assess-transcript-delivery.md) | Transcript rendering, outbox SMTP, download tokens | 26 | 3, 8 | pending |
| P2 | [discuss-student](../../../vektra-moodle-discuss/.s2s/plans/20261101-154950-discuss-student.md) | Button, pre-session screen, mint, iframe, own transcript | 24 | P1, 8, 9, 10 | pending |

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
| 11 | [assess-teacher-ops](20261101-154950-assess-teacher-ops.md) | Teacher config (write-only), list, reset, terminate, extra time | 21 | 8, 10 | pending |
| 12 | [assess-privacy-retention](20261101-154950-assess-privacy-retention.md) | Retention, crypto-shred purge, erasure, export, monitoring | 27 | 10 | pending |
| P3 | [discuss-teacher-privacy](../../../vektra-moodle-discuss/.s2s/plans/20261101-154950-discuss-teacher-privacy.md) | Settings section, teacher pages, download proxy, Privacy API | 22 | P1, 10, 11, 12 | pending |
| 13 | [assess-hardening-release](20261101-154950-assess-hardening-release.md) | Chaos suite, NFR campaign, runbooks, pilot go/no-go | 24 | all | pending |

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
