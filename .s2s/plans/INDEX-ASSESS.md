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

1. **Branch per wave**, off `develop`: `feat/assess-wN` in vektra-stack and, when the wave contains
   plugin plans, `feat/discuss-wN` in vektra-moodle-discuss. No per-plan branches.
2. **Loop**: `/loop` with the wave prompt below. Each iteration does one task (or a small group of
   tightly coupled tasks), tests it, commits it, and updates the plan file in the same commit.
3. **End of each plan** (inside the loop): run `/code-review` on that plan's diff, fix the findings,
   **re-run the plan's full test suite** (a fix that breaks something goes back to step 2), then set
   the plan to `completed` here and in the plan file and move on to the next plan.
4. **End of wave**: run the wave gate; if it fails, fix, re-run the affected tests and **re-run the
   whole gate** until it passes. Write the evidence to `.s2s/waves/assess-wN-review.md` (based on
   `.s2s/waves/checklist-template.md`), then open ONE PR per repo `feat/*-wN` → `develop` **with the
   `/github-workflow:github-pr-creation` skill** (it asks the user to confirm target and content,
   so the loop waits there), handle the review (`/github-workflow:github-pr-review`), and merge only on the user's decision
   (`/github-workflow:github-pr-merge`). The branch is deleted on merge.
5. **Between waves**: `/compact`, then start the next wave from `develop`.

`develop` therefore only ever receives complete, gate-verified waves. As a second line of defence,
merged code stays dormant: with `VEKTRA_ASSESS_ENABLED=false` (the default) no `/api/v1/assess`
route is mounted and no assess service runs (plan assess-foundations, task 6.1b).

### Wave loop prompt

Start a wave with `/loop` and this prompt, replacing `N`:

> Wave WN of `.s2s/plans/INDEX-ASSESS.md`. Work on branch `feat/assess-wN` (vektra-stack) and, for plugin plans, `feat/discuss-wN` (vektra-moodle-discuss); create them from `develop` if missing. Read INDEX-ASSESS, pick the `in_progress` plan of wave WN, or the first `pending` one whose dependencies are `completed`, and read the full plan file (plugin plans: `../vektra-moodle-discuss/.s2s/plans/`). If tasks are already `[x]`, check the code that implements them before continuing. Do the first unchecked task: implement, run its tests, check it off, commit (Conventional Commits, plan file in the same commit). If a test fails, fix and re-run until green. When the plan's tasks and acceptance criteria are all done, run `/code-review` on the plan's diff, fix the findings, re-run the plan's full test suite until green, and mark the plan `completed` in the plan file and in INDEX-ASSESS. When every plan of the wave is `completed`, run the wave gate; if it fails, fix, re-run the affected tests and the whole gate until it passes. Write `.s2s/waves/assess-wN-review.md`, push, create the wave PR(s) to `develop` with the `/github-workflow:github-pr-creation` skill, and stop the loop. Obey the stop rules in INDEX-ASSESS.

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
- **The wave is complete**: the PR(s) are open, the gate evidence is written.

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
| 4 | [assess-moodle-ws](20261101-154950-assess-moodle-ws.md) | Moodle 4.3/5.2 envs, WS client, capability matrix, setup check | 43 | 1 (package scaffold only) | pending |
| 2 | [assess-data-crypto](20261101-154950-assess-data-crypto.md) | assess_* schema, grants, SECURITY DEFINER, envelope crypto | 38 | 1 | completed |
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

### Decisions 2026-11-02 (user)
- Plan 2 (assess-data-crypto) runs before plan 4 (assess-moodle-ws): `assess_sites` is created by plan 2 task 1.1, not by foundations.
- Moodle dev environments (plan 4 group A) live in the `vektra-moodle` repo on `feat/assess-w1` (created from `develop`), one extra PR for the wave; layout verified before creating files. Dev environment runs on the user's machine.
