# Implementation Plan: vektra_assess.llm — DiscussionTurnRunner, post-check, prompts and the NFR-023 gate

**ID**: 20261101-154950-assess-llm-runner
**Status**: in_progress
**Branch**: feat/assess-llm-runner
**Created**: 2026-11-01T14:49:50Z
**Updated**: 2026-11-05T16:30:00Z
**Approved**: 2026-11-01 (coordinator)

## Traceability

**Source**: COMP-006, ARCH-074 (with ARCH-069 for the typed input boundary)
**Source Type**: architecture

## References

### Requirements
- REQ-094: Opening message: system facts block + LLM orientation (Part B side) @.s2s/requirements.md
- REQ-095: Item references and explanation scope @.s2s/requirements.md
- REQ-096: Grounding, citation and fidelity @.s2s/requirements.md
- REQ-097: Contest dialogue pattern (C8) @.s2s/requirements.md
- REQ-098: Behaviour classes: refusals, boundaries, distress @.s2s/requirements.md
- REQ-099: Solution-oracle limitation: measurement and teacher disclosure @.s2s/requirements.md
- REQ-100: Language, register, tone, determinism @.s2s/requirements.md
- REQ-083: Per-session abuse and cost limits (per-turn token accounting only) @.s2s/requirements.md
- REQ-086: Mid-session provider outage (provider-call records only; state handling is plan 8) @.s2s/requirements.md
- REQ-073: Contest handling verification (adversarial suite) @.s2s/requirements.md
- NFR-015: Anti-leak exclusion of solution and hidden material (structural RAG-OFF evidence) @.s2s/requirements.md
- NFR-017: Context-assembly exclusion verification (request-bytes canaries at the runner) @.s2s/requirements.md
- NFR-022: Answer quality and safety targets @.s2s/requirements.md
- NFR-023: Behaviour regression gate per model and provider @.s2s/requirements.md

### Architecture
- ARCH-074: Direct DiscussionTurnRunner, not a QueryPipeline, emitting QueryTrace, with an eval adapter @.s2s/architecture.md
- ARCH-069: BR-011 boundary at capture and in a single typed assemble_context @.s2s/architecture.md
- ARCH-072: Discussion retrieval isolation (RAG ON locked off in v1) @.s2s/architecture.md
- ARCH-073: Top-level vektra_assess with import-linter contracts @.s2s/architecture.md
- COMP-006: vektra_assess.llm: DiscussionTurnRunner @.s2s/architecture.md
- COMP-002 / COMP-003: callers (turns in the API, Part B in the worker) @.s2s/architecture.md

### Decisions
- ADR-0032: LLM context boundary: capture-time minimisation, typed assemble_context, direct runner @.s2s/decisions/ADR-0032-llm-context-boundary-capture-time-minimisation-typ.md
- ADR-0020: Composable Jinja2 prompt templates (prompt_version = SHA-256[:8]) @.s2s/decisions/ADR-0020-prompt-template-architecture.md
- ADR-0019: Three-tier RAG evaluation strategy (CI / staging / production tiers) @.s2s/decisions/ADR-0019-rag-evaluation-strategy.md
- ADR-0014: QueryPipeline abstraction (explicitly NOT used here) @.s2s/decisions/ADR-0014-query-pipeline-abstraction.md

### Dependencies
- 20261101-154950-assess-context (plan 6): ContextBundle type, assemble_context(), seeded snapshot fixtures and the canary fixture matrix
- Transitively 20261101-154950-assess-foundations (plan 1: package skeleton, settings, error codes, import-linter contracts) and 20261101-154950-assess-data-crypto (plan 2: audit store)

## Overview

This plan builds `vektra_assess.llm`, the only code path that sends discussion content to a model.
`DiscussionTurnRunner.run(bundle: ContextBundle, turn: StudentTurn) -> (TurnResult, QueryTrace)`
and `run_stream(...)` render composable Jinja2 prompts and call `LLMProvider.stream`. The
SafeguardHook calls `pre_query` and `pre_response`. Every sentence passes a deterministic post-check
(grounding, citation, fidelity, contest lexicon, behaviour-class core sentences) before any byte is
released to the caller. It emits a content-free QueryTrace through an injected trace writer.

The runner is imported by two processes. The API runs student turns (plan 8) and the
assess-worker produces the opening Part B during pre-flight (plan 8). The runner is not a
QueryPipeline and is not registered in the ProviderRegistry. It does not import vektra_core or
vektra_analytics: the composition roots in vektra_app inject everything it needs.

The second half of the plan is evaluation. It adds `tests/eval/assess_adapter.py`, the first
Python-level eval adapter in the repo, the NFR-022 suite with a calibrated judge, and the NFR-023
activation gate. Without a passing run, the system refuses to use a (provider, model,
prompt_version) triple for discussions.

## Design Notes

**Typed input, no RAG branch.** The runner accepts only `ContextBundle`, whose constructor is
private to `vektra_assess.context` (plan 6). The constructor has no retrieval or VectorStoreProvider
parameter, and v1 contains no RAG-ON code at all. If a bundle's frozen policy says
`rag_enabled=True`, the runner raises `RagNotAvailableError` and fails closed. `post_retrieval` is
therefore never called in v1. RAG-ON enablement goes to BACKLOG, so this plan writes no dormant
branch (the "dormant capability" rule). This is the structural NFR-015 evidence: AST and
import-linter checks find no reference to `VectorStoreProvider`, `vector_store` or `post_retrieval`
in `vektra_assess.llm`.

**Release discipline: sentence-buffered, check-then-release.** `run_stream` accumulates
`CompletionChunk.content` into a buffer. A sentence is released only once it is complete. A
sentence ends at `.`, `?`, `!` or a newline outside a number, a quote or a citation tag. A quote
opened after "Commento del docente" is held until it closes. Each released unit has passed every
per-sentence check. No check may retract text that has already been released, so every check is
designed to be decidable per sentence. Whole-turn invariants are enforced by truncation instead:
the word limit stops the stream and drops the rest, and an unresolved tag is removed within its
own sentence. `SafeguardHook.pre_response` runs per sentence with
`context.metadata["partial"]=True`, then once on the full turn for audit only. If it disallows or
modifies a sentence, that sentence is replaced with "[risposta sostituita dal sistema]" and the
turn is marked.

**Post-check stages** (pure functions over a sentence plus the bundle, in `vektra_assess.llm.postcheck`):
1. *Machine markers*: the model is instructed to emit `[[CLASS <code>]]` once at the start, and
   `[[PUNTO_CONTESTATO label="…" status="proposed|confirmed|corrected"]]` lines when needed. These
   markers are parsed and stripped and never forwarded. An unknown class, or a contest label that is
   not in the bundle, is dropped and audited as a content-free event.
2. *Behaviour classes (REQ-098)*: for R-SOL, R-OTHER, R-OFFTOPIC, R-INJECT/META, R-GHOST, R-GRADE
   and W-DISTRESS, the runner itself emits the Vektra-shipped core sentence and its alternative
   clause from `strings/{it,en}.yaml`. It then forwards at most one contextual sentence from the
   model, and the rest is dropped. This makes the "core sentence in 100% of refusals" criterion
   true by construction. Refusal turns carry `TurnResult.behaviour_class`. They earn no credit and
   are never outages; plan 8 consumes this.
3. *Scoped value check (REQ-096a)*: numbers bound by pattern to `punti`, `/max`, `livello`, `voto`
   (and the EN equivalents) are compared with the snapshot value of the label cited in the same
   sentence. A mismatch, or a value with no resolvable label, is replaced with
   "[valore non verificato: vedi la pagina di revisione]" and the turn is marked. Formulas,
   matrices and page numbers are untouched, and that is measured by a false-replacement fixture set.
4. *Citation tags (REQ-096b, REQ-095)*: `[Domanda n]`, `[Criterio n]`, `[Valutazione complessiva]`
   and `[Consegna p.X]` must resolve in the bundle's label index. Unresolved tags are removed and
   the turn is marked. A `[Materiale: …]` tag is always removed in v1 (RAG OFF).
5. *Verbatim feedback (REQ-096c)*: text quoted as "Commento del docente" must be an exact substring
   of the cited item's feedback. Otherwise the quote is replaced with the fixed missing-data phrase
   and the turn is marked.
6. *Contest and verdict lexicon (REQ-097, BR-009)*: verdict, merit-rating, estimate, grade-change
   and person-directed judgement patterns (REQ-100) are matched in IT and EN. A match replaces the
   sentence with the BR-009 notice, the turn is marked, and `grade_claim_replaced` is audited.
   Teacher-intent patterns (REQ-096 HARD) are handled the same way. The REQ-044 lexicon that
   REQ-097 says to "extend" does not exist in code, so it is built here, owned by assess (see Notes).
7. *Part B mode (REQ-094)*: no grade, mark, point or level values at all. The output is capped at
   120 words (the stream stops) and names at most 3 labelled items. A violation fails Part B and
   never truncates it into a commit. The worker caller then rolls back.

**Prompts.** Templates live in `vektra-assess/src/vektra_assess/llm/templates/`:
`system.j2` (role, register "tu", tone, scope, behaviour classes, marker grammar),
`bundle.j2` (snapshot projection in delimited DATA blocks; submission text and files marked
untrusted), `history.j2`, `turn.j2` and `part_b.j2`. A local `PromptRenderer` mirrors
vektra_core `TemplateRenderer`; it is not imported, because ARCH-073(b) forbids that.
`prompt_version` is SHA-256[:8] over the template sources, the core-sentence string files and the
post-check lexicon version. It fits `query_traces.prompt_version String(8)`, so that column needs
no migration. Changing a lexicon or a core sentence therefore also requires a new NFR-023 run.

**Language and determinism (REQ-100).** A dependency-free IT/EN/other stopword detector runs on the
latest student message and selects the reply language. An unsupported language gets Italian
replies, and the runner prepends the explanatory sentence. The defaults are temperature 0.2
(`VEKTRA_ASSESS_LLM_TEMPERATURE`), a fixed `max_tokens` per reply, and `seed` passed through
`**kwargs` where the provider supports it. Assess runs with no model fallback, because a fallback
model would bypass the NFR-023 gate. Any fallback model must have its own activation record.

**Token and cost accounting (REQ-083).** LitellmProvider `_stream_impl` reports no usage. The
runner calls `LLMProvider.count_tokens` on the rendered prompt and on the completion text and
records `TurnUsage(prompt_tokens, completion_tokens, estimated=True)`. Cost comes from an optional
operator price table (`VEKTRA_ASSESS_LLM_PRICE_PER_1K_IN/OUT`) and is null for on-prem models.
Plan 8 owns budget enforcement.

**Provider-call records (REQ-086 input).** Each provider call yields a `ProviderCallRecord` with
started_at, first_token_at, ended_at, outcome (ok / error / first_token_timeout) and an error
class. If no first token arrives within `VEKTRA_ASSESS_OUTAGE_TIMEOUT_S` (45 s), the runner raises
`ProviderOutageError` carrying the record. Slow but progressing streams are not outages. The
runner never decides credit; plan 8 does.

**QueryTrace decision (spike resolved by reading the code).** The QueryTrace dataclass has all
fields required. `chunks_retrieved` accepts `[]`, `prompt_version` fits String(8), but there is no
pipeline field and `namespace_id` is NOT NULL with a FK to `namespaces`. Decision:
(1) Add an additive nullable `pipeline VARCHAR(32)` column to `query_traces` and an optional
`pipeline: str | None = None` field at the end of the dataclass. Assess writes
`'assess-discussion'`. Existing analytics aggregations exclude non-null pipeline rows explicitly,
so RAG metrics do not absorb assess turns.
(2) A reserved namespace row, `assess-discussion`, is seeded by the same migration, and assess
traces use it. No per-site namespaces are created, and no RAG chunk can live there: the ingest API
refuses that id.
(3) The trace carries `response_id = turn_id` and no session, user, site or cmid ids. Step metadata
holds counts, class codes, language and replacement counts only.
Writes go through an injected `TraceWriter` Protocol (`async write(trace, namespace) -> None`)
defined in `vektra_assess.llm`. vektra_app adapts it to `AnalyticsService.store_trace` in the API
and in the worker. Assess traces are gated by `VEKTRA_ASSESS_STORE_TRACES` (default on, content
free), independent of `store_traces_enabled`.

**Fail-closed safeguard construction.** vektra_core `create_safeguard` falls open to
PassthroughSafeguard when Presidio fails to load. A new vektra_app helper, `build_assess_safeguard()`,
raises `StartupValidationError` in that case: it detects a Passthrough result for any configured
mode other than `none`. `none` is allowed only when set explicitly (`VEKTRA_ASSESS_SAFEGUARD_MODE`),
never as a fallback. If `pre_query` returns `modified_content` (PII anonymisation), that text is what
the model sees. `SafeguardContext.key_scope` is set explicitly to `assess:session`; the `admin`
default is never used.

## Tasks

### A. Runner core
- [x] Create `vektra-assess/src/vektra_assess/llm/__init__.py` exporting only `DiscussionTurnRunner`, `StudentTurn`, `TurnResult`, `TurnUsage`, `ProviderCallRecord`, `ReleasedSegment`, `TraceWriter`, `RagNotAvailableError`, `ProviderOutageError`, `PartBRejectedError`; add `types.py` with frozen dataclasses (StudentTurn: text, turn_index, history: tuple[PriorTurn, ...], ui_language; TurnResult: text, marks, behaviour_class, contest_entries, language, usage, call_records, replaced_count, content_hash)
  Done 2026-11-05: `llm/types.py` + exports (test `test_llm_types.py`). `DiscussionTurnRunner` joins `__all__` with the runner task; `TurnResult` also carries `interrupted` (cancellation task) and `ContestEntry`/`PriorTurn` are the element types.
- [x] Add import-linter contracts in root `pyproject.toml`: `vektra_assess.llm` forbids `vektra_core`, `vektra_analytics`, `vektra_ingest`, `vektra_index`, `qdrant_client`, `sentence_transformers`, `torch`; only `vektra_assess.api`, `vektra_assess.worker` (pre-flight) and the tests may import `vektra_assess.llm`; verify `uv run lint-imports` fails on a deliberate violating import before removing it
  Done 2026-11-05: (llm) `vektra_assess.llm imports no retrieval, analytics or ML stack` and (llm-callers) `only vektra_assess.api and vektra_assess.worker import vektra_assess.llm` (source list = every other assess child, held by `test_llm_callers_contract_covers_every_other_child`; vektra_app is allowed as the composition root). Planted imports (analytics and qdrant_client in llm, llm in context and delivery) each break the named contract in `test_boundaries.py`. 20 contracts kept.
- [x] Implement `PromptRenderer` (Jinja2 `StrictUndefined`, autoescape off, sandboxed environment) with templates `system.j2`, `bundle.j2`, `history.j2`, `turn.j2`, `part_b.j2`; compute `prompt_version` (SHA-256[:8] over templates + `strings/*.yaml` + `postcheck.LEXICON_VERSION`); unit test: changing any one input changes the version
  Done 2026-11-05: `llm/prompts.py` + `llm/templates/*.j2` (Italian instructions, reply language as a parameter; Jinja2 `SandboxedEnvironment`, `StrictUndefined`); the context message wraps `bundle_to_model_payload` only, the label list comes from `canonical_labels`. `compute_prompt_version(templates, strings, lexicon_version)`: the lexicon version is a parameter until `postcheck.lexicon` exists (B), which then passes `LEXICON_VERSION`. `jinja2>=3.1` added to vektra-assess deps (already in the lock). Tests: `test_llm_prompts.py` (13).
- [x] Implement history token budgeting in `llm/budget.py` (context window from settings; history trimmed oldest-first, never the bundle; bundle overflow raises, it is never silently truncated)
  Done 2026-11-05: `budget.fit(head, history, tail, count, context_window, max_tokens)` reserves `max_tokens` first, raises `ContextOverflowError` when system + bundle + current message do not fit, keeps history newest first; `PromptRenderer.turn_parts` gives the three parts. Settings `VEKTRA_ASSESS_LLM_{MODEL,TEMPERATURE,MAX_TOKENS,CONTEXT_WINDOW,SEED,PRICE_PER_1K_IN,PRICE_PER_1K_OUT}` and `VEKTRA_ASSESS_OUTAGE_TIMEOUT_S` added to `VektraAssessSettings` (the plan's `AssessLLMSettings` is this class). Tests: `test_llm_budget.py` (5), `test_config.py` (+1).
- [x] Implement `DiscussionTurnRunner.__init__(llm: LLMProvider, safeguard: SafeguardHook, trace_writer: TraceWriter, settings: AssessLLMSettings, audit: AssessAuditSink)`; no registry access; raise `RagNotAvailableError` when `bundle.policy.rag_enabled`
  Done 2026-11-05: `llm/runner.py`; settings are `VektraAssessSettings` (no separate class), `AuditSink` is a Protocol matching the worker audit hook (`async (event, data)`), a missing `VEKTRA_ASSESS_LLM_MODEL` refuses construction. **Drift fixed**: the bundle had no RAG field, so `ContextBundle.course_material` (from the frozen `FrozenFlags.course_material`, default False) is added by `assemble`; `_admit` raises `RagNotAvailableError` on it. `DiscussionTurnRunner` is now exported. Tests: `test_llm_runner.py` (4).
- [ ] Implement `run_stream(bundle, turn) -> AsyncIterator[ReleasedSegment | TurnResult]`: pre_query → render → `llm.stream(temperature, max_tokens, seed)` → first-token timeout guard → sentence buffer → post-check → per-sentence pre_response → yield; finally yield one `TurnResult` with `content_hash` = SHA-256 of the delivered text; `run()` consumes `run_stream`
- [ ] Implement `run_part_b(bundle) -> (TurnResult, QueryTrace)` using `part_b.j2` and Part B post-check mode; raises `PartBRejectedError` (content-free reason) on any Part B violation (one regeneration attempt within the caller-supplied remaining budget, then fail)
- [ ] Implement `ProviderCallRecord` capture and `ProviderOutageError` (error or no first token within `VEKTRA_ASSESS_OUTAGE_TIMEOUT_S`); cancellation (client gone, deadline + grace hit by caller) closes the provider stream and returns a `TurnResult` with `interrupted=True` and only the released text
- [ ] Implement token accounting via `llm.count_tokens` on the rendered prompt and the completion; optional price table; `TurnUsage.estimated=True`

### B. Post-check and behaviour strings
- [x] Implement `llm/postcheck/sentences.py` (segmenter aware of decimals, `p.12`, `3.5/10`, tags, quotes) with a fixture set of math/numerical and page-reference text
  Done 2026-11-05: incremental `split(buffer, final)` (stateless rescan: the caller keeps the rest); holds a trailing point, open quotes and open tags; a quoted sentence closes ours only when a capital follows. Fixture set `tests/fixtures/llm/sentences.json` (20 cases: decimals, `3.5/10`, `p.12`, matrices, formulas, quotes, markers, IT/EN abbreviations), each also fed one character at a time. Tests: `test_llm_sentences.py` (43).
- [ ] Implement `postcheck/markers.py` (CLASS and PUNTO_CONTESTATO grammar; strip; validate labels against the bundle label index; drop and audit invalid)
- [ ] Implement `postcheck/values.py` (scoped grade/mark/point/level patterns IT+EN; compare with the cited item; fixed replacement string; mark)
- [ ] Implement `postcheck/citations.py` (tag resolution; `[Materiale:]` always removed in v1) and `postcheck/feedback_quote.py` (exact-substring rule)
- [ ] Implement `postcheck/lexicon.py` (verdict, merit, estimate, grade-change, teacher-intent, person-directed judgement and sarcasm lists IT+EN; `LEXICON_VERSION`); a replacement emits the BR-009 notice and the content-free `grade_claim_replaced` audit event
- [ ] Ship `llm/strings/it.yaml` and `en.yaml`: BR-009 statement, core sentence + alternative clause per class, missing-data phrases, unsupported-language sentence, default support text; loaded at import, validated by a schema test (every class × language present); expose a read-only accessor for the plugin/UI (plans 9, P2) so Moodle strings cannot override them
- [ ] Implement `llm/language.py` (stopword detector IT/EN/other; ties default IT) with a 200-message mixed fixture set
- [ ] Implement Part B checks (0 values, <= 120 words, <= 3 labels) in `postcheck/part_b.py`

### C. Trace, safeguard wiring and composition roots
- [ ] Migration (next revision in the chain): `ALTER TABLE query_traces ADD COLUMN pipeline VARCHAR(32) NULL`; seed `namespaces` row `assess-discussion`; downgrade drops both; add `pipeline` to `QueryTraceOrm` and optional `pipeline` to `vektra_shared.types.QueryTrace` (last field, default None); `AnalyticsService.store_trace` persists it
- [ ] Make existing analytics aggregation queries filter `pipeline IS NULL` and add a test that an assess trace does not change `/api/v1/analytics` RAG metrics (row counts before/after)
- [ ] Make the ingest/namespace API refuse the reserved `assess-discussion` namespace id (error envelope, test)
- [ ] Build steps: `safeguard_pre_query`, `build_prompt` (prompt tokens, history turns kept), `llm_call` (first_token_ms, completion tokens, outcome), `postcheck` (replacements by stage, class, language), `safeguard_pre_response`; `chunks_retrieved=[]`, `llm_model`, `prompt_version`
- [ ] vektra_app: `build_assess_safeguard()` (fail-closed) and `build_assess_runner(settings, registry)` used by `create_app` (API) and by `vektra_app/assess_worker.py` (Part B); assess LLMProvider built from `VEKTRA_ASSESS_LLM_*` (dedicated model, no fallback list); trace writer adapter over `AnalyticsService.store_trace` (worker: verify the worker sys.modules assertion from plan 3 permits `vektra_analytics` and `litellm`; if not, a raw-SQL trace writer in vektra_app)
- [ ] Add startup step `_step_N_assess_llm_check`: refuses start if the safeguard fell open, if `VEKTRA_ASSESS_LLM_MODEL` is unset while assess is enabled, or if the active (provider, model, prompt_version) has no passing activation (see E)
- [ ] Measure the worker idle RSS with litellm imported (ceiling ~300 MB, plan 3 CI check); record the number in Notes

### D. CI-tier tests (synthetic, deterministic)
- [ ] Add `vektra-assess/tests/fakes.py`: `RecordingLLMProvider` (captures exact request bytes, scripted streams incl. slow, erroring and stalled), `ScriptedHostileLLM` (emits each forbidden pattern: fabricated values, verdicts, unresolved tags, wrong quotes, Part B grades, >120 words), `RecordingSafeguard`, `RecordingTraceWriter`, `RecordingAuditSink`; `conftest.py` imports `vektra_shared.testing.hermetic_env`
- [ ] Byte-level release test: with `ScriptedHostileLLM`, assert that the concatenated `ReleasedSegment` bytes contain 0 forbidden patterns and that no segment is yielded before its sentence passed all stages (instrumented ordering)
- [ ] Canary test on plan 6's fixture matrix: run every fixture through `assemble_context` → runner with `RecordingLLMProvider`; grep request bytes, released text, QueryTrace (as stored JSON) and audit rows for every canary → 0 hits
- [ ] Structural NFR-015 test: AST scan of `vektra_assess/llm/**` finds 0 references to `VectorStoreProvider`, `vector_store`, `post_retrieval`; `RecordingSafeguard` shows 0 `post_retrieval` calls across the suite; a RAG-ON bundle raises `RagNotAvailableError`
- [ ] Fail-closed tests: Presidio load failure in `build_assess_safeguard` → `StartupValidationError`; `pre_query` disallow → no LLM call (recording provider count 0); `pre_response` modify → marked replacement
- [ ] Outage record tests: error → `ProviderOutageError`; stall 46 s (fake clock) → timeout; slow 40 s progressing → success, no outage
- [ ] Class-core string-match test: for each class × language, a scripted class marker produces the exact shipped core sentence + alternative clause and at most one contextual sentence
- [ ] False-replacement fixture set (math, matrices, page refs) → false replacements <= 1% (CI fails above)
- [ ] Wire CI: `ci-unit.yml` assess filter/job already from plan 1 runs these; add `vektra_assess.llm` to the BUG-027 import module list and the mypy path list if plan 1 did not

### E. Eval adapter, NFR-022 suite and NFR-023 gate
- [ ] Create `tests/eval/assess_adapter.py`: loads seeded snapshots from `tests/eval/assess/fixtures/`, builds bundles only via `assemble_context`, drives `DiscussionTurnRunner` with a real LLMProvider built by `vektra_app.build_assess_runner`, scores delivered output; refuses to run a dataset whose `data_basis` is not `synthetic` against a provider whose `VEKTRA_ASSESS_PROVIDER_LOCALITY=cloud` (NFR-022 data basis)
- [ ] Build the synthetic dataset in `tests/eval/assess/`: >= 100 item questions, >= 10 attempts, >= 2 disciplines, quiz + assignment, 4.3 + 5.2 shapes; refusal set >= 20 per class (IT+EN); yes/no probe set; contest 30 adversarial + 30 non-adversarial + 20 bait + 10 submission-embedded; injection >= 30; oracle set (open-explanation, disjoint from yes/no, per question type); W-DISTRESS >= 20; unsupported-language probes; manifest with `data_basis` and a disjointness check
- [ ] Implement deterministic scorers (HARD classes: fabricated values, unresolved tags, verdict/merit/estimate/grade-change, canary leaks, injection compliance markers, yes/no confirmations, teacher-intent patterns, Part B values/word count, label resolution, register "tu", language match)
- [ ] Implement LLM-judge scorers (unsupported claims, teacher intent, refusal correctness, over-refusal, oracle, contested-tag recall, tone) with a pinned judge model and prompt; judge calibration script: Cohen's kappa vs human labels (>= 0.7) and recall on a seeded violation set (>= 20 per HARD class, >= 95%); judge-based HARD zeros are accepted only when calibration passes, else reported BLOCKED
- [ ] Determinism run: 3 reruns → Part A and post-checked values 100% identical; label/class agreement >= 95%
- [ ] Report generator: JSON + Markdown under `tests/eval/assess/results/` with each HARD value, (P) values with Wilson CIs, judge calibration, data-basis record, (provider, model, prompt_version), report SHA-256; human-review queue export (100% judge-flagged + 10% unflagged contest turns)
- [ ] Add `make eval-assess` (staging tier) and a CI-tier `make test` subset that runs the deterministic scorers on `ScriptedHostileLLM` and recorded transcripts (no network)
- [ ] NFR-023 gate: table `assess_model_activations` (next revision; provider, model, prompt_version, report_sha256, hard_pass, activated_at, activated_by_key_id); admin endpoint `POST /api/v1/assess/admin/model-activations` (admin scope) that accepts a report, recomputes HARD pass from it, refuses on any HARD failure or missing calibration, and audits every attempt (accepted or refused); startup step and plan-8 pre-flight refuse an unactivated triple (fail closed)
- [ ] Production tier (metrics only): Prometheus counters for post-check replacements by stage, class distribution, outage records, Part B rejections; content-free labels only

### G. Carried over from plan 6 (assess-context, PR #149)
- [ ] Typing guard: add `DiscussionTurnRunner.run` / `run_stream` / `run_part_b` to the mypy case in `vektra-assess/tests/typing/` (a `Snapshot` and a `dict` must each give an arg-type error), next to `bundle_to_model_payload`
- [ ] Rerun the exported canary matrix (`tests/` canary fixture module) through the REAL runner: request bytes and stored QueryTrace JSON, 0 hits (this is the D "canary test" task; plan 6 used a reference runner)
- [ ] Review block truncation indicator: a question/response block cut at the per-block cap, or shortened by the review item JSON cap (72464e2), is flagged in the sidecar reply, `ParserClient.extract_review_item`, `CaptureParser`, `QuizItem`, and gets a truncation note in Part A and the payload (CodeRabbit comments 4471764122, 4479124075)

### F. Docs
- [ ] `docs/reference/api.md`: the model-activation endpoint; `docs/` page on the assess eval suite (how to run, data-basis rule, judge calibration); `tests/eval/README.md` assess section
- [ ] `.env.example`: `VEKTRA_ASSESS_LLM_MODEL`, `VEKTRA_ASSESS_LLM_TEMPERATURE`, `VEKTRA_ASSESS_LLM_MAX_TOKENS`, `VEKTRA_ASSESS_LLM_CONTEXT_WINDOW`, `VEKTRA_ASSESS_OUTAGE_TIMEOUT_S`, `VEKTRA_ASSESS_SAFEGUARD_MODE`, `VEKTRA_ASSESS_STORE_TRACES`, `VEKTRA_ASSESS_PROVIDER_LOCALITY`, `VEKTRA_ASSESS_LLM_PRICE_PER_1K_IN/OUT`; CHANGELOG [Unreleased]

## State & Data Lifecycle

| State element | Created by | Updated by | Invalidated/Deleted by |
|---------------|-----------|------------|------------------------|
| `query_traces` rows with `pipeline='assess-discussion'` | Runner via TraceWriter (C) per turn / Part B | never | Retention: `cleanup_analytics_traces_task` is dormant today (never scheduled), so plan 12 must delete assess traces by `response_id IN (purged turn ids)` at purge, verified by row count |
| `namespaces` row `assess-discussion` | Migration (C) | never | Migration downgrade only |
| `query_traces.pipeline` column | Migration (C) | — | Migration downgrade |
| `assess_model_activations` rows | Activation endpoint (E) | never (append-only; a new triple = new row) | Superseded rows are kept as audit evidence; deleted only by migration downgrade |
| Prompt templates / strings / lexicon (files) | Package (A, B) | Code change → new `prompt_version` | Old versions remain in git; activation keyed by version |
| In-memory sentence buffer | `run_stream` per call | per chunk | Discarded at end of call or cancellation; never logged |
| Eval results files | `make eval-assess` (E) | — | Manual; synthetic-only content (real-data runs stay on-prem and out of git) |

## Acceptance Criteria

- [ ] Byte-capture test: 0 forbidden patterns in released bytes from `ScriptedHostileLLM`, and 0 segments released before the post-check (REQ-096, REQ-097, NFR-022 HARD on CI tier)
- [ ] Canary matrix from plan 6: 0 hits in request bytes, released text, stored QueryTrace JSON and audit rows (NFR-017, BR-011)
- [ ] AST check: 0 VectorStoreProvider/post_retrieval references in `vektra_assess.llm`; a RAG-ON bundle raises (NFR-015 structural evidence)
- [ ] Core sentence + alternative clause present in 100% of scripted refusals per class × language (REQ-098)
- [ ] Part B: 0 values and <= 120 words on 100% of the eval set; a forced Part B failure raises `PartBRejectedError` (REQ-094; the ELIGIBLE outcome is asserted in plan 8)
- [ ] Post-check false replacements <= 1% on the math/page fixture set (REQ-096 P)
- [ ] `make eval-assess` produces a report with every NFR-022 HARD target at 0 / 100% on delivered output, (P) targets with CIs, and the judge calibration (kappa >= 0.7, seeded recall >= 95%) (NFR-022)
- [ ] An activation attempt with a failing or uncalibrated report is refused and audited; startup refuses an unactivated triple; changing one template string changes `prompt_version` and makes startup refuse until re-activated (NFR-023)
- [ ] A cloud-locality provider run with a non-synthetic dataset is refused by the adapter before any request (counted by the recording transport: 0 requests) (NFR-022 data basis, NFR-023)
- [ ] 3 reruns: 100% identical post-checked values; >= 95% label/class agreement (REQ-100)
- [ ] Oracle rate <= 2% per question type reported (REQ-099 P); the D3 disclosure sentence is exported for REQ-077 (consumed by plan 11)
- [ ] An assess trace stored in `query_traces` has `pipeline='assess-discussion'`, namespace `assess-discussion` and no session/user identifiers (row inspected), and the RAG analytics metrics are unchanged (row counts)
- [ ] Presidio load failure → startup refuses (no PassthroughSafeguard in assess)

## Testing Approach

Three tiers per ADR-0019:
1. **CI tier** (hermetic). Recording and scripted-hostile fakes test the release discipline,
   each post-check stage, markers, language and Part B. Canaries run on plan-6 fixtures, plus the
   structural NFR-015 check, the fail-closed safeguard, outage records and token accounting. The
   deterministic scorers also run over a set of recorded model outputs that is committed in the
   repo.
2. **Staging tier** (`make eval-assess`). The real reference model (OQ-026) runs the full NFR-022
   suite per language, with the calibrated judge and 3 reruns for determinism. Its output is the
   activation report.
3. **Production tier**. Metrics-only counters; no content.

The integration test against Postgres (testcontainers pg16, `@pytest.mark.integration`) covers the
migration, the trace persistence round trip and the activation table and endpoint.

### Test Infrastructure

- **Required**: plan-6 snapshot fixtures and canary matrix; recording/hostile fakes (this plan, D); testcontainers Postgres for migration/trace/activation tests; a reachable reference LLM and judge model for the staging tier; human labels for judge calibration
- **Provided by**: fakes and datasets by tasks D and E; fixtures by 20261101-154950-assess-context; LLM endpoint by the operator (OQ-026); human labels by the eval owner (OQ-027)
- **False-pass guard**: `make eval-assess` exits non-zero and writes `status: BLOCKED` when the model or judge is unreachable, when calibration is missing or below threshold, or when any dataset size is under its NFR-022 minimum. A missing dataset is never reported as a pass. CI asserts that the deterministic-scorer test collected > 0 cases.

### NFR Verification

| NFR | Dataset/Load | Tool | Environment | Pass criterion |
|-----|--------------|------|-------------|----------------|
| NFR-015 | Whole `vektra_assess.llm` source + full CI suite | AST scan + RecordingSafeguard call counts | CI | 0 VectorStoreProvider/post_retrieval refs; 0 post_retrieval calls |
| NFR-017 | Plan-6 canary fixture matrix (every forbidden field × question type × shape) | RecordingLLMProvider byte grep + trace/audit grep | CI + NFR-023 staging run | 0 canary hits |
| NFR-022 | Synthetic eval set at NFR-022 sizes, IT + EN | `make eval-assess` with deterministic scorers + calibrated judge | Staging, reference model | All HARD = 0 (parser containment in plan 5); (P) within targets with CI; kappa >= 0.7, seeded recall >= 95% |
| NFR-023 | Same suite per (provider, model, prompt_version) | Activation endpoint + startup step | Staging → production config | Unactivated triple refused (startup + pre-flight); every attempt audited; 0 real records in cloud runs |

## Integration Notes

- **Plan 8** calls `run_stream` from the API turn route and `run_part_b` from the worker pre-flight.
  It consumes `TurnResult.behaviour_class` (no outage credit for refusals), `usage` (budget),
  `call_records` / `ProviderOutageError` (OUTAGE credit) and `interrupted`, and enforces deadline
  plus grace by cancelling the stream.
- **Plan 6** must give `ContextBundle` a label index (canonical labels → item values, feedback
  text, visibility flags), `policy.rag_enabled` and the language fields this plan reads.
- **Plan 10** renders `contest_entries` as "PUNTI CONTESTATI" and the marks ("[valore non
  verificato…]", "[risposta sostituita dal sistema]"), and re-hashes against `content_hash`.
- **Plan 11** takes the D3 oracle disclosure sentence and the BR-009 / core strings for the REQ-077
  help text. **Plan 12** deletes assess traces at purge. **Plans 9 and P2** display the shipped
  strings via the read-only accessor.
- vektra_analytics changes (pipeline column, aggregation filter) are additive. The existing
  `/api/v1/query` traces keep `pipeline=NULL`.

## Notes

- **Order (2026-11-05)**: group B (post-check) is done before A6-A9 (`run_stream`, Part B, outage, token accounting), because the stream releases only post-checked sentences.

- **Drift: the REQ-044 lexicon does not exist.** vektra_core safeguards contain no grade-change
  lexicon (only Presidio and passthrough). REQ-097's "extend the REQ-044 lexicon" is therefore
  implemented as a new, assess-owned lexicon in `vektra_assess.llm.postcheck`. The core RAG
  safeguards are unchanged.
- **Drift: ARCH-074 says "through the vektra_analytics write API"** and COMP-006 lists "vektra_core
  public API" as a dependency. Both contradict ARCH-073(b) and the import-linter rules, so they are
  replaced by the injected TraceWriter and LLMProvider.
- **Risk: litellm import cost in the worker.** Part B needs an LLMProvider in the worker, so
  importing litellm there is the first exception to the minimal worker import graph. Measure it
  (task C) against the ~300 MB idle ceiling.
- **Risk: token counts are estimates.** `count_tokens` falls back to len//4 when litellm has no
  tokenizer for the model (common with Ollama). Budgets (plan 8) must be generous enough to absorb
  this error. Streaming `include_usage` is a possible future improvement.
- **Risk: some HARD zeros depend on the judge.** They cannot be accepted until calibration passes,
  which needs human labels from the OQ-027 owner.
- **OQ-026 (reference model / hardware)** blocks the staging tier and the activation of a
  production triple. The design default is to run on the candidate model and record it in the
  report.
- **OQ-027 (eval-suite owner)** blocks dataset sign-off and human labels: the dataset is a
  maintained asset.
- **OQ-020 (DPO)**: the dataset stays synthetic-only until the DPO approves a data basis.
- **BACKLOG**: RAG-ON for discussions (ARCH-072 activation preconditions, uncached visibility,
  the post_retrieval branch, `[Materiale:]` tags) is out of scope. This plan deliberately ships
  no RAG branch.
