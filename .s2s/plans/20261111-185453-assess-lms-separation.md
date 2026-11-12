# Implementation Plan: Separating `vektra-assess` from the LMS (the `LmsProvider` Protocol)

**ID**: 20261111-185453-assess-lms-separation
**Status**: in_progress (ADR-0033 accepted 2026-11-11, with OD-1 to OD-6 decided; phase 4, the ADR itself, is in review in #201; PR 2a-1 done on `feat/lms-provider-protocol`, in review; PR 2a-2 done on `feat/assess-on-lms-protocol`, stacked on #203; PR 2a-3 done on `feat/assess-schema-branch`, stacked on #205, 2a-3.9 waiting for the coordinator; PR 2a-4 done on `feat/lms-moodle-adapter-out`, stacked on #206: the LMS boundary baseline is empty)
**Branch**: `docs/assess-lms-separation-adr` (phase 4), `feat/lms-provider-protocol` (2a-1, stacked on #201). Each later PR has its own branch, listed in the PR split below.
**Created**: 2026-11-11T18:54:53Z
**Updated**: 2026-11-12T12:00:00Z

## Traceability

**Source**: the 2026-11-08 external review of the LMS boundary (`lms-boundary-analysis.md`, `separation-handoff.md` §3, §4, §6, §7), and the guard `vektra-shared/tests/test_lms_boundary.py` with its baseline `lms_boundary_baseline.toml` (26 entries)
**Source Type**: architecture (ARCH-039, ARCH-065, ARCH-073)

## References

### Requirements
- REQ-067: Server-authoritative discussion lifecycle (the authority stays in Vektra) @.s2s/requirements.md
- REQ-070, REQ-086: Client-less, sub-minute scheduling @.s2s/requirements.md
- REQ-079: Setup check for the dedicated service account (split into generic and provider items) @.s2s/requirements.md
- REQ-095: Every id the plugin sends is re-resolved by Vektra @.s2s/requirements.md
- BR-006(d): Cohort-wide override reads @.s2s/requirements.md
- BR-011, NFR-017: Disclosure boundary, verified on bytes @.s2s/requirements.md
- BR-012: Server-side attempt selection @.s2s/requirements.md

### Architecture
- ARCH-039: ProviderRegistry; an 11th Protocol and an `lms` slot @.s2s/architecture.md
- ARCH-065: Vektra-authoritative split (amended) @.s2s/architecture.md
- ARCH-069: Capture-time minimisation and the typed ContextBundle @.s2s/architecture.md
- ARCH-070: Capability matrix @.s2s/architecture.md
- ARCH-073: Top-level `vektra_assess` with stable names (amended) @.s2s/architecture.md

### Decisions
- ADR-0033: LMS provider Protocol (this plan's design) @.s2s/decisions/ADR-0033-lms-provider-protocol.md
- ADR-0027: Assessment discussion as top-level `vektra_assess` (amended by ADR-0033) @.s2s/decisions/ADR-0027-assessment-discussion-as-a-top-level-vektra-assess.md
- ADR-0009: Namespace isolation via RLS (deviation recorded in ADR-0033 D9) @.s2s/decisions/ADR-0009-namespace-isolation-rls.md
- ADR-0005: Module boundary enforcement @.s2s/decisions/ADR-0005-module-boundary-enforcement.md
- ADR-0002: Repository split criteria (OD-5) @.s2s/decisions/ADR-0002-repo-split-criteria.md
- ADR-0032: LLM context boundary (the review-HTML profile, D4) @.s2s/decisions/ADR-0032-llm-context-boundary-capture-time-minimisation-typ.md

### Dependencies
- The author's remaining waves are closed first (`separation-handoff.md` §4: the separation is done in one pass, on a surface that no longer moves). Plan 13 can stay `in_progress` on its environment blockers; it does not block this plan.
- #199 and #200 are merged. Re-run the measurement predicates of ADR-0033 §7 on the merged `develop` before PR 2a-1, and record the numbers in `## Notes`.
- ADR-0033 decisions, all taken 2026-11-11:
  - OD-1 = (a): no assess data survives; squash; both ratchets close in 2a. It is valid only while no release ships assess and no plugin tag exists, which task 2a-3.0 re-checks.
  - OD-2 to OD-6: the defaults, decided after the owner consulted the coordinator.

## Overview

The vertical keeps the authority: it decides, it re-resolves every id it is given, and it schedules. It stops containing the transport. `vektra-assess` comes to depend only on an `LmsProvider` Protocol declared in `vektra_shared`. The Moodle web-service client, its two version model trees, breaker, probes and service-account checks become a provider. That provider is first staged in this repository outside the platform's perimeter (`adapters/vektra-lms-moodle/`), and then moved to its home repository.

Done is measured, not declared, and it is measured by the guard's baseline, which may only shrink:

- **ratchet 1** (phase 2a): the 8 non-migration entries are gone;
- **ratchet 2** (in 2a, PR 2a-3, because OD-1 = yes is decided): the 18 migration entries are gone and the file is empty.

## Design Notes

- **Why the provider cannot stay in `vektra_assess/moodle/` at the end of 2a.** The `vektra-assess` directory entry clears only when nothing under `vektra-assess/src` and `vektra-assess/ui/src` names an LMS. So the in-tree staging place is `adapters/`, which is not a uv workspace member and is not in the default image (ADR-0033 D6). Two guard additions keep that honest (tasks 2a-4b.9 and 2a-4b.10).
- **Every PR lowers the baseline as it goes.** The guard fails on a count that falls without the entry being lowered. So each PR that removes mentions edits `vektra-shared/tests/lms_boundary_baseline.toml` in the same commit: it lowers a count, or deletes an entry. No PR adds an entry. If a step seems to need one, stop: it is an ADR question (the stop rules of INDEX-ASSESS).
- **A dormant gap the move would arm**: once the client's loggers leave the `vektra_assess.*` namespace, they lose the strict redaction of `log_safety` (the hard-coded `ASSESS_LOGGER_PREFIX`). The registration API lands in 2a-1, and a test proves that a provider logger is redacted, before any code moves.
- **The vertical's gate logic does not move.** `MoodleStartSource` becomes `LmsStartSource` and keeps every derivation: the grace rule, the `own_close_at` rule, `fully_graded`, the release rule of BR-010. It reads neutral types instead of Moodle ones. A test is "moved" only when it tests transport; a test of a gate decision stays in the vertical, on the fake provider.
- **Composition by entry point**: `VEKTRA_ASSESS_LMS_PROVIDER=<name>` and the group `vektra.lms_providers`. Unset with `VEKTRA_ASSESS_ENABLED=true` means the worker refuses to start, with a remediation, and the setup item `lms_provider_registered` is `MISSING`.
- **The size cap.** About 100 files per PR (the review bot's cap). A PR that is mostly renames may exceed it (product owner, 2026-11-11): 2a-4 carries the 328 recorded fixtures in a rename-only commit, proved content-identical by `git diff --find-renames=100% --stat` (0 files modified, only renamed). CodeRabbit reviews 2a-2 only, so the cap binds that PR first.

## PR split

Regrouped by the product owner on 2026-11-11 (relayed by the coordinator): phase 2a is **4 PRs**. The task IDs are unchanged; only their grouping into PRs changed. 2a-2 is the former 2a-2a and 2a-2b together, and 2a-4 is the former 2a-4a and 2a-4b together.

| PR | Branch | Phase | Tasks | Content | Est. files | Baseline effect |
|---|---|---|---|---|---:|---|
| 2a-1 | `feat/lms-provider-protocol` | 2a | 2a-1.1 to 2a-1.11 | Protocol, neutral types, registry slot, `log_safety` registration, entry-point loader, conformance kit skeleton, `FakeLmsProvider`; the safeguard/locality startup rule (2a-1.11, product owner, 2026-11-11). No other behaviour change. | ~30 | none |
| 2a-2 | `refactor/assess-lms-start-source` | 2a | 2a-2a.\* and 2a-2b.\* | The vertical consumes the Protocol: `MoodleLmsProvider` adapter class over the existing readers (still in `moodle/`), `LmsStartSource`, capture on neutral types, setup-check split, `QuestionState` and `QuestionKind`, the review-HTML profile, unit tests; **and**, in the same PR, the integration and e2e suites on `FakeLmsProvider`, the old ports deleted, the static `site_id` predicate test (D9) | ~105 | none (the directory entry still covers `moodle/`) |
| 2a-3 | `feat/assess-schema-branch` | 2a (closes ratchet 2, OD-1) | 2a-3.\* | The assess Alembic branch, gated by `VEKTRA_ASSESS_ENABLED`; squash with the neutral names; the in-place renames of D5; the squash-equivalence test; docs | ~70 | the 18 migration entries removed; `assess_wiring.py` lowered |
| 2a-4 | `feat/lms-moodle-adapter-out` (planned as `refactor/lms-moodle-adapter`) | 2a | 2a-4a.\* and 2a-4b.\* | The 328 recorded fixtures moved in a rename-only commit (proved by `git diff --find-renames=100% --stat`), then `vektra_assess/moodle/` to `adapters/vektra-lms-moodle/src/vektra_lms_moodle/`; its tests; entry-point composition in `vektra-app`; wording in the 5 comment-only platform files; settings move; opt-in image build argument; guard and image-boundary additions | ~420, mostly renames | **the directory entry and the 7 platform entries removed (ratchet 1)** |
| 2b-1 | `chore/lms-moodle-adapter-out` | 2b | 2b.\* | `adapters/` removed; the image installs the pinned wheel (OD-6) | ~10 + `adapters/` | none |
| 3-1 | `refactor/testkit` | 3, **in parallel with 2a-2** | 3.1 | `vektra-testkit` dev-only distribution: `testing_recorders.py` and the conformance kit | ~30 | none |
| 3-2 | `feat/assess-api-rls` | 3, **in parallel with 2a-4** | 3.2 | RLS for `vektra_assess_api` on the site-scoped tables (D9) | ~15 | none |
| ~~3-3~~ | ~~`chore/assess-shared-hygiene`~~ | 3 | 3.3 to 3.5 | **Done in #199**: DEBT-042, the CHANGELOG `ensure_ascii` entry, the proven GRANT recipe with "up to the current head" | 0 | none |

**Review.** CodeRabbit gets one round, on 2a-2 only. Every other PR gets a local `/code-review` and green CI. The ~100-file cap may be exceeded by a PR that is mostly renames (2a-4), whose body carries the rename proof.

**Fallback only.** This applies if task 2a-3.0 finds the OD-1 validity condition false: a release ships assess or the plugin is tagged. Under **OD-1 = no**, 2a-3 shrinks to a `git mv` of the 36 revisions plus gating, with no squash and no renames of stored names. The renames then need a rename revision, which would add a migration entry, and that is not allowed (see Design Notes). So OD-1 = no means 2a cannot close ratchet 1 without the product owner first accepting one dated, temporary entry. Stop and ask before 2a-3 in that case.

## Tasks

### Phase 4: the decision (this PR, `docs/assess-lms-separation-adr`)
- [x] Measure the real LMS surface on `origin/develop` `f95c148b` with the guard's matcher and `rg`, file:line references included (ADR-0033 §1, §7)
- [x] Write ADR-0033, with the open decisions OD-1 to OD-6
- [x] Annotate ADR-0027 "Amended by ADR-0033"
- [x] Add the Separation section to `INDEX-ASSESS.md` and a one-line pointer to `.s2s/CONTEXT.md`
- [x] Product owner answers OD-1 to OD-6 (2026-11-11); ADR-0033 set to `accepted`, with the answers recorded in its section 6
- [ ] `.s2s/architecture.md`: ARCH-039 says 11 Protocols and lists the `lms` slot; the ARCH-065 and ARCH-073 rows point at ADR-0033 (on acceptance)

### Phase 2a, PR 2a-1: Protocol and neutral types
- [x] 2a-1.1 `vektra-shared/src/vektra_shared/lms/__init__.py`, `types.py`: `Reading`, `Readable`, `Unreadable`, `UnreadableReason`, `ShapeEvidence` (moved from `vektra-assess/src/vektra_assess/readable.py`, with the field `wsfunction` renamed `call`), and the neutral types of ADR-0033 D2. `readable.py` becomes a re-export until 2a-2b deletes it.
- [x] 2a-1.2 `vektra-shared/src/vektra_shared/lms/protocol.py`: `LmsProvider` (23 members, `@runtime_checkable`), `StaffRole`, `QuestionState`, `QuestionKind`, `OverdueMode`, `GradeMethod`, `HtmlExtractionProfile`, `ReviewHtml`, `SetupItem`, `CapabilityResult`, `StartupProblem`
- [x] 2a-1.3 `vektra-shared/src/vektra_shared/protocols.py`: re-export `LmsProvider`, docstring "10 Protocol interfaces" becomes 11; `registry.py` docstring lists `"lms"`
- [x] 2a-1.4 `vektra-shared/src/vektra_shared/log_safety.py`: `register_strict_logger_prefix(prefix)`, replacing the hard-coded `ASSESS_LOGGER_PREFIX` (lines 31, 113, 271); `vektra_assess/__init__.py` registers `"vektra_assess"`; test: a logger under a newly registered prefix gets the strict redaction, and one under an unregistered prefix does not (`vektra-shared/tests/test_log_safety.py`, `vektra-assess/tests/test_log_safety_wiring.py`)
- [x] 2a-1.5 `vektra-app/src/vektra_app/lms_providers.py`: `load_lms_provider(name)` over `importlib.metadata.entry_points(group="vektra.lms_providers")`, with a refusal listing the available names; not wired yet
- [x] 2a-1.6 `vektra-assess/src/vektra_assess/config.py`: `lms_provider: str | None` (`VEKTRA_ASSESS_LMS_PROVIDER`); `docs/reference/configuration.md`
- [x] 2a-1.7 `vektra-shared/tests/lms_conformance/`: provider-parametrised suite covering the Reading discipline, the re-resolution refusal, canary absence, no secrets in logs, and coverage of the declared setup items (ADR-0033 D10); `conftest.py` takes a provider factory and a recorded transport
- [x] 2a-1.8 `vektra-assess/tests/support/fake_lms.py`: `FakeLmsProvider` (neutral values, scriptable `Unreadable` per member, call recorder, `__vektra_test_double__` marker); the conformance kit runs green on it
- [x] 2a-1.9 `pyproject.toml` import-linter: `vektra_shared.lms` imports no component and no `httpx`
- [x] 2a-1.10 `make lint`, `make test`, guard green, baseline unchanged
- [x] 2a-1.11 (added by the product owner, 2026-11-11) `VEKTRA_ASSESS_SAFEGUARD_MODE=none` follows the model's locality: accepted with `VEKTRA_ENV=development` and any `VEKTRA_ASSESS_PROVIDER_LOCALITY`; outside development accepted with `onprem`, refused at startup with `cloud` (the default), in the refusal style of the other development-only switches (`StartupValidationError` at step `assess_llm_check`, `vektra-app/src/vektra_app/assess_llm.py`), the message saying that a cloud model requires the safeguard and how to fix it. Failing-first tests, one per row, plus `presidio` still the default (`vektra-app/tests/test_assess_llm.py`); the dev overlay still accepted (`test_compose_assess_profile.py`). Documented in `.env.example`, `docs/reference/configuration.md` and `docs/guides/operators/assess-deployment.md`

### Phase 2a, PR 2a-2 (part a): the vertical on the Protocol
- [x] 2a-2a.1 `vektra-assess/src/vektra_assess/moodle/provider.py`: `MoodleLmsProvider`, implementing `LmsProvider` over `moodle/readers/*`, `moodle/probes.py` and the Moodle items of `moodle/setup_check.py`; normalises `overduehandling`, `grademethod`, the attempt and question states, and `qtype` into the neutral enums. The conformance kit runs on it with the recorded fixtures.
- [x] 2a-2a.2 `vektra-assess/src/vektra_assess/worker/start_source.py`: `MoodleStartSource` becomes `LmsStartSource(provider: LmsProvider, …)`; same methods (`gate_facts`, `capture`, `enrolled`, `graders`, `teacher_facts`, `close_facts`, `forget`); no import from `vektra_assess.moodle`
- [x] 2a-2a.3 `vektra-assess/src/vektra_assess/worker/preflight.py`: `MoodleGateFacts` becomes `LmsGateFacts`; `StartSource` docstring; `worker/render.py`, `worker/enrolment.py`, `worker/teacher_gate.py`, `worker/transcript_gen.py`, `transcript/facts.py` follow
- [x] 2a-2a.4 `vektra-assess/src/vektra_assess/context/capture/capture.py`, `selection.py`: import the neutral types; `CaptureReaders` is satisfied by an `LmsProvider` bound to a site (`context/capture/readers.py` becomes a 20-line binding or is deleted)
- [x] 2a-2a.5 `vektra-assess/src/vektra_assess/context/qtypes.py`: `FINAL_STATES` and `NO_MARK_FINAL_STATES` move into `MoodleLmsProvider`'s normalisation; the vertical uses `QuestionState`, and `UNKNOWN` fails closed (test: an unknown provider state is never final)
- [x] 2a-2a.6 setup check split: new `vektra-assess/src/vektra_assess/setup/{__init__,registry,runner,items}.py` from the generic part of `moodle/setup_check.py` (ADR-0033 D3); `worker/setup_check.py`, `worker/setup_items.py`, `worker/parser_client.py` import from `vektra_assess.setup`; the provider's declared items that are not returned become `MISSING/not_returned`; new item `lms_provider_registered`
- [x] 2a-2a.7 `vektra-assess/src/vektra_assess/parser_sidecar/html_review.py`, `parser_protocol.py`, `worker/parser_client.py`: the `review_item` op takes an `HtmlExtractionProfile`; `REVIEW_BLOCKS` and `DROP_CLASSES` move into `MoodleLmsProvider`; the sidecar refuses a request without a profile; `tests/test_review_block_extraction.py` passes the Moodle profile explicitly
- [x] 2a-2a.8 `vektra-assess/src/vektra_assess/capabilities.py`: unchanged rules; docstrings neutral; `FLAGS` documented as Vektra's contract with every provider
- [x] 2a-2a.9 `pyproject.toml` import-linter contract (c): only `vektra_assess.worker` and `vektra_assess.context.capture` reference an `LmsProvider` instance; `vektra_assess` (minus `moodle`) must not import `vektra_assess.moodle` (the `ignore_imports` for capture are removed)
- [x] 2a-2a.10 unit tests moved to the fake, among them `test_start_source_cache.py`, `test_teacher_gate.py`, `test_context_capture.py`, `test_context_selection.py`, `test_capabilities.py`, `test_idor.py`, `test_render_moodle_reasons.py` (renamed in 2a-3), `test_parser_client.py`, `test_boundaries.py`
- [x] 2a-2a.11 `make lint`, `make test`, `make test-integration`; guard green, baseline unchanged (the moved mentions stay inside the directory entry)

### Phase 2a, PR 2a-2 (part b, same PR): the integration suites on the fake provider
- [x] 2a-2b.1 `vektra-assess/tests/integration/`: `test_session_preflight*.py`, `test_session_start_guard.py`, `test_session_state.py`, `test_session_get.py`, `test_session_burst_inprocess.py`, `test_chaos_parser_kill.py`, `test_snapshot_store_db.py`, `test_transcript_generation.py`, `test_teacher_config_format.py`, `test_setup_check_{handler,render,run,worker_items}.py`, `test_ui_e2e.py`, `test_probes_db.py`, and `support/{sessions,teacher_ops}.py` on `FakeLmsProvider`
- [x] 2a-2b.2 `tests/chaos/assess/worker_main.py`, `tests/nfr/test_assess_burst.py`, `scripts/assess_burst.py`: the fake is loaded through a development-only entry point; a production registry refuses it (test)
- [x] 2a-2b.3 D9 backstop: `vektra-assess/tests/test_site_predicate_static.py` parses every SQL text in `vektra_assess.api` and fails when a statement reads a site-scoped `assess_*` table without a `site_id` predicate; a planted route without the predicate turns it red (positive control)
- [x] 2a-2b.4 delete `vektra_assess/readable.py` (re-export) and `context/capture/readers.py` if still present; `make test-integration` green with 0 skipped (the existing JUnit-skip CI steps)

### Phase 2a, PR 2a-3: the assess schema branch and the neutral names (OD-1 = yes, decided 2026-11-11)
- [x] 2a-3.0 **validity check, at the moment of the cut and before any other 2a-3 task.** Both facts must hold, and each must be measured:
  1. no published release ships `vektra-assess`: no `vektra-assess` package or `assess_*` revision in any `v*` tag, checked with `git tag -l 'v*'` and then `git ls-tree -r <tag> -- vektra-assess migrations/versions | rg assess` for each tag;
  2. `vektra-moodle-discuss` has no tag: `gh api repos/vektralabs/vektra-moodle-discuss/tags` returns `[]`.

  Record both results in `## Notes`. If either is false, **stop**: OD-1 is decided again by the owner, and 2a-3 follows the fallback above.
- [x] 2a-3.1 before deleting anything: run 0001 to 0044 on an empty cluster and commit the normalised schema golden `vektra-assess/tests/golden/assess_schema_head.sql` (`pg_dump --schema-only`, normalised), with the D5 rename map applied, plus the effective-privilege dump of the grant matrix
- [x] 2a-3.2 `vektra-assess/src/vektra_assess/migrations/versions/a0001_assess_base.py`: one revision, `branch_labels=("assess",)`, `depends_on="0008"`, creating the head schema with `lms_site_uuid`, `lms_origin` (CHECK `ck_assess_sites_lms_origin`) and `lms_release`; the CREATEROLE error message names all three roles; delete `migrations/versions/0009…0044` (36 files)
- [x] 2a-3.3 `alembic.ini` `version_locations`; `migrations/env.py`; branch label `platform` on `migrations/versions/0001_initial_schema.py`; `docker/entrypoint.sh` (lines 21, 33) runs `alembic upgrade platform@head`, and `assess@head` only when `VEKTRA_ASSESS_ENABLED=true`
- [x] 2a-3.4 test `vektra-assess/tests/integration/test_schema_branch.py`: the squash equals the golden (schema and effective privileges); with assess off, a clean cluster has 0 `assess_*` tables, 0 `vektra_assess_*` roles and 0 assess functions after the entrypoint ran (counted); with assess on, a cluster without CREATEROLE fails with the three-role message, and the platform with assess off migrates without CREATEROLE (the review's managed-Postgres case)
- [x] 2a-3.5 renames in place (ADR-0033 D5):
  - `db/models.py`, `db/grants.py`, `ui/origins.py`, `vektra-app/src/vektra_app/assess_wiring.py`, `capabilities.py`, `api/setup_check.py` (JSON `lms_release`)
  - `context/snapshot.py` (`lms_release`, `rubric_source` `"lms"`), `teacher/schemas.py`, `teacher/validation.py`, `api/teacher_config.py`, `api/teacher_ops.py`
  - `worker/render.py` (`lms_reasons`), `worker/start_source.py` (`lms_rubric`), `worker/start_guard.py` and `delivery/notify.py` (`lms_unreachable`)
  - reason codes `lms_rubric_missing`, `lms_facts_unreadable`, `lms_origin_invalid`, `lms_origin_not_https`, `ui_origin_equals_lms_origin`
  - `vektra_assess/errors.py` messages and remediations
  - `ui/src/main.ts`, `ui/src/bridge.ts` (`parentOrigin`) and `ui/router.py`
  - the comment-only mentions in the vertical, for example `gates.py:3,97`, `audit.py:15`, `llm/strings/*.yaml:1`, `llm/shipped.py:6`
- [x] 2a-3.6 tests that name revisions are rewritten against the branch: `test_migration_0009.py`, `test_migration_0033.py`, `test_schema_0010_0013.py`, `test_schema_0018_jobs.py`, `test_grants.py`, `integration/test_migration_managed_postgres.py`, `test_render_moodle_reasons.py` (renamed `test_render_lms_reasons.py`)
- [x] 2a-3.7 docs:
  - `docs/guides/assess-moodle-service-account.md:111` (provisioning SQL), `docs/guides/operators/assess-framing.md`, `assess-deployment.md`
  - `docs/reference/{api,configuration,error-codes}.md`; the upgrade note: "re-create the development assess schema"
  - CHANGELOG `[Unreleased]`
- [x] 2a-3.8 baseline: delete the 18 `migrations/versions/*` entries and lower `assess_wiring.py`, in the same commit; guard green
- [ ] 2a-3.9 the databases. What is known:
  - the only database with assess data is the local F1 stack's Postgres (the `vektra-f1-postgres` container on the developer machine);
  - testcontainers are ephemeral;
  - the deploy VM is believed to run v0.8.0, which has no assess.

  At the moment of the 2a-3 cut:
  - **Local F1 database**: recreate its assess schema from `assess@head`. This is destructive, so stop and ask first, and run it **only after an explicit go from the owner**.
  - **Deploy VM**: a request to the internal-ops session, which operates it. This session does not touch it. The request is for a **read-only** check of whether any `assess_*` tables exist there. Only if they do does it become a recreate request to that session, again after the owner's go.

  Record in `## Notes` the result of the check, and what was recreated, by whom and when.

### Phase 2a, PR 2a-4 (part a): the recorded fixtures (rename-only commit)
- [x] 2a-4a.1 `git mv vektra-assess/tests/fixtures/moodle_ws adapters/vektra-lms-moodle/tests/fixtures/moodle_ws` (328 files), and the fixture path constant in `vektra-assess/tests/assess_moodle_support.py`
- [x] 2a-4a.2 proof in the PR body: `git diff --find-renames=100% --stat origin/develop...HEAD` shows 328 renames and 1 modified file, and `make test` is green

### Phase 2a, PR 2a-4 (part b, same PR): the adapter leaves the platform (ratchet 1)
- [x] 2a-4b.1 `adapters/vektra-lms-moodle/pyproject.toml` (distribution `vektra-lms-moodle`, depends on `vektra-shared` only, entry point `vektra.lms_providers: moodle = vektra_lms_moodle:build_provider`); `git mv vektra-assess/src/vektra_assess/moodle adapters/vektra-lms-moodle/src/vektra_lms_moodle`; imports rewritten
- [x] 2a-4b.2 provider settings class `vektra_lms_moodle/config.py`: the 11 settings of `vektra_assess/config.py:236-295` move here, with the names per OD-4; `startup_problems(env)` replaces `vektra-app/src/vektra_app/assess_worker.py:229-236`
- [x] 2a-4b.3 the provider's tests move to `adapters/vektra-lms-moodle/tests/`:
  - `vektra-assess/tests/test_moodle_*.py` (9) and `integration/test_moodle_{live,setup_live}.py`
  - `test_review_block_extraction.py`, `assess_moodle_support.py`, `test_assess_record_ws.py`
  - the live suites `integration/test_{context,idor,req073}_live.py` (they run against a stack with the provider installed)
- [x] 2a-4b.4 `scripts/assess_gen_ws_models.py` and `scripts/assess_record_ws.py` move to `adapters/vektra-lms-moodle/scripts/`; `.github/workflows/assess-moodle-contract.yml` paths updated; a CI job (path-unfiltered, like `test-structure`) runs the adapter's tests and the conformance kit
- [x] 2a-4b.5 `vektra-app/src/vektra_app/assess_worker.py`: `_ws_client` and the `MoodleStartSource` construction (lines 319-370, 685, 870) are replaced by `load_lms_provider(settings.lms_provider)` and `LmsStartSource(provider, …)`; one provider instance per worker (one rate cap, one breaker registry, as today)
- [x] 2a-4b.6 root `pyproject.toml`: dependency group `lms-moodle` with `vektra-lms-moodle = { path = "adapters/vektra-lms-moodle", editable = true }`; not a workspace member; `Makefile` and `uv.lock` updated
- [x] 2a-4b.7 `Dockerfile`: `ARG INSTALL_LMS_PROVIDER=` (empty by default) installs the adapter only when set, on the `INSTALL_UNSTRUCTURED` model; `docker-compose.assess.yml` and `docker-compose.assess.dev.yml` build with `INSTALL_LMS_PROVIDER=moodle`
- [x] 2a-4b.8 wording, the 5 comment-only platform files: `vektra-shared/src/vektra_shared/auth.py:80`, `errors.py:175`, `netguard.py:3,5,13,95` (a generic egress guard, ADR-0033 D8), `vektra-app/src/vektra_app/main.py:694`, `vektra-admin/src/vektra_admin/templates/keys.html:19`
- [x] 2a-4b.9 guard addition in `vektra-shared/tests/test_lms_boundary.py`: no workspace member's `pyproject.toml` depends on a `vektra-lms-*` distribution, with a planted dependency turning it red
- [x] 2a-4b.10 `vektra-shared/tests/test_image_content_boundary.py`: the default image contains no `vektra_lms_*` package and no `adapters/` file; with `INSTALL_LMS_PROVIDER=moodle` it does (positive control)
- [x] 2a-4b.11 import-linter: `vektra_lms_*` is forbidden to every platform package
- [x] 2a-4b.13 (added after the #205 review) `vektra_assess.keys.enforce_key_matrix` loads the secrets the provider declares (`LmsProvider.required_secrets`) instead of the fixed `assess_ws_tokens` spec: today the worker binds every LMS-reading handler only when those declared names were loaded (`assess_worker._bound_lms`), but the matrix still loads the Moodle name, so a provider declaring another secret could never start
- [x] 2a-4b.12 baseline: delete the `vektra-assess` directory entry and the 7 platform file entries in the same commit; guard green; `rg -il moodle vektra-assess/src vektra-assess/ui/src vektra-shared/src vektra-app/src vektra-admin/src` returns nothing (positive control: the same predicate on `adapters/` returns its files)

### Phase 2b: the adapter's home repository
- [ ] 2b.1 request to the plugin session (see Notes): host `vektra-lms-moodle` in the repository chosen by OD-5, with Python CI, the conformance run and a wheel release
- [ ] 2b.2 after the first published wheel: `.github/workflows/publish.yml` builds the image variant of OD-6, installing the wheel by pinned version and hash; remove `adapters/` and the `lms-moodle` dependency group
- [ ] 2b.3 the image-boundary test (2a-4b.10) asserts the variant's wheel hash

### Phase 3: the costs an operator feels
- [x] 3.1 (in parallel with 2a-2) `vektra-testkit` (new dev-only distribution, `testkit/` at the root, dev dependency group): `vektra-shared/src/vektra_shared/testing_recorders.py` and `vektra-shared/tests/lms_conformance/` move there; the `ProviderRegistry` refusal of test doubles stays; `test_image_content_boundary.py` asserts the production image has no `testing_recorders`
- [ ] 3.2 (in parallel with 2a-4) RLS for `vektra_assess_api` (ADR-0033 D9), a new revision of the assess branch:
  - policies on the site-scoped tables the API reads;
  - `SET LOCAL app.assess_site_id` from the key's site binding at the start of each request transaction (`vektra-assess/src/vektra_assess/api/deps.py`);
  - test: an API connection bound to site A, with the route's `site_id` predicate removed, reads 0 rows of site B
- [x] 3.3 `.s2s/BACKLOG.md`: DEBT item "unify `vektra_shared.netguard` with `vektra_learn.api._resolve_and_validate_url`" (owner: the author). Done in #199 (DEBT-042).
- [x] 3.4 CHANGELOG `Changed`: JSON logs are written with `ensure_ascii=False` (every deployment). Done in #199 (the CHANGELOG `ensure_ascii` entry).
- [x] 3.5 managed-Postgres upgrade note: prove the positive case (roles pre-created + the documented GRANT + assess migrated to head) on a cluster without superuser, then write it; "up to the current head" instead of a hand-written revision number. Done in #199 (the proven GRANT recipe, and "up to the current head").
- [ ] 3.6 fallback only, if task 2a-3.0 forces OD-1 = no: the second ratchet (squash after every retained database has been migrated; see ADR-0033 D7)

## State & Data Lifecycle

| State element | Created by | Updated by | Invalidated/Deleted by |
|---|---|---|---|
| `lms` entries in `ProviderRegistry` | worker and API startup (2a-4b.5, `load_lms_provider`) | never (one instance per process) | process exit; `aclose()` on shutdown (`worker/shutdown.py`) |
| `alembic_version` rows of the `assess` branch | `alembic upgrade assess@head` (2a-3.3) | each assess revision | `alembic downgrade assess@base`; re-creation of a dev schema (2a-3.9) |
| `vektra-assess/tests/golden/assess_schema_head.sql` | 2a-3.1, from the old chain | a new assess revision regenerates it (the test says how) | never deleted; it is the equivalence oracle |
| `LmsStartSource` resolution cache (`_BoundedCache`, size and TTL bounded) | `gate_facts` / `_resolve` | `put` | `pop` after `enrolled` and `close_facts`, TTL, size bound (unchanged from today) |
| the assess schema of the local F1 Postgres (`vektra-f1-postgres`) | the old chain 0009 to 0044 | | re-created in 2a-3.9 after the owner's go (OD-1); the deploy VM only on internal-ops' read-only check finding `assess_*` tables |
| `HtmlExtractionProfile` | provider constant, versioned (`profile_version`) | a provider release | not stored; the snapshot header records `profile_version` for provenance |

## Acceptance Criteria

Each criterion is checked against the guard's data file. A status line or a green job is not evidence on its own: the counts are.

**Phase 2a (ratchet 1)**
- [x] `vektra-shared/tests/lms_boundary_baseline.toml` contains no `directory` entry and no `file` entry outside `migrations/`. The 8 entries are gone: `vektra-assess`, `vektra-shared/src/vektra_shared/{auth,errors,netguard}.py`, `vektra-app/src/vektra_app/{assess_wiring,assess_worker,main}.py`, `vektra-admin/src/vektra_admin/templates/keys.html`.
- [ ] `uv run pytest vektra-shared/tests/test_lms_boundary.py` is green on `develop` after the last 2a PR.
- [ ] No 2a PR added a baseline entry: `git log -p origin/develop -- vektra-shared/tests/lms_boundary_baseline.toml` shows only `-` lines.
- [x] `rg -il moodle vektra-assess/src vektra-assess/ui/src` returns 0 files, and the same predicate on `adapters/vektra-lms-moodle/src` returns more than 0 (the predicate is not dead).
- [x] `vektra_assess` imports no provider: import-linter green with the new contracts, and `python -c "import vektra_assess, sys; assert not [m for m in sys.modules if m.startswith('vektra_lms')]"`.
- [x] The conformance kit is green on `FakeLmsProvider` and on `MoodleLmsProvider` with the 328 recorded fixtures (4.3 and 5.2), canary sinks included.
- [x] The vertical's suites (unit, integration, chaos) use no Moodle fixture: `rg -l moodle_ws vektra-assess/tests tests/` returns nothing.
- [ ] With `VEKTRA_ASSESS_ENABLED=true` and `VEKTRA_ASSESS_LMS_PROVIDER` unset, the worker refuses to start, and the setup item `lms_provider_registered` is `MISSING`.
- [x] The default image contains no `vektra_lms_*` package (image content test), and the `INSTALL_LMS_PROVIDER=moodle` build does.
- [x] Ratchet 2 also closes in 2a (OD-1 = yes, decided): the baseline file has no entries, and the squash-equivalence test is green.

**Phase 2b**
- [ ] `adapters/` does not exist in this repository, and the image variant installs a wheel pinned by hash.

**Phase 3 (ratchet 2, if not already closed)**
- [x] `lms_boundary_baseline.toml` contains no entries, and the guard is green.
- [x] With `VEKTRA_ASSESS_ENABLED=false`, a clean cluster has 0 `assess_*` tables, 0 `vektra_assess_*` roles and 0 assess functions after `docker/entrypoint.sh` ran (counted with `\dt assess_*`, `\du vektra_assess*`, `\df`), and the platform migrates without CREATEROLE. Met in 2a-3: `integration/test_schema_branch.py::test_a_*` (counted in SQL) and `integration/test_migration_managed_postgres.py::test_a_0_*`.
- [x] The production image contains no `testing_recorders` module.
- [ ] The RLS test reads 0 rows of site B from a connection bound to site A, with the route predicate removed.

## Testing Approach

- **Guard and structure**:
  - `vektra-shared/tests/test_lms_boundary.py` runs on every PR in `test-structure`, which has no path filter;
  - the two guard additions (2a-4b.9, 2a-4b.10) have planted positive controls;
  - the import-linter contracts.
- **Conformance kit**: one suite, run twice. Run on the fake, it proves the kit is not vacuous. Run on the Moodle provider with recorded fixtures, it proves the provider. A deliberately broken provider that raises, or that returns the hinted course, must turn the kit red (negative controls).
- **Vertical**: the gate-order test, IDOR (INT-001), the canary suite (NFR-017) and the snapshot tests run on `FakeLmsProvider`, with markers planted in every neutral field that may carry content.
- **Schema**: the squash equivalence (schema and effective privileges) against the golden taken from the old chain; the assess-off counts; the managed-Postgres case.
- **Live**: the provider's live suites stay BLOCKED where the dev Moodle environments are unavailable, and are reported as BLOCKED, never as passed.

### Test Infrastructure

- **Required**:
  - Postgres (testcontainers), for the schema, grant and RLS tests;
  - Docker, for the image content tests;
  - the 328 recorded fixtures, for the provider's conformance run;
  - the dev Moodle 4.3 and 5.2 environments (in `vektra-moodle`), for the live suites only.
- **Provided by**: the existing `make test-integration` and the `test-structure` job; 2a-1.7 (the kit); 2a-3.1 (the golden); 2a-4b.4 (the adapter CI job).
- **False-pass guard**:
  - the JUnit skip-count steps already in CI fail a job that skipped a suite;
  - the conformance kit asserts it ran at least one case per Protocol member;
  - the squash test fails on an empty schema ("the migrations did not run"), as the grant matrix test already does.

## Integration Notes

- **Plugin side** (requests for the plugin session; never edited from here):
  1. P3 is written against `rubric_source: "lms"` and the reason codes `lms_rubric_missing` and `lms_facts_unreadable` (OD-3), and, if the site-admin page shows it, against the setup report field `lms_release`.
  2. No change for the kept wire names `cmid`, `loginas`, `realuser_id` (OD-2, decided).
  3. OD-5 (decided): a top-level `provider/` tree holding `vektra-lms-moodle`, excluded from the plugin release zip (the release script's content check gains an assertion), a Python CI job running the provider's tests and the conformance kit against a pinned `vektra-stack`, and a wheel release.
  4. Check the plugin's docs and settings help for `moodle_origin` and `moodle_site_uuid` provisioning text (they become `lms_origin` and `lms_site_uuid` in 2a-3).
- **Internal ops (deploy VM)**: a request, not a task of this repository. At the 2a-3 cut, check read-only whether any `assess_*` tables exist. Only if they do, recreate them, after the owner's go (task 2a-3.9).
- **Operators**: the local F1 assess schema is re-created in 2a-3 (OD-1); the settings move to the provider as `VEKTRA_LMS_MOODLE_*` with no alias (OD-4); the image needs `INSTALL_LMS_PROVIDER=moodle` (2a), then the published `<version>-lms-moodle` tag (2b, OD-6).
- **`vektra-learn`** is unaffected. `enrolment` and `course_staff` are available to it later through the same Protocol.

## Notes

- 2026-11-11: ADR-0033 written (proposed). Measured on `origin/develop` `f95c148b` with #199 and #200 open; #199 changes no baseline file's mention count (checked on its diff). Guard-exact counts:
  - `vektra-assess`: 96 files and 551 mentions (35 files and 247 mentions in `moodle/`);
  - the platform: 7 files and 31 mentions;
  - migrations: 18 files and 37 mentions.
- 2026-11-11, decisions of the product owner (ADR-0033 §6, now `accepted`):
  - OD-1 = (a), no assess data survives ("they are test data, recreate"): squash into the `assess` branch with `lms_*` names, and both ratchets close in 2a. It is valid only while no release ships assess and no plugin tag exists, which task 2a-3.0 checks at the cut. Only the local F1 Postgres is known to hold assess data, and it is recreated after the owner's explicit go. For the deploy VM (believed to run v0.8.0, without assess), the internal-ops session is asked for a read-only check, and a recreate only if `assess_*` tables exist.
  - OD-2 to OD-6: the defaults, decided after the owner consulted the coordinator: keep `cmid`/`loginas`/`realuser_id`; `rubric_source` `"lms"` in place; `VEKTRA_LMS_MOODLE_*` with no alias; the provider in `vektra-moodle-discuss`, excluded from the plugin zip; a second image tag with the pinned wheel.
- 2026-11-11, ADR-0033 §7 re-measured before PR 2a-1 (Dependencies), on `origin/develop` `3fae70d5` (#199 merged; #200 merged earlier). Guard-exact counts (the guard's `mentions_in` over `tracked_tree`):
  - `vektra-assess`: 96 files and 551 mentions (`moodle/`: 35 and 247; `src` outside `moodle/`: 59 and 294; `ui/src`: 2 and 10);
  - the platform: 7 files and 31 mentions; migrations: 18 files and 37 mentions. The baseline reports no violation.
  - imports of the client from outside `moodle/`: 20 import statements in 8 files (the same lines as §7; the `rg` predicate also prints 6 docstring lines in 5 more files, excluded by reading);
  - reader calls: 32 `r.read_*`/`r.fetch_*` sites over 16 functions, plus 3 `resolve_activity`/`resolve_in_course` call sites: 35 over 18, as §7;
  - `cmid`: 586 occurrences in 44 files outside `moodle/`; 21 migration files name it;
  - fixtures: 328 (4.3: 158, 5.2: 170); 44 revisions, 0001 to 0044; `ENABLE ROW LEVEL` only in 0003;
  - 19 test files reference `MoodleStartSource` or `MoodleCaptureReaders`, as §7. The "38 test files reference the client, its fixtures or `assess_moodle_support.py`" of §7 has no recorded predicate; `rg -l 'vektra_assess\.moodle|moodle_ws|assess_moodle_support' vektra-assess/tests -g '*.py'` gives 47 (plus 1 in `vektra-app/tests`). The 2a-2 sizing should use 47.

  Every number of §7 is unchanged by #199, except the test-file count, whose original predicate is unknown.
- 2026-11-11, after PR 2a-1: `vektra-assess` is at 95 files and 549 mentions (the two prose mentions of `readable.py` went with its re-export; the directory entry has no count). The platform and the migrations are unchanged (7/31, 18/37), the baseline file is untouched, and `vektra_shared.lms` names no product.
- 2026-11-11, PR 2a-1, where it deviates from the task text:
  - **Stacked on #201.** #201 (the ADR and this plan) was still open, and its branch predates #199, so 2a-1 merges it rather than rebasing on it. The PR diff shows #201's five files until #201 merges.
  - **2a-1.1/2a-1.2, where the enums live.** `StaffRole`, `QuestionState`, `QuestionKind`, `OverdueMode`, `GradeMethod`, `AttemptState`, `HtmlExtractionProfile` and `ReviewHtml` are in `types.py`, not `protocol.py`: the neutral dataclasses need them and `protocol.py` imports `types.py`, so the task's split was an import cycle. `protocol.py` holds `LmsProvider`, `SetupItem`, `CapabilityResult`, `StartupProblem` and the `SETUP_*` result strings. Everything is re-exported from `vektra_shared.lms`.
  - **Names chosen for the neutral types** (ADR D2 left them open): `AttemptState` values are neutral (`in_progress`, not the wire spelling), so 2a-2a's provider maps them; `ReviewVisibility` flags are `show_*` (they are flags, not content: the type-level test allows no other field name with "right answer" in it); `ReviewItem.status_label`, `max_mark`, `items` (was `slots`); `GradeItem.kind: ActivityKind` (was `itemmodule: str`); `AssignmentSettings.activity_id` (was `cmid`); `NOT_IN_USER_COURSES` and `PROVIDER_ERROR` are constants of `types.py`.
  - **2a-1.3**: the docstring said "All 9", not 10. It now says 11: nine defined there, `KeyStoreProvider` in `auth.py`, `LmsProvider` re-exported.
  - **2a-1.4**: `ASSESS_LOGGER_PREFIX` is gone; `register_strict_logger_prefix` validates a dotted lower-case module path, and a prefix registered after `install_assess_stdlib_handler` ran gets its own scrub handler at registration, so the order of imports does not matter. `install_assess_stdlib_handler` now returns one handler per prefix (a list); no caller used the single return value. `is_assess_logger` stays as an alias of `is_strict_logger`. The vektra-shared tests register `"vektra_assess"` themselves (they must not import the vertical, `test_image_content_boundary.py`); the assess side proves in a subprocess that `vektra_shared.log_safety` alone registers nothing and `import vektra_assess` registers exactly `vektra_assess`.
  - **2a-1.7, the kit's shape.** The suite is a class, `lms_conformance.suite.LmsConformanceSuite`, which a provider's test module subclasses with a `ConformanceTarget`; the target carries the provider factory (one provider per `TransportMode`: recorded, unknown shape, timeout, breaker open, raising) over the recorded transport. `conftest.py` only puts the kit on `sys.path`: a fixture in a conftest is not visible to a consumer in another test tree. The checks are plain functions in `kit.py`, so the negative controls call them on broken providers (`vektra-assess/tests/test_fake_lms.py`: raises, trusts the hint, leaks a canary, logs its credential, drops a declared item, answers nothing; each is red). Two parts of D10 are left to 2a-2 by design: the canary check skips `ReviewHtml.html` (the teacher-visible rendering; its cleanliness after the profile is the vertical's canary sinks' job), and the "exact LLM request bytes of a full capture" sink needs the vertical's capture on the Protocol.
  - **2a-1.6**: `VEKTRA_ASSESS_LMS_PROVIDER` is documented as "not read yet"; the name is validated (`[a-z][a-z0-9_-]{0,63}`, empty is unset).
- 2026-11-11, `/code-review high` on #203, 10 findings, each fixed with a failing-first test where testable:
  - `LmsProvider.startup_problems(development: bool)` takes the resolved flag, not the raw `VEKTRA_ENV` (ADR-0033 D2 code block amended);
  - `docker-compose.assess.yml` forwards `VEKTRA_ENV: ${VEKTRA_ENV:-}` to the worker and the parser (an empty value counts as unset); `vektra-assess-db-init` runs `psql`, no Vektra code, and gets none;
  - the strict-prefix registration moved out of `vektra_assess/__init__` (the forkserver preload put `vektra_shared` and `structlog` in every parsing child) into `vektra_app.main.configure_structlog` and the worker's `main`; a child ceiling (`assess_parser.assert_child_import_ceiling`) now refuses both;
  - `load_lms_provider` is async, refuses a marked test double before building it, and `aclose()`s anything built and then refused;
  - the kit walks numbers, walks review HTML as its own profile leaves it, and takes its anti-vacuous witness from what the provider's read paths consumed in the run; the fake keeps its canaries in per-member wire answers, one of them numeric;
  - `build_assess_safeguard(settings)` enforces the safeguard/locality rule itself, so the eval adapter is held to it (the eval CI tier now sets `VEKTRA_ENV=development`);
  - `log_safety`'s prefixes are an immutable tuple swapped under a lock;
  - the platform-wide `VEKTRA_ENV` default is recorded as DEBT-043, not changed here;
  - the test-double marker is defined once, in `vektra_shared.registry`.
- 2026-11-11, open point for the pilot (2a-1.11): **Presidio's effectiveness on Italian-language data is not measured.** The default safeguard (`presidio`) is loaded with an English spaCy model (`en_core_web_lg` or `_sm`, `assess_llm.py`), and the pilot's student data is in Italian. Until a recall measure on Italian text exists, `presidio` being "on" is not evidence that personal data is caught. It matters for the pilot's go/no-go: owner to decide who measures it, and on which dataset.
- 2026-11-11, 2a-1.11 caveat, then closed in the same PR (coordinator, 2026-11-11): `VEKTRA_ENV` defaults to `development` in both settings classes, so a deployment that did not set it took the "development" row of every assess development-only switch. Closed inside vektra-assess only (the platform default in `vektra_shared/config.py` is unchanged): `VektraAssessSettings.is_development` is true only when `VEKTRA_ENV=development` is in `model_fields_set`, and every assess decision that relaxed on development uses it. That covers the refusals of `WORKER_ALLOW_UNBOUND`, `PRIVACY_ALLOW_UNSET`, `MOODLE_ALLOW_HTTP`, `LLM_ALLOW_UNACTIVATED` and the safeguard/locality rule; the runtime honouring of the privacy and activation switches (`worker/preflight.py`, `worker/render.py`, `api/teacher_ops.py`); the erasure-mode requirement; the UI-origin checks (`assess_wiring.py`, `ui/router.py`, the setup check); and the loopback and plain-http allowances of the LMS client. The platform's own `main.py` trace default is out of scope and unchanged. The refusals say `VEKTRA_ENV unset; ... set VEKTRA_ENV=development explicitly`. Every compose file, CI job and chaos harness that uses a dev switch already set `VEKTRA_ENV=development`; two unit-test files that relied on the default now set it (`test_assess_startup.py` fixture, `test_moodle_client_tls.py`).
- 2026-11-11, PR 2a-2 (branch `feat/assess-on-lms-protocol`, stacked on #203), where it deviates from the task text:
  - **Wiring pulled forward from 2a-4b.5.** The worker already loads its one provider with `load_lms_provider(VEKTRA_ASSESS_LMS_PROVIDER)` (`assess_worker._load_lms`); `_ws_client` and the `MOODLE_ALLOW_HTTP` check are gone from `vektra-app` (the latter is `MoodleLmsProvider.startup_problems`). The `moodle` entry point is declared by `vektra-assess/pyproject.toml` until 2a-4 moves it to the adapter's distribution. Unset with assess enabled refuses the worker. Effect on the baseline: `assess_worker.py` reached 0 mentions and its entry is **removed** (12 -> 0). No other entry changed, none added. `docker-compose.assess.dev.yml` defaults the setting to `moodle`; `docker-compose.assess.yml` passes it with no default.
  - **The chaos worker** offers the fake through `assess_worker.main(lms_entry_points=...)` (a development-only entry point, `VEKTRA_ASSESS_LMS_PROVIDER=fake`); a test double is refused unless `VEKTRA_ENV=development` is explicit (acceptance cases in `tests/chaos/assess/test_acceptance_worker.py`). `scripts/assess_burst.py` and `tests/nfr/test_assess_burst.py` target a real dev stack and compose no client: unchanged.
  - **Contract (c), 2a-2a.9.** `vektra_assess.setup` (the generic runner) also calls the provider, so the AST test allows `LmsProvider` under `worker/`, `context/capture/`, `setup/` and `moodle/`. Contract (c) is split in three: no assess module imports `vektra_assess.moodle`; only the worker imports `parser_client`; only the worker imports `vektra_assess.setup`. The capture `ignore_imports` are gone.
  - **Recorded suites kept as joint tests.** `test_context_capture.py`, `golden_part_a_cases.py` and the canary matrix (`tests/canary/runner.py`) still run on the 4.3/5.2 recordings, now through `MoodleLmsProvider`: they are the D4 joint guarantee (Vektra's canary sinks over the provider's recorded reviews) and move with the provider in 2a-4. Their logic counterparts are on the fake (`test_capture_fake.py`, `test_context_selection.py`, `test_idor.py`, `test_start_source_cache.py`, `test_teacher_gate.py`) and the integration suites seed through `FakeLmsProvider` (`support/sessions.py`, `support/teacher_ops.py`).
  - **Names kept for 2a-3.** `ws_tls_ok` is not renamed `lms_tls_ok` yet; `moodle_release`, `moodle_origin`, `moodle_rubric`, `moodle_reasons` stay (D5 renames). The snapshot's `QuizItem.qtype` and `state` now hold the neutral `QuestionKind` / `QuestionState` values (model payload: `tipo: multiple_choice`).
  - **Review HTML (D4).** The sidecar's `review_item` takes the profile in the request header and refuses a request without a valid one (`no_profile`); its reply keys are `question_text`/`response`/`truncated`. `test_review_extraction_golden.py` proves the Moodle profile reproduces, for all 162 recorded questions, the extraction of the code before the change (`tests/golden/review_extraction_moodle.json`, committed first).
  - **D9 backstop** (`test_site_predicate_static.py`): 14 API statements read a site-scoped table without a `site_id` predicate; each is safe for a stated reason (the key-to-site lookup, a lookup by an id from a token already verified for the site, one deployment-wide count) and sits in a commented allowlist that fails when stale. Phase-3 RLS is their only database backstop.
  - **Open**: the fake answers `not_found` where Moodle's `resolve_in_course` answers `activity_not_in_course` (a neutral constant for it would let a fake-based test reach `api/teachers.py`'s definitive branch).
- 2026-11-12, PR 2a-2 (#205), after the high-effort code review (10 findings, all fixed):
  - **Protocol additions** (`vektra_shared.lms`): `LmsProvider.required_secrets` (24 members; the worker binds the setup check on the provider's declared secrets, not on a hard-coded key name); `HtmlExtractionProfile.kinds` (the question kinds a provider proved its profile for; the vertical extracts only kinds in both its own `EXTRACTION_KINDS` and the profile's); `SUBMISSION_MISMATCH` / `FILE_NOT_IN_SUBMISSION` (file-ownership refusals the vertical audits for any provider).
  - **One fail-closed wrapper**: `vektra_shared.lms.guarded` / `GuardedLmsProvider` gives every Protocol member a declared failure and fails at import when a member has none. The worker wraps the provider once in `_load_lms`; `SiteReaders` and the setup runner call `guarded()` again, which is a no-op on a wrapped provider, so the vertical stays fail-closed when composed by a test. The four hand-written copies are gone. A raising `probe_capabilities` is now `not_probed` for every flag, so the site's release does not move.
  - **Snapshot schema 2 and prompt version**: the neutral `QuestionKind` / `QuestionState` values in `QuizItem.qtype` and `state` bump `SCHEMA_VERSION` to 2. Under OD-1 there is no v1 reader, and a v1 header fails validation. The payload's `tipo:` line changed, so `context.payload.PAYLOAD_VERSION` (2) now enters `compute_prompt_version`: `prompt_version` goes from `8fd783eb` to `806fa729`, and an NFR-023 activation is needed again. The synthetic eval dataset (`tests/eval/assess/build_dataset.py`, `fixtures/*.json`) was regenerated with the neutral vocabulary and schema 2.
  - **Parser wire**: `PROTOCOL_VERSION` 2 (worker and sidecar of different builds refuse each other); a profile is validated on its encoded size against the request header (`MAX_PROFILE_BYTES`); a request the client cannot frame is a protocol violation (the item degrades), not an exception; the profile's wire form is cached per profile.
  - **Worker lifecycle**: the provider is closed when a later startup check refuses, and at shutdown (tested). `VEKTRA_ASSESS_LMS_PROVIDER=moodle` is an active line in `.env.example`; no implicit default.
  - The Moodle setup-check module returns the Protocol's `SetupItem` and uses `SETUP_*`; its own `Outcome` and `OK`/`MISSING`/`UNVERIFIABLE` aliases are gone.
  - **Review**: CodeRabbit skipped (file count); replaced by two local /code-review high passes, 20 findings fixed (coordinator, 2026-11-12: no split, to avoid another CI and merge cycle).
  - **Second pass** (10 findings on `d8904d1d`): `CheckContext` wraps its provider, so `run_items` and every other public setup entry point fails closed; `HtmlExtractionProfile` freezes `keep`, `drop_classes` and `kinds` in `__post_init__` (a provider's `set` no longer breaks the client's cache); the guard gives `name` a declared failure (`<unnamed>`), its startup fallback names `VEKTRA_ASSESS_LMS_PROVIDER`, and it no longer forwards non-Protocol attributes (no recursion on copy or pickle; no vertical caller needed one); one composition gate (`assess_worker._bound_lms`) binds every LMS-reading handler only with the provider's declared secrets (key matrix loading stays fixed: task 2a-4b.13); an over-sized request is detected before the socket connect and reported `request_too_large`, not as a sidecar violation; an intact snapshot of an old schema is `unsupported_schema`, not `unparseable` (the finding cited `snapshot.py:555`; the site is `context/capture/store.py`); one `not_probed` helper (`vektra_shared.lms.not_probed`); the Moodle provider imports `tokens.SECRET_NAME`, and its file reader emits the neutral refusal codes directly (identity map gone).
- 2026-11-11, PR 3-1 (task 3.1), where it deviates from the task text:
  - **Branch** `chore/vektra-testkit` (not `refactor/testkit`), stacked on #203 (2a-1), which created `vektra-shared/tests/lms_conformance/`.
  - **Layout.** `testkit/` is a workspace member, distribution `vektra-testkit`, package `vektra_testkit`: `testing_recorders.py` is now `vektra_testkit.recorders`, the kit is `vektra_testkit.lms_conformance` (`kit`, `suite`). The kit's `conftest.py` (it only put the kit on `sys.path`) is gone, and so is the `sys.path` insert in `vektra-assess/tests/test_fake_lms.py`: the kit is an installed package. The kit's own test (`test_kit.py`) stays a vektra-shared test, as `vektra-shared/tests/test_lms_conformance_kit.py`; `test_testing_recorders.py` stays too. The shared tests import `vektra_shared` and `vektra_testkit` only (the existing guard now allows exactly these two).
  - **Installed by** the dev groups of the root, `vektra-shared`, `vektra-assess` and `vektra-app`, so every CI `uv sync --dev` (with or without `--package`) has it; `testkit/**` triggers the shared, assess and app jobs.
  - **A workspace member is installed by `uv sync --no-dev`** (measured: `uv export --no-dev` lists `./testkit` and pytest). The Dockerfile therefore copies only `testkit/pyproject.toml` (uv needs the manifest) and every `uv sync` there adds `--no-install-package vektra-testkit`. That flag skips the package, not its dependencies, so `vektra-testkit` does **not** declare pytest (every dev group that installs it already does); with that, the image's resolution is identical to the one before this PR (`uv export` with the Dockerfile flags, diffed).
  - **Proof** in `test_image_content_boundary.py`: the kit is in dev groups only and no shipped source imports it; the Dockerfile never copies `testkit/` beyond its manifest and every sync skips it; the Dockerfile's final `uv sync` flags replayed into `uv export` give no `vektra-testkit` and no pytest, and without the skip they do (negative control); a freshly built `vektra_shared` wheel has no `testing_recorders` and no `vektra_testkit`, and the kit's own wheel is caught by the same check (negative control). A local `docker build` of the API image was walked file by file: no `testing_recorders`, `vektra_testkit` or `lms_conformance` path, neither importable, nor pytest; the only file naming `vektra_testkit` is the comment in `vektra_shared/registry.py`.
  - mypy now also checks `testkit/src/vektra_testkit` (`make lint`, `lint.yml`), as it checked the recorders inside `vektra_shared` before.
- 2026-11-12, PR 2a-3 (branch `feat/assess-schema-branch`, stacked on #205), task 2a-3.0 at the cut, measured on `origin/feat/assess-on-lms-protocol` `cf083d13`:
  1. `git tag -l 'v*'` lists 10 tags (v0.1.0, v0.2.0, v0.3.0, v0.4.0, v0.5.0, v0.5.1, v0.6.0, v0.7.0, v0.7.1, v0.8.0); `git ls-tree -r --name-only <tag> -- vektra-assess migrations/versions | rg -c assess` is 0 for each (v0.8.0 ends at `migrations/versions/0008_conversation_owner.py` and has no `vektra-assess` path). No release ships assess.
  2. `gh api repos/vektralabs/vektra-moodle-discuss/tags` returns `[]`.

  Both facts hold: OD-1 = (a) stays valid, and 2a-3 squashes.
- 2026-11-12, task 2a-3.1: the goldens were taken from the old chain (0001 to 0044, `alembic upgrade head` on an empty pgvector/pg16 container) before any revision was deleted, by `vektra-assess/tests/support/schema_dump.py`: `assess_schema_head.sql` (the database's normalised `pg_dump --schema-only`, plus the seeded `namespaces` rows) and `assess_privileges_head.txt` (the three roles' attributes and memberships, and every effective privilege on schema public, sorted: 819 lines). The D5 renames (`moodle_site_uuid`, `moodle_origin`, `ck_assess_sites_moodle_origin`, `moodle_release` to their `lms_*` names) are applied to both; 4 lines of the old dump carried them, and no line of either golden names an LMS.
- 2026-11-12, PR 2a-3, where it deviates from the task text or adds to it:
  - **The squash is pg_dump's spelling.** `a0001_assess_base.py` carries the old head's `pg_dump --schema-only` DDL (renamed), grouped per table, then the foreign keys, the functions and the triggers, plus the hand-written parts of the old chain: the namespace refusal (formerly 0019), the role creation with the CREATEROLE message (formerly 0009 and 0012, now one message naming all three roles), the `api_keys` and `query_traces` changes (0009, 0019, 0020), the definer ownership and `GRANT_STATEMENTS` (the rendered matrix). One CHECK (`ck_assess_privacy_url`) keeps the source spelling of 0040: pg_dump's deparse, re-parsed, nests its `AND`s differently. With that, the dumps are identical on the first comparison.
  - **Alembic records only `a0001`** once the branch is applied: `0008` is implied by `depends_on`. So `alembic_version` holds one row (`a0001`) with assess on, and `0008` with it off.
  - **`vektra_shared.startup.check_database_schema`** (not in the task list) had to change: it read only `migrations/versions` and compared the current heads with all heads, so an assess-enabled database (`a0001`) would have failed the API's step 3. It now loads `alembic.ini`, requires the heads of the branches it is given (`platform` always, `assess` only when enabled: `vektra_app.assess_wiring.migration_branches()`), counts a revision as applied when a current one depends on it, and refuses a revision unknown to the release by name (a database still at `0044`).
  - **`alembic upgrade head` fails now** (two heads). Platform test fixtures run `platform@head`; the assess, admin (assess scopes and the reserved namespace are tested there) and chaos fixtures run `heads`.
  - **`migrations/env.py`** needed no change: the assess location is `version_locations` in `alembic.ini` (`path_separator = newline`). The image copies `vektra-assess/src/vektra_assess/migrations/` to the same path under `/app`.
  - **`deploy/postgres/init.sql` no longer creates the two assess roles**: a fresh compose volume with assess off then has 0 of them, as the acceptance criterion counts. With assess on, the branch creates all three and `assess-roles.sql` (db-init) sets LOGIN.
  - **Consequence for the admin UI with assess off**: an assess-scope key is refused by the platform's `ck_api_keys_scopes` (the assess scopes are added by the assess branch). Not changed here; an admin UI that hides the assess scopes when assess is off would be a follow-up.
  - **Tests of intermediate data migrations deleted**, since the revisions they drove no longer exist: the round trips to 0033, 0034, 0035, 0038, 0017 (`test_delivery_schema.py`, `test_transcript_schema.py`, `test_teacher_config_schema.py`, `test_schema_assess_jobs.py`), the 0033 `moodle_origin` backfill and the 0036 composite-config refusal. Kept as head guarantees: the `rotate_keys` refusal of `ck_assess_jobs_kind` (`test_jobs_outbox.py`), the origin CHECK against the code's normalisation (`test_schema_lms_origin.py`), the 0019 namespace refusal and the reserved-row downgrade (`test_trace_pipeline_db.py`, now `assess@base`), the scopes and the assess-key downgrade refusal (`test_schema_assess_scopes.py`), the sessions downgrade refusal (`test_schema_assess_tables.py`). `test_schema_branch.py` adds the full round trip: `downgrade assess@base` gives back the platform schema at 0008 byte for byte.
  - **Renamed test files**: `test_migration_0009.py` -> `test_schema_assess_scopes.py`, `test_migration_0033.py` -> `test_schema_lms_origin.py`, `test_schema_0010_0013.py` -> `test_schema_assess_tables.py`, `test_schema_0018_jobs.py` -> `test_schema_assess_jobs.py`, `test_render_moodle_reasons.py` -> `test_render_lms_reasons.py`. `test_grants.py` names no revision and is unchanged; the revision-naming grant test is `integration/test_grant_matrix.py`, now pointed at the assess location. New: `test_assess_migrations_static.py` (one revision, branch, the guard's matcher finds no LMS in the branch, the rendered matrix, the three-role message).
  - **Renames beyond the D5 table**, in the same spirit: the setup reason `same_site_as_moodle` -> `same_site_as_lms`, the reference rejection `moodle_id` -> `lms_id`, the Italian notification and transcript texts ("id Moodle" -> "id LMS"), and every other prose mention of the vertical outside `moodle/` and `config.py`. Left for 2a-4: `config.py` (15 mentions, the settings that move with the provider, 2a-4b.2) and 8 citations of the plan slug `assess-moodle-ws` in 5 files (`worker/slots.py` uses it as a value).
  - **`prompt_version` changes again**: the shipped string files' header comments name the LMS no more, and their bytes enter the version: `806fa729` -> `2a40bfa0`; a new NFR-023 activation is needed (CHANGELOG).
  - **Baseline**: the 18 migration entries are deleted, and `assess_wiring.py` reached 0 mentions (its 5 remaining were startup remediation texts), so its entry is deleted too rather than lowered. 19 entries removed, none added, no count raised; 6 entries remain (the `vektra-assess` directory and 5 platform files), for 2a-4.
  - **Size**: about 190 files, 36 of them deleted revisions and 12 regenerated eval fixtures. Local review only (CodeRabbit reviews 2a-2 only).
- 2026-11-12, PR 2a-4 (branch `feat/lms-moodle-adapter-out`, stacked on #206), where it deviates from the task text or adds to it:
  - **Commits.** The 328 recordings move in a rename-only commit (`git show --stat -M100%`: 328 renames, 0 lines changed); the package, its scripts and the moved test modules in a second rename-only commit (70 renames). Both use `git mv` only; the edits follow in separate commits. The second rename commit does not import on its own (its imports are rewritten in the next one).
  - **Layout.** `adapters/vektra-lms-moodle/`: `src/vektra_lms_moodle/` (the former `vektra_assess/moodle/`), `scripts/` (`gen_ws_models.py`, `record_ws.py`, and the operator CLI `setup_spike.py`, formerly `vektra_assess.moodle.spike`: it runs the vertical's setup check, so it is a script beside the provider and not part of the distribution, which depends on `vektra-shared` only), `tests/` (unit, conformance, joint) and `tests/integration_moodle/` (the joint DB suites and the live ones; not `integration/`, which would shadow the vertical's `integration` package the joint suites import).
  - **The joint suites stay joint.** Beyond 2a-4b.3, these moved too, because they run the vertical's code over the provider's recordings (the D4 joint guarantee) and the acceptance criterion is "no Moodle fixture in the vertical's suites": `test_context_capture.py`, `golden_part_a_cases.py` with `test_context_part_a.py` and `golden/part_a.txt`, `canary/` with `test_canary_matrix.py`, `test_review_extraction_golden.py` with its golden, `integration/test_setup_check_{run,handler,render}.py`, the recorded half of `test_context_selection.py` (now `test_context_selection_recorded.py`; the pure cases stay), the Moodle half of `test_idor.py` (now `integration_moodle/test_idor_moodle.py`, calling the vertical's `_matrix`), and the recorded canary subset of `integration/test_snapshot_store_db.py` (now `integration_moodle/test_snapshot_canary_db.py`). `InProcessParser`, `OFF` and `wire_profile` moved from `test_context_capture.py` into the vertical's `tests/support/parser.py`, which the vertical's capture tests now import.
  - **How the joint suites reach the vertical's harness.** The adapter's `tests/conftest.py` puts `vektra-assess/tests` on `sys.path` and takes over the vertical's conftest fixtures (hermetic env, structlog reset, canary log capture); `integration_moodle/conftest.py` re-exports the vertical's real-Postgres fixtures. `VEKTRA_STACK_ROOT` points at the vertical when the adapter is not in this repository (2b). The suites run with `UV_NO_SYNC=1`: the vertical's DB fixture calls `uv run alembic`, which would otherwise sync the provider out of the venv mid-run.
  - **2a-4b.2, settings.** `vektra_lms_moodle.config.MoodleSettings`: the 11 settings plus a 12th, `VEKTRA_ASSESS_INGEST_ACCOUNT_USERNAME` -> `VEKTRA_LMS_MOODLE_INGEST_ACCOUNT_USERNAME`: only the provider's `not_ingestion_account` item read it, so leaving it in the vertical would have kept a dead setting there. No alias (OD-4). The provider also reads `VEKTRA_ENV` (same explicit-development rule) and `VEKTRA_ASSESS_SECRETS_DIR` (the directory the worker's key matrix validated its secret in). The live-test harness variables (`VEKTRA_ASSESS_MOODLE43_URL`, `..._MANIFEST_DIR`, `..._TOKEN_DIR`) are test inputs, not settings, and keep their names: the contract workflow and the plugin session's docs use them.
  - **The `teacher_gate` budget** was derived from `VEKTRA_ASSESS_WS_TOTAL_TIMEOUT_S`; the vertical no longer reads the provider's settings, so it is the fixed default for a 30 s deadline (`TEACHER_GATE_ASSUMED_CALL_DEADLINE_S`, 95 s), overridable as before; the adapter's suite asserts its default deadline fits. Documented in `configuration.md` and the CHANGELOG.
  - **2a-4b.13, key matrix.** `assess_ws_tokens` left `KEY_MATRIX`. The worker loads the provider first, then `enforce_key_matrix(..., provider_secrets=lms.required_secrets)` requires and loads each declared name as credentials, with the same file check (`vektra_shared.secret_files`, new, shared with the provider's token reader, which may not import `vektra_assess.keys`). The API and the parser load no provider, so they refuse every `assess_*` file the matrix does not grant them (this replaces the `assess_ws_token*` glob); a declared name must therefore be `assess_<name>` and not a matrix name. Lost: the API's refusal of an env var named after the WS token secret (it can no longer know the name). `keys generate --provider-secret <name>`; `make assess-keys` passes `assess_ws_tokens`.
  - **Dormant gap closed at the move (Design Notes).** The provider's loggers left `vektra_assess.*`: `build_provider` now registers `vektra_lms_moodle` as a strict prefix, and the adapter's suite proves a provider log line is redacted and runs the vertical's two log guards (named loggers, literal messages) on the provider's source.
  - **Parser ceiling.** The parser refused the client as a stray `vektra_assess.*` module; it now refuses any `vektra_lms_*` module by prefix (`assess_parser.LMS_PROVIDER_PACKAGE_PREFIX`).
  - **2a-4b.7 and OD-6, images.** `ARG INSTALL_LMS_PROVIDER=` (empty) before the first `FROM`; stages `lms-provider-none` (adds nothing) and `lms-provider-moodle` (copies the adapter's manifest, README and `src/` only, then the same `uv sync` plus `--group lms-moodle`, non-editable); `FROM lms-provider-${INSTALL_LMS_PROVIDER:-none} AS venv`, and the runtime copies the venv from it. BuildKit builds only the selected stage, so a default build never copies `adapters/`. `publish.yml` gains the `lms-moodle` variant (`<version>-lms-moodle`); in 2a its provider is built from the tagged tree with its resolution pinned by `uv.lock`, not yet a published wheel by hash (2b.2). No tag or image was cut or pushed. `docker-compose.assess.yml` builds the variant as `vektra-stack-lms-moodle` and runs every assess service from it; the dev overlay inherits it; the CI assess-stack job builds and seeds from it.
  - **2a-4b.9/10/11, guards.** `test_lms_boundary.py`: `adapters/` is outside the perimeter by the member rule alone (a test shows the same path would be judged if it were a member); no workspace member is an adapter; no workspace member declares a `vektra-lms-*` requirement in dependencies, extras or groups (positive control planted); the adapter's sources do name its LMS (the predicate is not dead); the baseline is empty. `test_image_content_boundary.py` reads the Dockerfile per stage: the default build selects the stage without a provider, only a provider stage copies `adapters/` and never its tests, the default resolution (`uv export` with the builder's flags) has no `vektra-lms-*` and the provider stage's has `vektra-lms-moodle` (positive control), no shipped source imports a provider, and the adapter's recordings count as test assets. Import-linter: contract (c) is now "no platform package imports an LMS provider" (every root package as a source, `vektra_lms_moodle` forbidden; `test_boundaries.py` checks both lists); its planted canaries break it without the provider installed. A real-image check (built, not unit-tested) is in the PR body.
  - **CI.** `test-lms-moodle` in `ci-unit.yml`, path-unfiltered and in the CI gate: `uv sync --group lms-moodle`, the adapter's whole tree (live suites skip as BLOCKED), then a JUnit check that both conformance classes, the joint setup-check, IDOR and canary-DB suites and the canary matrix ran and were not skipped. `assess-moodle-contract.yml` installs the group and runs the adapter's live suites and its `scripts/setup_spike.py`. `test_suite_execution_coverage.py` discovers `adapters/*/tests` and accepts `make test-lms-moodle` as the adapter's local gate. mypy checks the adapter's source (`make lint`, `lint.yml`).
  - **`slots.py`** cited the web-service plan's slug as the owner of the setup-check slots; that slot was bound by the job loop on 2026-11-08, so its owner is now `JOB_LOOP`. The other citations of the slug in `src` are reworded ("the LMS web-service plan (20261101-154950)").
  - **Not done here:** `.s2s/architecture.md` (phase 4's last task), and 2b.
- 2026-11-12, PR 2a-4 (#207), after the high-effort code review (10 findings, each verified against the code and fixed failing-first; the new tests were run red against the previous code):
  1. **The API's env-var refusal of the WS tokens came back, without building a provider.** A provider declares its secrets statically on its entry point (`REQUIRED_SECRETS`; the Moodle one is the `build_provider` object, an instance of `_Factory`) and `required_secrets` must return the same names (the loader refuses a mismatch). `vektra_app.lms_providers.installed_provider_secrets()` loads the installed entry points without calling them; the API passes those names to `enforce_key_matrix`, which refuses an env var carrying one (`_env_names_carrying`), assess enabled or not. The parser loads no entry point (its ceiling refuses `vektra_lms_*`); its fragment check now also covers bare `ASSESS_*` names.
  2. `MoodleSettings` has `populate_by_name=False`: bare names are not read.
  3. **Protocol member 25, `call_deadline_s`** (guard: `nan` on failure, refused at startup; kit: checked in `(0, 600]`; fake and stubs updated). `load_policy(env, *, lms_call_deadline_s)` derives `teacher_gate` from it; `TEACHER_GATE_ASSUMED_CALL_DEADLINE_S` is gone.
  4. A setting under a pre-2a-4 name refuses the worker (`MoodleLmsProvider.startup_problems`), listing each with its new name; exact names for `VEKTRA_ASSESS_MOODLE_*` so the live-suite variables are not caught, whole families for `VEKTRA_ASSESS_WS_*` and `VEKTRA_ASSESS_BREAKER_*`.
  5. The explanation of a settings `ValidationError` moved to `vektra_shared.settings_errors` (the vertical's function is a wrapper); the provider raises `vektra_shared.lms.ProviderConfigurationError`, which the loader surfaces as it is.
  6. `vektra_lms_moodle/__init__.py` registers the strict prefix at import; `build_provider`'s and the conftest's registrations are gone.
  7. The worker finds the provider (no build), checks the whole key matrix with the declared names, and only then builds it; a missing provider and key violations are reported in one refusal.
  8. The factory is called with `development=` (the vertical's resolved flag); `MoodleSettings` lost `env`, `is_development` and `env_described`, and `MoodleWsClient` takes `development` (default False).
  9. `keys.py`: the redundant `granted` set is gone, one `is_provider_secret_name` helper, and the not-found case comes from `check_secret_file`.
  10. The provider module's docstring is current.
- 2026-11-12, request for phase 2b (to the plugin session that owns `vektra-moodle-discuss`; not sent from here, this session never edits that repository):

  > Phase 2b of the LMS separation (ADR-0033 D6, OD-5, OD-6; plan 20261111-185453-assess-lms-separation, tasks 2b.1 to 2b.3). In vektra-stack, PR 2a-4 staged the Moodle LMS provider as its own distribution in `adapters/vektra-lms-moodle/` (package `vektra_lms_moodle`, distribution `vektra-lms-moodle`, entry point `vektra.lms_providers: moodle = vektra_lms_moodle.provider:build_provider`, depends on `vektra-shared` only). Please host it in `vektra-moodle-discuss`:
  > 1. a top-level `provider/` tree holding the current content of `adapters/vektra-lms-moodle/` (`pyproject.toml`, `README.md`, `src/`, `scripts/`, `tests/`, including the 328 recordings under `tests/fixtures/moodle_ws/`), moved with history if practical;
  > 2. the plugin release zip excludes `provider/`, and the release script's content check asserts it;
  > 3. a Python CI job that checks out a pinned `vektra-stack` ref, sets `VEKTRA_STACK_ROOT` to it (the provider's joint suites import the vertical's test harness from `vektra-assess/tests`), installs that workspace plus `provider/`, and runs `pytest provider/tests` with `UV_NO_SYNC=1` and Docker available; the job must fail if `TestMoodle43Conformance`, `TestMoodle52Conformance` or the `integration_moodle` suites are skipped (vektra-stack's `test-lms-moodle` job has the JUnit check to copy);
  > 4. the weekly live contract run against the dev Moodle 4.3 and 5.2 sites (`-m moodle`, the variables `VEKTRA_ASSESS_MOODLE43_URL`, `VEKTRA_ASSESS_MOODLE52_URL`, `VEKTRA_ASSESS_MOODLE_MANIFEST_DIR`, `VEKTRA_ASSESS_MOODLE_TOKEN_DIR`), as vektra-stack's `assess-moodle-contract.yml` does today;
  > 5. a wheel release of `vektra-lms-moodle`, versioned with the vektra-stack release it was tested against, and its sha256 published with it.
  > When the first wheel is published, tell the vektra-stack coordinator its version and hash: vektra-stack then removes `adapters/` and the `lms-moodle` group, and `publish.yml`'s `lms-moodle` variant installs that wheel by pinned version and hash (2b.2, 2b.3). The provider's settings are `VEKTRA_LMS_MOODLE_*` (no alias); please update any plugin help text that names the old `VEKTRA_ASSESS_MOODLE_*`, `VEKTRA_ASSESS_WS_*` or `VEKTRA_ASSESS_BREAKER_*` names.
- 2026-11-12, task 2a-3.9, the commands (not run by this session; the coordinator runs them after the merge, on the owner's pre-authorisation of OD-1, which 2a-3.0 confirmed):
  - **Local F1 database** (container `vektra-f1-postgres-1`, volume `vektra-f1_vektra_pgdata`; at the cut it is at revision `0037` with 34 `assess_*` tables, the 3 roles, 2 namespaces, 5 API keys, 0 source documents). Its assess revisions no longer exist, so the whole test database is re-created; the API keys and the `assess_sites` rows are provisioned again afterwards (with `lms_site_uuid` / `lms_origin`).
    ```sh
    # 0. read-only, before
    docker exec vektra-f1-postgres-1 psql -U vektra -d vektra -Atc "SELECT version_num FROM alembic_version; SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'assess\_%'"
    # 1. stop the F1 stack (volumes kept), then drop its database volume
    /private/tmp/claude-501/-Users-scalzo-my-Unical-2025-DEH-ALMA-vektralabs-vektra-stack/75a21cc1-8da1-4d11-a7aa-b674ff0e9e4c/scratchpad/f1/dc.sh down
    docker volume rm vektra-f1_vektra_pgdata
    # 2. with the 2a-3 tree checked out in the main checkout (the stack builds from it): rebuild and start
    /private/tmp/claude-501/-Users-scalzo-my-Unical-2025-DEH-ALMA-vektralabs-vektra-stack/75a21cc1-8da1-4d11-a7aa-b674ff0e9e4c/scratchpad/f1/dc.sh up -d --build
    # 3. verify: expect "a0001", then 37, 3, 17
    docker exec vektra-f1-postgres-1 psql -U vektra -d vektra -Atc "SELECT version_num FROM alembic_version; SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'assess\_%'; SELECT count(*) FROM pg_roles WHERE rolname LIKE 'vektra\_assess\_%'; SELECT count(*) FROM pg_proc WHERE pronamespace='public'::regnamespace AND (proname LIKE 'assess\_%' OR proname='get_session_material')"
    ```
  - **Deploy VM** (a request to the internal-ops session; read-only; this session does not touch it), with that host's Postgres container, user and database:
    ```sh
    docker exec <postgres container> psql -U <db user> -d <db> -Atc "SELECT version_num FROM alembic_version; SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'assess\_%'; SELECT count(*) FROM pg_roles WHERE rolname LIKE 'vektra\_assess\_%'"
    ```
    Expected on v0.8.0: `0008`, `0`, `0` (v0.8.0 has no assess revision and its `init.sql` creates no assess role). Any `assess_*` table makes it a recreate request, after the owner's go.
