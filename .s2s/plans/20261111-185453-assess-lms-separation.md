# Implementation Plan: Separating `vektra-assess` from the LMS (the `LmsProvider` Protocol)

**ID**: 20261111-185453-assess-lms-separation
**Status**: in_progress (ADR-0033 accepted 2026-11-11, with OD-1 to OD-6 decided; phase 4, the ADR itself, is in review in #201; PR 2a-1 done on `feat/lms-provider-protocol`, in review; PR 2a-2 done on `feat/assess-on-lms-protocol`, stacked on #203)
**Branch**: `docs/assess-lms-separation-adr` (phase 4), `feat/lms-provider-protocol` (2a-1, stacked on #201). Each later PR has its own branch, listed in the PR split below.
**Created**: 2026-11-11T18:54:53Z
**Updated**: 2026-11-11T21:30:00Z

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
| 2a-4 | `refactor/lms-moodle-adapter` | 2a | 2a-4a.\* and 2a-4b.\* | The 328 recorded fixtures moved in a rename-only commit (proved by `git diff --find-renames=100% --stat`), then `vektra_assess/moodle/` to `adapters/vektra-lms-moodle/src/vektra_lms_moodle/`; its tests; entry-point composition in `vektra-app`; wording in the 5 comment-only platform files; settings move; opt-in image build argument; guard and image-boundary additions | ~420, mostly renames | **the directory entry and the 7 platform entries removed (ratchet 1)** |
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
- [ ] 2a-3.0 **validity check, at the moment of the cut and before any other 2a-3 task.** Both facts must hold, and each must be measured:
  1. no published release ships `vektra-assess`: no `vektra-assess` package or `assess_*` revision in any `v*` tag, checked with `git tag -l 'v*'` and then `git ls-tree -r <tag> -- vektra-assess migrations/versions | rg assess` for each tag;
  2. `vektra-moodle-discuss` has no tag: `gh api repos/vektralabs/vektra-moodle-discuss/tags` returns `[]`.

  Record both results in `## Notes`. If either is false, **stop**: OD-1 is decided again by the owner, and 2a-3 follows the fallback above.
- [ ] 2a-3.1 before deleting anything: run 0001 to 0044 on an empty cluster and commit the normalised schema golden `vektra-assess/tests/golden/assess_schema_head.sql` (`pg_dump --schema-only`, normalised), with the D5 rename map applied, plus the effective-privilege dump of the grant matrix
- [ ] 2a-3.2 `vektra-assess/src/vektra_assess/migrations/versions/a0001_assess_base.py`: one revision, `branch_labels=("assess",)`, `depends_on="0008"`, creating the head schema with `lms_site_uuid`, `lms_origin` (CHECK `ck_assess_sites_lms_origin`) and `lms_release`; the CREATEROLE error message names all three roles; delete `migrations/versions/0009…0044` (36 files)
- [ ] 2a-3.3 `alembic.ini` `version_locations`; `migrations/env.py`; branch label `platform` on `migrations/versions/0001_initial_schema.py`; `docker/entrypoint.sh` (lines 21, 33) runs `alembic upgrade platform@head`, and `assess@head` only when `VEKTRA_ASSESS_ENABLED=true`
- [ ] 2a-3.4 test `vektra-assess/tests/integration/test_schema_branch.py`: the squash equals the golden (schema and effective privileges); with assess off, a clean cluster has 0 `assess_*` tables, 0 `vektra_assess_*` roles and 0 assess functions after the entrypoint ran (counted); with assess on, a cluster without CREATEROLE fails with the three-role message, and the platform with assess off migrates without CREATEROLE (the review's managed-Postgres case)
- [ ] 2a-3.5 renames in place (ADR-0033 D5):
  - `db/models.py`, `db/grants.py`, `ui/origins.py`, `vektra-app/src/vektra_app/assess_wiring.py`, `capabilities.py`, `api/setup_check.py` (JSON `lms_release`)
  - `context/snapshot.py` (`lms_release`, `rubric_source` `"lms"`), `teacher/schemas.py`, `teacher/validation.py`, `api/teacher_config.py`, `api/teacher_ops.py`
  - `worker/render.py` (`lms_reasons`), `worker/start_source.py` (`lms_rubric`), `worker/start_guard.py` and `delivery/notify.py` (`lms_unreachable`)
  - reason codes `lms_rubric_missing`, `lms_facts_unreadable`, `lms_origin_invalid`, `lms_origin_not_https`, `ui_origin_equals_lms_origin`
  - `vektra_assess/errors.py` messages and remediations
  - `ui/src/main.ts`, `ui/src/bridge.ts` (`parentOrigin`) and `ui/router.py`
  - the comment-only mentions in the vertical, for example `gates.py:3,97`, `audit.py:15`, `llm/strings/*.yaml:1`, `llm/shipped.py:6`
- [ ] 2a-3.6 tests that name revisions are rewritten against the branch: `test_migration_0009.py`, `test_migration_0033.py`, `test_schema_0010_0013.py`, `test_schema_0018_jobs.py`, `test_grants.py`, `integration/test_migration_managed_postgres.py`, `test_render_moodle_reasons.py` (renamed `test_render_lms_reasons.py`)
- [ ] 2a-3.7 docs:
  - `docs/guides/assess-moodle-service-account.md:111` (provisioning SQL), `docs/guides/operators/assess-framing.md`, `assess-deployment.md`
  - `docs/reference/{api,configuration,error-codes}.md`; the upgrade note: "re-create the development assess schema"
  - CHANGELOG `[Unreleased]`
- [ ] 2a-3.8 baseline: delete the 18 `migrations/versions/*` entries and lower `assess_wiring.py`, in the same commit; guard green
- [ ] 2a-3.9 the databases. What is known:
  - the only database with assess data is the local F1 stack's Postgres (the `vektra-f1-postgres` container on the developer machine);
  - testcontainers are ephemeral;
  - the deploy VM is believed to run v0.8.0, which has no assess.

  At the moment of the 2a-3 cut:
  - **Local F1 database**: recreate its assess schema from `assess@head`. This is destructive, so stop and ask first, and run it **only after an explicit go from the owner**.
  - **Deploy VM**: a request to the internal-ops session, which operates it. This session does not touch it. The request is for a **read-only** check of whether any `assess_*` tables exist there. Only if they do does it become a recreate request to that session, again after the owner's go.

  Record in `## Notes` the result of the check, and what was recreated, by whom and when.

### Phase 2a, PR 2a-4 (part a): the recorded fixtures (rename-only commit)
- [ ] 2a-4a.1 `git mv vektra-assess/tests/fixtures/moodle_ws adapters/vektra-lms-moodle/tests/fixtures/moodle_ws` (328 files), and the fixture path constant in `vektra-assess/tests/assess_moodle_support.py`
- [ ] 2a-4a.2 proof in the PR body: `git diff --find-renames=100% --stat origin/develop...HEAD` shows 328 renames and 1 modified file, and `make test` is green

### Phase 2a, PR 2a-4 (part b, same PR): the adapter leaves the platform (ratchet 1)
- [ ] 2a-4b.1 `adapters/vektra-lms-moodle/pyproject.toml` (distribution `vektra-lms-moodle`, depends on `vektra-shared` only, entry point `vektra.lms_providers: moodle = vektra_lms_moodle:build_provider`); `git mv vektra-assess/src/vektra_assess/moodle adapters/vektra-lms-moodle/src/vektra_lms_moodle`; imports rewritten
- [ ] 2a-4b.2 provider settings class `vektra_lms_moodle/config.py`: the 11 settings of `vektra_assess/config.py:236-295` move here, with the names per OD-4; `startup_problems(env)` replaces `vektra-app/src/vektra_app/assess_worker.py:229-236`
- [ ] 2a-4b.3 the provider's tests move to `adapters/vektra-lms-moodle/tests/`:
  - `vektra-assess/tests/test_moodle_*.py` (9) and `integration/test_moodle_{live,setup_live}.py`
  - `test_review_block_extraction.py`, `assess_moodle_support.py`, `test_assess_record_ws.py`
  - the live suites `integration/test_{context,idor,req073}_live.py` (they run against a stack with the provider installed)
- [ ] 2a-4b.4 `scripts/assess_gen_ws_models.py` and `scripts/assess_record_ws.py` move to `adapters/vektra-lms-moodle/scripts/`; `.github/workflows/assess-moodle-contract.yml` paths updated; a CI job (path-unfiltered, like `test-structure`) runs the adapter's tests and the conformance kit
- [ ] 2a-4b.5 `vektra-app/src/vektra_app/assess_worker.py`: `_ws_client` and the `MoodleStartSource` construction (lines 319-370, 685, 870) are replaced by `load_lms_provider(settings.lms_provider)` and `LmsStartSource(provider, …)`; one provider instance per worker (one rate cap, one breaker registry, as today)
- [ ] 2a-4b.6 root `pyproject.toml`: dependency group `lms-moodle` with `vektra-lms-moodle = { path = "adapters/vektra-lms-moodle", editable = true }`; not a workspace member; `Makefile` and `uv.lock` updated
- [ ] 2a-4b.7 `Dockerfile`: `ARG INSTALL_LMS_PROVIDER=` (empty by default) installs the adapter only when set, on the `INSTALL_UNSTRUCTURED` model; `docker-compose.assess.yml` and `docker-compose.assess.dev.yml` build with `INSTALL_LMS_PROVIDER=moodle`
- [ ] 2a-4b.8 wording, the 5 comment-only platform files: `vektra-shared/src/vektra_shared/auth.py:80`, `errors.py:175`, `netguard.py:3,5,13,95` (a generic egress guard, ADR-0033 D8), `vektra-app/src/vektra_app/main.py:694`, `vektra-admin/src/vektra_admin/templates/keys.html:19`
- [ ] 2a-4b.9 guard addition in `vektra-shared/tests/test_lms_boundary.py`: no workspace member's `pyproject.toml` depends on a `vektra-lms-*` distribution, with a planted dependency turning it red
- [ ] 2a-4b.10 `vektra-shared/tests/test_image_content_boundary.py`: the default image contains no `vektra_lms_*` package and no `adapters/` file; with `INSTALL_LMS_PROVIDER=moodle` it does (positive control)
- [ ] 2a-4b.11 import-linter: `vektra_lms_*` is forbidden to every platform package
- [ ] 2a-4b.12 baseline: delete the `vektra-assess` directory entry and the 7 platform file entries in the same commit; guard green; `rg -il moodle vektra-assess/src vektra-assess/ui/src vektra-shared/src vektra-app/src vektra-admin/src` returns nothing (positive control: the same predicate on `adapters/` returns its files)

### Phase 2b: the adapter's home repository
- [ ] 2b.1 request to the plugin session (see Notes): host `vektra-lms-moodle` in the repository chosen by OD-5, with Python CI, the conformance run and a wheel release
- [ ] 2b.2 after the first published wheel: `.github/workflows/publish.yml` builds the image variant of OD-6, installing the wheel by pinned version and hash; remove `adapters/` and the `lms-moodle` dependency group
- [ ] 2b.3 the image-boundary test (2a-4b.10) asserts the variant's wheel hash

### Phase 3: the costs an operator feels
- [ ] 3.1 (in parallel with 2a-2) `vektra-testkit` (new dev-only distribution, `testkit/` at the root, dev dependency group): `vektra-shared/src/vektra_shared/testing_recorders.py` and `vektra-shared/tests/lms_conformance/` move there; the `ProviderRegistry` refusal of test doubles stays; `test_image_content_boundary.py` asserts the production image has no `testing_recorders`
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
- [ ] `vektra-shared/tests/lms_boundary_baseline.toml` contains no `directory` entry and no `file` entry outside `migrations/`. The 8 entries are gone: `vektra-assess`, `vektra-shared/src/vektra_shared/{auth,errors,netguard}.py`, `vektra-app/src/vektra_app/{assess_wiring,assess_worker,main}.py`, `vektra-admin/src/vektra_admin/templates/keys.html`.
- [ ] `uv run pytest vektra-shared/tests/test_lms_boundary.py` is green on `develop` after the last 2a PR.
- [ ] No 2a PR added a baseline entry: `git log -p origin/develop -- vektra-shared/tests/lms_boundary_baseline.toml` shows only `-` lines.
- [ ] `rg -il moodle vektra-assess/src vektra-assess/ui/src` returns 0 files, and the same predicate on `adapters/vektra-lms-moodle/src` returns more than 0 (the predicate is not dead).
- [ ] `vektra_assess` imports no provider: import-linter green with the new contracts, and `python -c "import vektra_assess, sys; assert not [m for m in sys.modules if m.startswith('vektra_lms')]"`.
- [ ] The conformance kit is green on `FakeLmsProvider` and on `MoodleLmsProvider` with the 328 recorded fixtures (4.3 and 5.2), canary sinks included.
- [ ] The vertical's suites (unit, integration, chaos) use no Moodle fixture: `rg -l moodle_ws vektra-assess/tests tests/` returns nothing.
- [ ] With `VEKTRA_ASSESS_ENABLED=true` and `VEKTRA_ASSESS_LMS_PROVIDER` unset, the worker refuses to start, and the setup item `lms_provider_registered` is `MISSING`.
- [ ] The default image contains no `vektra_lms_*` package (image content test), and the `INSTALL_LMS_PROVIDER=moodle` build does.
- [ ] Ratchet 2 also closes in 2a (OD-1 = yes, decided): the baseline file has no entries, and the squash-equivalence test is green.

**Phase 2b**
- [ ] `adapters/` does not exist in this repository, and the image variant installs a wheel pinned by hash.

**Phase 3 (ratchet 2, if not already closed)**
- [ ] `lms_boundary_baseline.toml` contains no entries, and the guard is green.
- [ ] With `VEKTRA_ASSESS_ENABLED=false`, a clean cluster has 0 `assess_*` tables, 0 `vektra_assess_*` roles and 0 assess functions after `docker/entrypoint.sh` ran (counted with `\dt assess_*`, `\du vektra_assess*`, `\df`), and the platform migrates without CREATEROLE.
- [ ] The production image contains no `testing_recorders` module.
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
  - **Open**: no test yet proves `checked.lms.aclose()` at shutdown; the fake answers `not_found` where Moodle's `resolve_in_course` answers `activity_not_in_course` (a neutral constant for it would let a fake-based test reach `api/teachers.py`'s definitive branch).
