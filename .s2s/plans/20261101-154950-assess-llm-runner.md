# Implementation Plan: vektra_assess.llm — DiscussionTurnRunner, post-check, prompts and the NFR-023 gate

**ID**: 20261101-154950-assess-llm-runner
**Status**: completed
**Branch**: feat/assess-llm-runner-core (PR #150, merged), feat/assess-eval-gate (PR B)
**Created**: 2026-11-01T14:49:50Z
**Updated**: 2026-11-06T01:50:00Z
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
- [x] Implement `run_stream(bundle, turn) -> AsyncIterator[ReleasedSegment | TurnResult]`: pre_query → render → `llm.stream(temperature, max_tokens, seed)` → first-token timeout guard → sentence buffer → post-check → per-sentence pre_response → yield; finally yield one `TurnResult` with `content_hash` = SHA-256 of the delivered text; `run()` consumes `run_stream`
  Done 2026-11-05: `runner.run_stream` / `run`; the per-sentence stages live in `postcheck/gate.py` (`SentenceGate`: markers -> class core sentence + at most one contextual sentence -> lexicon (whole sentence -> BR-009 notice in the reply language, `grade_claim_replaced` audited with the category) -> values -> citations -> feedback quotes), then `pre_response` per sentence (`partial=True`; disallow or modify -> `[risposta sostituita dal sistema]`), once more on the whole turn (`partial=False`, audit only). `pre_query` disallow -> no model call, audited; `modified_content` is what the model sees. `SafeguardContext.key_scope='assess:session'`, namespace `assess-discussion`. Reply language from `detect`; `other` -> Italian plus the shipped notice. First-token timeout and call records are in place (A8 adds the cancellation path and the timing tests). **Open**: `run` returns `TurnResult` only; the `QueryTrace` half of `(TurnResult, QueryTrace)` comes with the trace steps (C). No hard word cap per turn (REQ-100 is a Should; `max_tokens` bounds it). Tests: `test_llm_runner.py` (+8).
- [x] Implement `run_part_b(bundle) -> (TurnResult, QueryTrace)` using `part_b.j2` and Part B post-check mode; raises `PartBRejectedError` (content-free reason) on any Part B violation (one regeneration attempt within the caller-supplied remaining budget, then fail)
  Done 2026-11-05: `run_part_b(bundle, language='it', remaining_s=None) -> TurnResult`: generated whole (never streamed to the student), stream stopped past 120 words; checked whole by markers (any marker fails), citations (an unresolved tag fails), lexicon, feedback quote, `part_b.check` and `pre_response`; one regeneration if `remaining_s` allows; second failure -> `PartBRejectedError(<first code>)`; each rejection audited as `assess_part_b_rejected` (attempt, codes). As for turns, the `QueryTrace` comes with C. Tests: `test_llm_runner.py` (+9).
- [x] Implement `ProviderCallRecord` capture and `ProviderOutageError` (error or no first token within `VEKTRA_ASSESS_OUTAGE_TIMEOUT_S`); cancellation (client gone, deadline + grace hit by caller) closes the provider stream and returns a `TurnResult` with `interrupted=True` and only the released text
  Done 2026-11-05: every provider call yields a record (`ok`, `error` + exception type name only, `first_token_timeout`, `cancelled`); an error before or after the first token, or no first token within `VEKTRA_ASSESS_OUTAGE_TIMEOUT_S`, raises `ProviderOutageError(record)`; a slow but progressing stream is not an outage (the timeout guards the first token only). Cancellation: `run_stream(..., stop: asyncio.Event)`; once set, the next-chunk wait is abandoned, the provider stream is closed (`aclose`, on every path), the unfinished sentence is dropped and the result is `interrupted=True` with only the released text. Tests: `test_llm_runner.py` (+5, real short timings rather than a fake clock).
- [x] Implement token accounting via `llm.count_tokens` on the rendered prompt and the completion; optional price table; `TurnUsage.estimated=True`
  Done 2026-11-05: `TurnUsage(prompt, completion, estimated=True, cost)`; turn prompt = the budgeted prompt (with per-message overhead), completion = the RAW model output (what is paid, not the delivered text); Part B sums every generation, a rejected one included; `cost` from `VEKTRA_ASSESS_LLM_PRICE_PER_1K_IN/OUT` (a missing side counts 0), None without a price table (on-prem). Tests: `test_llm_runner.py` (+4). Group A complete.

### B. Post-check and behaviour strings
- [x] Implement `llm/postcheck/sentences.py` (segmenter aware of decimals, `p.12`, `3.5/10`, tags, quotes) with a fixture set of math/numerical and page-reference text
  Done 2026-11-05: incremental `split(buffer, final)` (stateless rescan: the caller keeps the rest); holds a trailing point, open quotes and open tags; a quoted sentence closes ours only when a capital follows. Fixture set `tests/fixtures/llm/sentences.json` (20 cases: decimals, `3.5/10`, `p.12`, matrices, formulas, quotes, markers, IT/EN abbreviations), each also fed one character at a time. Tests: `test_llm_sentences.py` (43).
- [x] Implement `postcheck/markers.py` (CLASS and PUNTO_CONTESTATO grammar; strip; validate labels against the bundle label index; drop and audit invalid)
  Done 2026-11-05: `markers.extract(sentence, labels) -> Markers(text, behaviour_class, contest, dropped)`; codes `marker_class_unknown`, `marker_contest_label_unknown`, `marker_contest_malformed` are what the runner audits (never the model's label text). Only `CLASS` and `PUNTO_CONTESTATO` are markers: a matrix `[[1, 2], [3, 4]]` stays text. Tests: `test_llm_markers.py` (15).
- [x] Implement `postcheck/values.py` (scoped grade/mark/point/level patterns IT+EN; compare with the cited item; fixed replacement string; mark)
  Done 2026-11-05: `values.value_index(bundle)` (label -> visible (value, max) pairs and singles; a hidden mark is absent, so stating it is always replaced) and `values.check(sentence, index)`. Scope: a number next to punti/punteggio/voto/valutazione/livello/mark/score/point/grade/level, or a ratio `a/b` in a sentence that talks about grades (checked as a pair). A value with no cited item is replaced. False-replacement fixture `tests/fixtures/llm/no_values.json` (20 math/page/units sentences IT+EN): 0 replacements (<= 1% asserted). Tests: `test_llm_values.py` (34).
- [x] Implement `postcheck/citations.py` (tag resolution; `[Materiale:]` always removed in v1) and `postcheck/feedback_quote.py` (exact-substring rule)
  Done 2026-11-05: `citations.check(sentence, labels)` keeps tags that resolve (case and spacing tolerant; `[Consegna p.X]` with X > 0 resolves to `Consegna`), removes the rest and every `[Materiale: ...]`, leaves non-tag brackets alone. `feedback_quote.feedback_index(bundle)` (quiz review text, criterion remark, overall feedback as in the bundle) and `feedback_quote.check`: a quote after "Commento del docente" must be a whitespace-normalised substring of the cited item's feedback (any item when none is cited), else `[commento del docente non verificato: vedi la pagina di revisione]`. Tests: `test_llm_citations.py` (18).
- [x] Implement `postcheck/lexicon.py` (verdict, merit, estimate, grade-change, teacher-intent, person-directed judgement and sarcasm lists IT+EN; `LEXICON_VERSION`); a replacement emits the BR-009 notice and the content-free `grade_claim_replaced` audit event
  Done 2026-11-05: `lexicon.PATTERNS` (categories verdict, grade_change, merit, estimate, teacher_intent, person, tone; IT+EN), `scan(sentence) -> category | None`, `LEXICON_VERSION` = SHA-256[:8] of the patterns, now passed by the runner to `PromptRenderer`. The BR-009 replacement and the `grade_claim_replaced` audit (category only) are applied by the runner in `run_stream` (A6). Fixture `tests/fixtures/llm/lexicon.json`: >= 3 hits per category plus 15 clean contest/explanation sentences (REQ-097 RELATE wording such as "pertinente" passes). Tests: `test_llm_lexicon.py` (57).
- [x] Ship `llm/strings/it.yaml` and `en.yaml`: BR-009 statement, core sentence + alternative clause per class, missing-data phrases, unsupported-language sentence, default support text; loaded at import, validated by a schema test (every class × language present); expose a read-only accessor for the plugin/UI (plans 9, P2) so Moodle strings cannot override them
  Done 2026-11-05: `strings/{it,en}.yaml` + `llm/shipped.py` (`get(lang)` read-only `Shipped`, Italian for unsupported languages; `refusal(class, support_text)` = core + alternative; import fails with `ShippedStringsError` on a missing class or key). Single source asserted: it `br009` == `context.strings.BR009_TEMPLATE`, `missing.*` == the post-check replacement constants. Wording is a draft for UX (REQ-098: validated in the pilot). `pyyaml` added to vektra-assess deps, `yaml` to the mypy ignore list. Tests: `test_llm_shipped.py` (21).
- [x] Implement `llm/language.py` (stopword detector IT/EN/other; ties default IT) with a 200-message mixed fixture set
  Done 2026-11-05: stopword vote `detect(text) -> it | en | other` (other needs >= 2 stopwords of FR/ES/DE and to outvote both IT and EN; ties and no signal -> it). Fixture `tests/fixtures/llm/languages.json`: 70 IT, 70 EN, 60 other (FR/ES/DE) student messages; 197/200 correct (3 short 'other' messages read as Italian, i.e. the safe default), >= 95% per language asserted. Tests: `test_llm_language.py` (10).
- [x] Implement Part B checks (0 values, <= 120 words, <= 3 labels) in `postcheck/part_b.py`
  Done 2026-11-05: `part_b.check(text) -> PartBCheck(ok, violations, words, labels)` with codes `part_b_value` (any grade-bound number or any ratio, via `values.asserts_any_value`), `part_b_words` (> 120, brackets are not words), `part_b_labels` (> 3 distinct labels), `part_b_empty`. Limits come from `prompts.PART_B_MAX_*`, the same numbers the template states. Tests: `test_llm_part_b.py` (12).

### C. Trace, safeguard wiring and composition roots
- [x] Migration (next revision in the chain): `ALTER TABLE query_traces ADD COLUMN pipeline VARCHAR(32) NULL`; seed `namespaces` row `assess-discussion`; downgrade drops both; add `pipeline` to `QueryTraceOrm` and optional `pipeline` to `vektra_shared.types.QueryTrace` (last field, default None); `AnalyticsService.store_trace` persists it
  Done 2026-11-05: revision `0019_assess_trace_pipeline` (column `pipeline VARCHAR(32) NULL`; namespace `assess-discussion` with `config.reserved=true`, `ON CONFLICT DO NOTHING`; downgrade deletes the assess traces, the namespace, the column). `QueryTraceOrm.pipeline`, `QueryTrace.pipeline: str | None = None` (last field), `trace_from_dict` and `AnalyticsService.store_trace` / `_orm_to_trace` carry it. Tests: `vektra-analytics/tests/test_service.py` (+1), `vektra-assess/tests/integration/test_trace_pipeline_db.py` (3, real Postgres: column shape, seed row, round trip, downgrade counted).
- [x] Make existing analytics aggregation queries filter `pipeline IS NULL` and add a test that an assess trace does not change `/api/v1/analytics` RAG metrics (row counts before/after)
  Done 2026-11-05: `AnalyticsService` reads (`get_metrics` and its p95 / retrieval score / model distribution, `list_traces`, `get_trace`) filter `pipeline IS NULL`; `delete_before` (retention) is deliberately unfiltered. Integration test `test_assess_traces_do_not_move_the_rag_analytics`: total, average latency, model distribution and listed ids identical before and after inserting an assess row (whose presence is counted), and `get_trace` of the assess id returns None.
- [x] Make the ingest/namespace API refuse the reserved `assess-discussion` namespace id (error envelope, test)
  Done 2026-11-05: `vektra_shared.namespace.RESERVED_NAMESPACES` / `is_reserved` (case and space insensitive) / `refuse_reserved_namespace` -> `ERR-NAMESPACE-001` (403, REQ-010 envelope). Wired into every content or config write: `POST /api/v1/ingest` (query or form namespace), `POST /api/v1/ingest/batch`, learn `POST /content/ingest`, admin `PATCH /api/v1/admin/namespaces/{id}/config`, admin UI namespace create. Tests: `vektra-ingest/tests/test_api.py` (+3), `vektra-shared/tests/test_namespace_reserved.py` (2), `vektra-admin/tests/test_integration.py` (+1, Postgres: 403 and the stored config unchanged). The admin integration file takes about a minute per test locally: a `-k namespace_config` run exceeds 10 minutes without hanging (checked test by test).
- [x] Build steps: `safeguard_pre_query`, `build_prompt` (prompt tokens, history turns kept), `llm_call` (first_token_ms, completion tokens, outcome), `postcheck` (replacements by stage, class, language), `safeguard_pre_response`; `chunks_retrieved=[]`, `llm_model`, `prompt_version`
  Done 2026-11-05: turn trace steps `safeguard_pre_query` (allowed, modified), `build_prompt` (prompt tokens, history kept / dropped), `llm_call` (first_token_ms, completion tokens, outcome), `postcheck` (replacements by stage, class, language, released count; gate time), `safeguard_pre_response` (sentences, safeguard replacements); Part B traces `llm_call` + `postcheck` (violation codes) per attempt, accepted or rejected. `chunks_retrieved=[]`, `pipeline='assess-discussion'`, namespace `assess-discussion`, `response_id = StudentTurn.turn_id` (new field, plan 8 passes the persisted id). Written through `TraceWriter` when `VEKTRA_ASSESS_STORE_TRACES` (default on); a writer failure is audited (`assess_trace_write_failed`), never fails the turn. **Deviation**: the trace rides on `TurnResult.trace` instead of `run()` returning a `(TurnResult, QueryTrace)` tuple (one return shape for `run`, `run_stream` and `run_part_b`). Tests: `test_llm_runner.py` (+4; the trace JSON is grepped for student, reply and label text: 0 hits).
- [x] vektra_app: `build_assess_safeguard()` (fail-closed) and `build_assess_runner(settings, registry)` used by `create_app` (API) and by `vektra_app/assess_worker.py` (Part B); assess LLMProvider built from `VEKTRA_ASSESS_LLM_*` (dedicated model, no fallback list); trace writer adapter over `AnalyticsService.store_trace` (worker: verify the worker sys.modules assertion from plan 3 permits `vektra_analytics` and `litellm`; if not, a raw-SQL trace writer in vektra_app)
  Progress 2026-11-05 (API half done): `vektra_app/assess_llm.py` with `build_assess_safeguard(mode)` (Passthrough from a non-`none` mode -> `StartupValidationError`; `none` only via `VEKTRA_ASSESS_SAFEGUARD_MODE=none`, default `presidio`), `build_assess_llm` (dedicated LitellmProvider, `VEKTRA_LLM_FALLBACK_MODEL` / RAG base / key overridden to None; new `VEKTRA_ASSESS_LLM_API_BASE`), `AnalyticsTraceWriter`, `build_assess_runner`. Tests `vektra-app/tests/test_assess_llm.py` (7). Worker half done 2026-11-05 after the user's decisions: (1) `build_worker_part_b_runner` uses an EXPLICIT `PassthroughSafeguard` (the worker import ceiling excludes Presidio/spaCy; Part B has no student input and its output is rejected by the deterministic Part B checks); a subprocess test shows the module pulls no presidio/spacy/vektra_analytics. (2) Revision `0020_assess_worker_trace_grant` re-renders the grant matrix with `grants.EXTERNAL` (INSERT on the nine trace columns of `query_traces` for `vektra_assess_worker`, no SELECT) plus a RESTRICTIVE RLS policy `query_traces_assess_worker_only` (namespace and pipeline must be `assess-discussion`); `WorkerTraceWriter` is raw SQL as the worker role. Grant-matrix catalog test now checks non-assess tables against `EXTERNAL` (21 integration tests pass); `test_the_worker_can_insert_an_assess_trace_and_nothing_else`: the worker's assess row lands, a RAG-looking row, a read and an API-role insert are all refused by the database.
- [x] Add startup step `_step_N_assess_llm_check`: refuses start if the safeguard fell open, if `VEKTRA_ASSESS_LLM_MODEL` is unset while assess is enabled, or if the active (provider, model, prompt_version) has no passing activation (see E)
  Done 2026-11-05 (safeguard and model halves): `vektra_app.assess_llm.step_13_assess_llm_check`, run by `create_app` after step 12: dormant when assess is disabled; enabled -> `build_assess_safeguard` (fell open -> `StartupValidationError(step='assess_llm_check')`), `build_assess_llm` (no model -> refuse); keeps `assess_safeguard`, `assess_llm`, `assess_prompt_version` on `app.state` for the plan-8 turn route. The activation half (refuse an unactivated triple) needs the activation table and is done with the NFR-023 gate task in E. Tests: `vektra-app/tests/test_assess_llm.py` (+5).
- [x] Measure the worker idle RSS with litellm imported (ceiling ~300 MB, plan 3 CI check); record the number in Notes
  Done 2026-11-05: the worker startup now imports `vektra_app.assess_llm` (runner, prompts, post-check, shipped strings, jinja2, yaml) next to `LitellmProvider`, so the plan-3 stack test (`tests/assess/test_assess_stack.py`, VmRSS <= 300 MB in the container) and the import-ceiling checks measure it. Local measurement (macOS, psutil RSS after `gc.collect()`): worker modules + litellm 209.1 MB, + runner modules 209.9 MB (+0.8 MB); no forbidden module loaded. The container number comes from the stack test in CI.

### D. CI-tier tests (synthetic, deterministic)
- [x] Add `vektra-assess/tests/fakes.py`: `RecordingLLMProvider` (captures exact request bytes, scripted streams incl. slow, erroring and stalled), `ScriptedHostileLLM` (emits each forbidden pattern: fabricated values, verdicts, unresolved tags, wrong quotes, Part B grades, >120 words), `RecordingSafeguard`, `RecordingTraceWriter`, `RecordingAuditSink`; `conftest.py` imports `vektra_shared.testing.hermetic_env`
  Done 2026-11-05: `tests/fakes.py` (`RecordingLLMProvider` re-exported from vektra_shared, `TimedLLM` slow / stalled / failing before or after the first token, `ScriptedHostileLLM` + `HOSTILE` (11 forbidden classes) + `FORBIDDEN` substrings, `RecordingSafeguard` (counts `post_retrieval`, which raises), `RecordingTraceWriter`, `RecordingAuditSink`); `test_llm_runner.py` now uses them. `conftest.py` already imports `hermetic_env` (plan 1). Self-test `test_llm_fakes.py` (14): every hostile reply carries a forbidden pattern and every pattern is exercised.
- [x] Byte-level release test: with `ScriptedHostileLLM`, assert that the concatenated `ReleasedSegment` bytes contain 0 forbidden patterns and that no segment is yielded before its sentence passed all stages (instrumented ordering)
  Done 2026-11-05: `test_llm_release.py` (15): each of the 11 hostile replies, all of them in one turn, and the same streamed one character at a time release 0 `FORBIDDEN` substrings; an instrumented gate shows every yielded segment passed the gate first and every gate input was a whole sentence; a canary (lexicon switched off) lets `Hai ragione` through, so the zero is not vacuous.
- [x] Canary test on plan 6's fixture matrix: run every fixture through `assemble_context` → runner with `RecordingLLMProvider`; grep request bytes, released text, QueryTrace (as stored JSON) and audit rows for every canary → 0 hits
  Done 2026-11-05 (also group G item 2): `tests/canary/runner.py` replaced the reference runner with `DiscussionTurnRunner` (one student turn via `run_stream` + one `run_part_b`), fed by an `EchoLLM` that replies with every message it received (a model repeating its whole context). Sinks: request bytes (all calls incl. count_tokens), the runner's audit rows appended to capture's, the stored QueryTrace JSON from the trace writer, and a 9th sink `released`. `test_canary_matrix.py`: 11 passed; every sink non-empty (e.g. quiz case: request 10.7 KB, trace 1.3 KB, released 4.8 KB), 0 forbidden canaries, positive controls present, 0 vector-store calls.
- [x] Structural NFR-015 test: AST scan of `vektra_assess/llm/**` finds 0 references to `VectorStoreProvider`, `vector_store`, `post_retrieval`; `RecordingSafeguard` shows 0 `post_retrieval` calls across the suite; a RAG-ON bundle raises `RagNotAvailableError`
  Done 2026-11-05: `test_llm_nfr015.py` (6): AST scan of every file under `vektra_assess/llm` (names, attributes, imports, string literals) finds 0 references, with 4 planted canaries caught; a course-material bundle raises `RagNotAvailableError` from `run` and `run_part_b` with 0 provider calls and 0 safeguard calls. `RecordingSafeguard.post_retrieval` raises on any call, so every runner test in the suite (release, canary matrix, runner) would fail on one: none does.
- [x] Fail-closed tests: Presidio load failure in `build_assess_safeguard` → `StartupValidationError`; `pre_query` disallow → no LLM call (recording provider count 0); `pre_response` modify → marked replacement
  Done 2026-11-05: Presidio load failure through vektra_core's real `create_safeguard` (import made to fail) -> `StartupValidationError` (`vektra-app/tests/test_assess_llm.py::test_a_real_presidio_load_failure_refuses_startup`, plus the step-13 test); `pre_query` disallow -> 0 provider calls (`test_a_refused_query_never_reaches_the_model`); `pre_response` modify -> `[risposta sostituita dal sistema]`, marked (`test_a_rewritten_sentence_is_replaced_and_marked`).
- [x] Outage record tests: error → `ProviderOutageError`; stall 46 s (fake clock) → timeout; slow 40 s progressing → success, no outage
  Done 2026-11-05 with A8 (`test_llm_runner.py`): error -> `ProviderOutageError` (exception type only, no message); no first token within the timeout -> `first_token_timeout`; slow but progressing -> success, no outage; error after the first token -> outage. Timings are scaled (timeout 0.05-0.2 s, real sleeps) rather than a fake clock at 45/46/40 s: the property tested is the same, first-token gap vs total duration.
- [x] Class-core string-match test: for each class × language, a scripted class marker produces the exact shipped core sentence + alternative clause and at most one contextual sentence
  Done 2026-11-05: `test_llm_classes.py` (14 = 7 classes x IT/EN): the released segments are exactly [shipped core + alternative clause, the one contextual sentence]; the model's further sentences are dropped.
- [x] False-replacement fixture set (math, matrices, page refs) → false replacements <= 1% (CI fails above)
  Done 2026-11-05 with B3: `tests/fixtures/llm/no_values.json` (20 math / matrix / page / unit sentences, IT+EN), 0 replacements; `test_false_replacements_stay_under_one_percent` fails CI above 1%.
- [x] Wire CI: `ci-unit.yml` assess filter/job already from plan 1 runs these; add `vektra_assess.llm` to the BUG-027 import module list and the mypy path list if plan 1 did not
  Done 2026-11-05: `ci-unit.yml` `test-assess` already runs every `vektra-assess/tests` unit test (the llm tests included) and `test-app` the vektra-app ones; the mypy path list already holds `vektra-assess/src/vektra_assess`; the BUG-027 import list (`integration.yml`) now adds `vektra_assess.llm`, `.llm.runner`, `.llm.shipped`, `yaml`, `vektra_app.assess_llm`.

### E. Eval adapter, NFR-022 suite and NFR-023 gate
- [x] Create `tests/eval/assess_adapter.py`: loads seeded snapshots from `tests/eval/assess/fixtures/`, builds bundles only via `assemble_context`, drives `DiscussionTurnRunner` with a real LLMProvider built by `vektra_app.build_assess_runner`, scores delivered output; refuses to run a dataset whose `data_basis` is not `synthetic` against a provider whose `VEKTRA_ASSESS_PROVIDER_LOCALITY=cloud` (NFR-022 data basis)
  Done 2026-11-05: `tests/eval/assess_adapter.py` (`load_fixtures` -> `assemble_context` with each fixture's frozen policy; `load_items` (JSONL); `run_item` / `run_dataset` through `DiscussionTurnRunner` built by `vektra_app.assess_llm.build_assess_runner`; scores the DELIVERED text); `check_data_basis` raises `DataBasisRefused` before any request when the manifest's `data_basis` is not `synthetic` and `VEKTRA_ASSESS_PROVIDER_LOCALITY=cloud` (new setting, default `cloud`, the safe side). Seed: `tests/eval/assess/manifest.json` (`data_basis: synthetic`), fixture `quiz-it-01`, `smoke.jsonl`. Tests `vektra-app/tests/test_eval_assess_adapter.py` (4): the refusal counts 0 provider calls; `onprem` may run it.
- [x] Build the synthetic dataset in `tests/eval/assess/`: >= 100 item questions, >= 10 attempts, >= 2 disciplines, quiz + assignment, 4.3 + 5.2 shapes; refusal set >= 20 per class (IT+EN); yes/no probe set; contest 30 adversarial + 30 non-adversarial + 20 bait + 10 submission-embedded; injection >= 30; oracle set (open-explanation, disjoint from yes/no, per question type); W-DISTRESS >= 20; unsupported-language probes; manifest with `data_basis` and a disjointness check
  Done 2026-11-05: deterministic generator `tests/eval/assess/build_dataset.py` (seeded; invented content only) writes 12 fixtures (8 quiz x 10 questions in math and history, 4 assignments x 5 criteria; 4.3 and 5.2; 2 quiz fixtures with marks hidden) = 100 item questions, and `probes/*.jsonl`: explain 100, refusal 154 (22 per class, IT 12 + EN 10; W-DISTRESS included), yes/no 30 (hidden-mark fixtures only), oracle 30 (other fixtures, disjoint texts), contest 90 (30 adversarial, 30 non-adversarial, 20 bait, 10 submission-embedded with a planted 'NOTA PER IL CORRETTORE'), injection 30, unsupported language 9, Part B 12; `manifest.json` records `data_basis: synthetic` and the sizes. `vektra-app/tests/test_eval_assess_dataset.py` (13) holds every minimum, the disjointness rule, fixture coverage and the generator's determinism. **Pending**: dataset sign-off by the eval-suite owner (OQ-027).
- [x] Implement deterministic scorers (HARD classes: fabricated values, unresolved tags, verdict/merit/estimate/grade-change, canary leaks, injection compliance markers, yes/no confirmations, teacher-intent patterns, Part B values/word count, label resolution, register "tu", language match)
  Done 2026-11-05: `tests/eval/assess_scoring.py`: `score(delivered, fixture) -> Scored(violations)` over the DELIVERED text, per sentence, skipping the post-check's own replacement markers; HARD classes `fabricated_value`, `unresolved_tag`, the seven lexicon categories (verdict, merit, estimate, grade_change, teacher_intent, person, tone), `injection_compliance` (planted strings, system-prompt fragments, markers), `yes_no_confirmation` (hidden-item probes), `part_b`, `register` (formal address), `language`, `refusal_core` (shipped core sentence missing); `tally` reports every class. Canary leaks are scored by the NFR-017 matrix (CI tier, real runner) rather than re-implemented here; label resolution feeds the (P) label-agreement metric of the determinism run. Tests `vektra-app/tests/test_eval_assess_scoring.py` (14): clean output 0, each class detected.
- [x] Implement LLM-judge scorers (unsupported claims, teacher intent, refusal correctness, over-refusal, oracle, contested-tag recall, tone) with a pinned judge model and prompt; judge calibration script: Cohen's kappa vs human labels (>= 0.7) and recall on a seeded violation set (>= 20 per HARD class, >= 95%); judge-based HARD zeros are accepted only when calibration passes, else reported BLOCKED
  Done 2026-11-05 (code; calibration data pending): `tests/eval/assess_judge.py`: pinned judge (`VEKTRA_ASSESS_JUDGE_MODEL`, temperature 0, `JUDGE_PROMPT_VERSION` = SHA-256[:8] of the prompt), strict JSON verdict over 7 criteria (HARD: `unsupported_claim`, `teacher_intent`; (P): wrong refusal, over-refusal, oracle, contest tag missed, tone); an unparsable verdict counts as a violation of every criterion. `calibrate` -> PASS / FAIL / BLOCKED: Cohen's kappa >= 0.7 per criterion vs human labels, recall >= 95% on >= 20 seeded violations per judged HARD class; missing human labels -> BLOCKED. Seeded set generated into `tests/eval/assess/calibration/seeded.jsonl` (22 per class). **BLOCKED until the eval-suite owner (OQ-027) supplies `calibration/human_labels.jsonl`.** Tests `vektra-app/tests/test_eval_assess_judge.py` (10).
- [x] Determinism run: 3 reruns → Part A and post-checked values 100% identical; label/class agreement >= 95%
  Done 2026-11-05 (code; the measured numbers come from the staging run): `tests/eval/assess_determinism.py`: `rerun(runner, fixtures, items, n=3)` and `measure` -> Part A byte-identical share, post-checked values identical share (every surviving value is the cited item's snapshot value, in every run), primary-label and class agreement (>= 95% for `passed`). Tests `vektra-app/tests/test_eval_assess_determinism.py` (2): identical reruns pass, label/class drift lowers the agreement and fails.
- [x] Report generator: JSON + Markdown under `tests/eval/assess/results/` with each HARD value, (P) values with Wilson CIs, judge calibration, data-basis record, (provider, model, prompt_version), report SHA-256; human-review queue export (100% judge-flagged + 10% unflagged contest turns)
  Done 2026-11-05: `tests/eval/assess_report.py`: `Report` (triple, judge model + prompt version, data basis, dataset sizes, HARD and judged-HARD counts, (P) rates with 95% Wilson CIs, calibration, determinism, blocked reasons) -> `finalize()` sets `status` PASS / FAIL / BLOCKED (BLOCKED never a pass) and `report_sha256` over the canonical JSON of every other field (`verify_sha` detects an edited report); `review_queue` = every judge-flagged turn + a fixed 10% sample of the unflagged contest turns; `write` -> JSON, Markdown and `.review.jsonl` under `tests/eval/assess/results/` (gitignored). Tests `vektra-app/tests/test_eval_assess_report.py` (7).
- [x] Add `make eval-assess` (staging tier) and a CI-tier `make test` subset that runs the deterministic scorers on `ScriptedHostileLLM` and recorded transcripts (no network)
  Done 2026-11-05: `make eval-assess` -> `tests/eval/eval_assess.py`: data-basis check first, dataset minimums (under -> BLOCKED), model and judge reachability (health check; unreachable -> BLOCKED with 0 stream calls), every probe through the real runner, deterministic scorers, judge on deliveries + seeded set, calibration, 3-rerun determinism on 30 turns, report + review queue; exit 0 only on PASS. CI tier: `vektra-app/tests/test_eval_assess_ci_tier.py` (4) runs the same pipeline with recording providers over the committed dataset (cases collected > 0): a naive model (never refuses, always Italian) scores 0 content violations but fails `refusal_core` and `language`; a hostile reply fails; an unreachable model is BLOCKED. Committed recorded transcripts were not added: the CI tier scores scripted outputs instead (same scorers).
- [x] NFR-023 gate: table `assess_model_activations` (next revision; provider, model, prompt_version, report_sha256, hard_pass, activated_at, activated_by_key_id); admin endpoint `POST /api/v1/assess/admin/model-activations` (admin scope) that accepts a report, recomputes HARD pass from it, refuses on any HARD failure or missing calibration, and audits every attempt (accepted or refused); startup step and plan-8 pre-flight refuse an unactivated triple (fail closed)
  Done 2026-11-05: revision `0021_assess_model_activations` (append-only table, CHECKs on the hex SHA and prompt_version, partial index on passing triples; grant matrix re-rendered: API INSERT+SELECT, worker SELECT, nobody UPDATE/DELETE). `vektra_assess.llm.activation`: `evaluate_report` RECOMPUTES the verdict (SHA over the canonical body, status, every HARD and judge-HARD class 0, calibration PASS, data basis) and never trusts `status`; `is_activated`. Endpoint `POST /api/v1/assess/admin/model-activations` (exact `admin`): appends a row only on a passing report, audits every attempt (`assess_model_activation_accepted` / `_refused`, codes only), refuses with `ERR-ASSESS-004` (422). Startup step 13 refuses an unactivated (model, prompt_version); `VEKTRA_ASSESS_LLM_ALLOW_UNACTIVATED` is honoured only with `VEKTRA_ENV=development` and set, with `SAFEGUARD_MODE=none` and a dev model, in `docker-compose.assess.dev.yml` (the CI stack has neither an activated model nor Presidio). The eval report hashes with `activation.canonical_json` (single canonical form). The plan-8 pre-flight will call `is_activated` (worker role has SELECT). Tests: `test_llm_activation.py` (11), `integration/test_model_activations_db.py` (4, Postgres), grant matrix (16), `vektra-app/tests/test_assess_llm.py` (+4: refused, accepted, a template edit makes a new prompt_version that startup refuses, dev bypass refused outside development), compose dev-file test updated.
- [x] Production tier (metrics only): Prometheus counters for post-check replacements by stage, class distribution, outage records, Part B rejections; content-free labels only
  Done 2026-11-05: `RunnerMetrics` Protocol (types) injected into the runner (no metrics library in vektra_assess): per turn, replacements by stage, behaviour class, provider-call outcomes (outages included, from every raise path); Part B rejections by code. `vektra_app.assess_llm.PrometheusRunnerMetrics` (counters `vektra_assess_postcheck_replacements_total{stage}`, `vektra_assess_behaviour_class_total{behaviour_class}`, `vektra_assess_provider_calls_total{outcome}`, `vektra_assess_part_b_rejections_total{code}`, registered once per process) kept on `app.state.assess_metrics` by step 13 for the turn route. The worker gets none (it has no /metrics). Tests: runner (+2, labels hold no student or reply text), vektra-app (+1, registry samples).

### G. Carried over from plan 6 (assess-context, PR #149)
- [x] Typing guard: add `DiscussionTurnRunner.run` / `run_stream` / `run_part_b` to the mypy case in `vektra-assess/tests/typing/` (a `Snapshot` and a `dict` must each give an arg-type error), next to `bundle_to_model_payload`
  Done 2026-11-05: the mypy case lives in `vektra-assess/tests/test_context_guards.py` (`TYPING_CASE`; there is no `tests/typing/` directory): it now also passes a `Snapshot` and a `dict` to `run`, `run_stream` and `run_part_b`; `mypy --strict` reports exactly 8 arg-type errors (2 serializer + 6 runner).
- [x] Rerun the exported canary matrix (`tests/` canary fixture module) through the REAL runner: request bytes and stored QueryTrace JSON, 0 hits (this is the D "canary test" task; plan 6 used a reference runner)
  Done with the D canary task above (same change).
- [x] Review block truncation indicator: a question/response block cut at the per-block cap, or shortened by the review item JSON cap (72464e2), is flagged in the sidecar reply, `ParserClient.extract_review_item`, `CaptureParser`, `QuizItem`, and gets a truncation note in Part A and the payload (CodeRabbit comments 4471764122, 4479124075)
  Done 2026-11-05: the sidecar's review item reply is `{qtext, response, truncated}` (true when a block came back READ_PARTIAL at its cap, or when the JSON cap shortened a value); `ParserClient.extract_review_item` and `CaptureParser` return `(q, r, truncated)` and refuse a reply without the flag; `capture_quiz` sets `QuizItem.truncated` (only when not degraded); Part A appends `S.TRUNCATED`, the payload a 'non trattarlo come completo' note. Goldens unchanged (no golden item is truncated). Tests: `test_review_block_extraction.py` (+2, flag at the block cap and at the JSON cap), `test_parser_client.py` (flag parsed; a flagless reply degrades), `test_context_payload.py` (+1, Part A line and payload note). Resolves CodeRabbit comments 4471764122 and 4479124075 on PR #149.

### F. Docs
- [x] `docs/reference/api.md`: the model-activation endpoint; `docs/` page on the assess eval suite (how to run, data-basis rule, judge calibration); `tests/eval/README.md` assess section
  Done 2026-11-05: `docs/reference/api.md` (activation endpoint), `docs/reference/error-codes.md` (`ERR-ASSESS-004`, `ERR-NAMESPACE-001` is in shared errors), new `docs/guides/contributors/assess-eval-suite.md` (+ index entry), `tests/eval/README.md` assess section.
- [x] `.env.example`: `VEKTRA_ASSESS_LLM_MODEL`, `VEKTRA_ASSESS_LLM_TEMPERATURE`, `VEKTRA_ASSESS_LLM_MAX_TOKENS`, `VEKTRA_ASSESS_LLM_CONTEXT_WINDOW`, `VEKTRA_ASSESS_OUTAGE_TIMEOUT_S`, `VEKTRA_ASSESS_SAFEGUARD_MODE`, `VEKTRA_ASSESS_STORE_TRACES`, `VEKTRA_ASSESS_PROVIDER_LOCALITY`, `VEKTRA_ASSESS_LLM_PRICE_PER_1K_IN/OUT`; CHANGELOG [Unreleased]
  Done 2026-11-05: `.env.example` and `docs/reference/configuration.md` list every new setting (incl. `VEKTRA_ASSESS_LLM_API_BASE`, `VEKTRA_ASSESS_LLM_SEED`, `VEKTRA_ASSESS_LLM_ALLOW_UNACTIVATED`, judge variables); CHANGELOG [Unreleased] Added (runner, analytics pipeline, reserved namespace) and Fixed (truncation flags).

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

- [x] Byte-capture test: 0 forbidden patterns in released bytes from `ScriptedHostileLLM`, and 0 segments released before the post-check (REQ-096, REQ-097, NFR-022 HARD on CI tier) Evidence: `test_llm_release.py`.
- [x] Canary matrix from plan 6: 0 hits in request bytes, released text, stored QueryTrace JSON and audit rows (NFR-017, BR-011) Evidence: `test_canary_matrix.py` through the real runner (9 sinks).
- [x] AST check: 0 VectorStoreProvider/post_retrieval references in `vektra_assess.llm`; a RAG-ON bundle raises (NFR-015 structural evidence) Evidence: `test_llm_nfr015.py`, plus contract (llm) in `test_boundaries.py`.
- [x] Core sentence + alternative clause present in 100% of scripted refusals per class × language (REQ-098) Evidence: `test_llm_classes.py`.
- [ ] Part B: 0 values and <= 120 words on 100% of the eval set; a forced Part B failure raises `PartBRejectedError` (REQ-094; the ELIGIBLE outcome is asserted in plan 8) **Half done, half BLOCKED**: a forced Part B failure raises `PartBRejectedError` (`test_two_bad_part_b_raise_with_a_content_free_reason`); the 100%-of-eval-set measurement needs the staging run (reference model, OQ-026).
- [x] Post-check false replacements <= 1% on the math/page fixture set (REQ-096 P) Evidence: `test_llm_values.py::test_false_replacements_stay_under_one_percent` (0/20).
- [ ] `make eval-assess` produces a report with every NFR-022 HARD target at 0 / 100% on delivered output, (P) targets with CIs, and the judge calibration (kappa >= 0.7, seeded recall >= 95%) (NFR-022) **BLOCKED**: the command and the report exist and the CI tier runs them; a PASS needs the reference model (OQ-026) and the judge calibration labels (OQ-027).
- [x] An activation attempt with a failing or uncalibrated report is refused and audited; startup refuses an unactivated triple; changing one template string changes `prompt_version` and makes startup refuse until re-activated (NFR-023) Evidence: `test_model_activations_db.py`, `test_llm_activation.py`, `test_changing_a_template_changes_prompt_version_and_startup_refuses`.
- [x] A cloud-locality provider run with a non-synthetic dataset is refused by the adapter before any request (counted by the recording transport: 0 requests) (NFR-022 data basis, NFR-023) Evidence: `test_a_non_synthetic_dataset_is_refused_before_any_request` (0 provider calls).
- [ ] 3 reruns: 100% identical post-checked values; >= 95% label/class agreement (REQ-100) **BLOCKED**: `assess_determinism.py` measures it; the numbers need the staging run (OQ-026).
- [ ] Oracle rate <= 2% per question type reported (REQ-099 P); the D3 disclosure sentence is exported for REQ-077 (consumed by plan 11) **Half done, half BLOCKED**: the D3 sentence is shipped verbatim (`shipped.get(lang).oracle_disclosure`, test against requirements.md) for plan 11; the oracle rate is reported by the judge in the staging run (OQ-026, OQ-027).
- [x] An assess trace stored in `query_traces` has `pipeline='assess-discussion'`, namespace `assess-discussion` and no session/user identifiers (row inspected), and the RAG analytics metrics are unchanged (row counts) Evidence: `integration/test_trace_pipeline_db.py` (row inspected: pipeline and namespace; RAG metrics and listing unchanged), `test_each_turn_writes_one_content_free_trace` (no student or reply text in the trace JSON).
- [x] Presidio load failure → startup refuses (no PassthroughSafeguard in assess) Evidence: `test_a_real_presidio_load_failure_refuses_startup`, `test_step_13_refuses_a_safeguard_that_fell_open`.

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

- **Coordinator decision 2026-11-06 (PR #151 review): degraded start instead of startup refusal.** The activation is recorded through the API itself, so refusing to start without one would leave a first deployment, and every deployment after a `prompt_version` change, unable to activate. Startup step 13 now starts the API degraded (`app.state.assess_llm_activated = False`, `assess_llm_unactivated` logged with its remediation); the safeguard and model checks still refuse startup. Serving stays fail closed: **plan 8's pre-flight (M1.5) must call `is_activated` (honouring `VEKTRA_ASSESS_LLM_ALLOW_UNACTIVATED` in development) before every session start**, so a new activation row takes effect without a restart. `evaluate_report` now enforces the whole NFR-022 contract (full HARD inventories, blocked reasons, dataset minimums, calibration measurements at threshold, integer counts); the eval suite and the gate share it through a consistency test.

- **Completed (2026-11-06)**: part A merged as PR #150 (CodeRabbit: 3 rounds, about 45 findings); part B (group E: eval suite, NFR-023 activation gate, migration 0021) on `feat/assess-eval-gate`, applied as the exact diff between the split point and `backup/assess-llm-runner-full`. Every task is done; the acceptance criteria that need a model run stay **BLOCKED** on OQ-026 (reference model) and OQ-027 (judge human labels): `make eval-assess` PASS, the 3-rerun determinism numbers, the oracle rate, and Part B on 100% of the eval set. Activation must be run on the merged `prompt_version`.

- **CodeRabbit review of PR #150 (2026-11-06)**: about 40 findings across runner, post-check, context capture, ingest and tests, fixed with regression tests. Two left as they are. (a) Line structure of the released text: segments are stripped sentences, so `TurnResult.text` joins them with spaces; keeping the model's line breaks needs the splitter to carry separators, deferred to plan 9 (discussion UI), where rendering is decided. (b) The whole-turn `pre_response` stays audit-only: release is per sentence by design (check-then-release, ARCH-072), and a retraction after release is not possible in a stream. The post-check changes alter `LEXICON_VERSION` and therefore `prompt_version`: any NFR-023 activation must be run on the merged version (PR B). A teacher quote opened after a quote lead is now held until it closes, without the 300-char cap (fail closed, bounded by `llm_max_tokens`).

- **Split into two PRs (2026-11-06, coordinator decision)**: the full branch exceeded the CodeRabbit 100-file limit. PR A (`feat/assess-llm-runner-core`) ships groups A-D, F and the review fixes: the runner, post-check, prompts, composition, trace, migrations 0019-0020. PR B, opened after A merges, ships group E: the eval suite (`tests/eval/assess*`, `make eval-assess`), the NFR-023 activation gate (migration 0021, `vektra_assess.llm.activation`, `POST /api/v1/assess/admin/model-activations`, `ERR-ASSESS-004`, `PROVIDER_LOCALITY`, `LLM_ALLOW_UNACTIVATED`) and their docs. Group E tasks are checked because they are done on `backup/assess-llm-runner-full`; on A's tree they are absent, so A's startup step 13 checks model and safeguard only. The plan is marked completed when B merges.

- **/code-review of the plan diff (2026-11-06), 10 findings, all fixed**: (1) the Presidio safeguard loads its engines lazily and fell open at the first call, so `build_assess_safeguard` now loads them at build time and refuses startup when they fail; (2) shipped text (refusal core, BR-009 notice with its date) is no longer passed to the response safeguard, and anonymised model text is released anonymised instead of replaced whole (disallowed text still is); (3) an unbalanced `[` or quote no longer freezes sentence ends: a bracket never spans a line, any opener older than 300 chars or still open at the end is demoted to text; (4) value and quote replacements use the reply language's shipped phrase; (5) the admin UI cannot delete the reserved namespace; (6) `evaluate_report` never raises (malformed -> refusal with audit), refuses an empty judge section and a prompt_version / SHA that would break the table CHECKs; (7) only the provider's own read failure is an outage, a gate or safeguard bug propagates as itself; (8) Part B is bounded after its first token (stall -> outage `StreamStall`), and a cancelled read is awaited before the stream is closed; (9) Part B uses the shared `_close`; (10) the Part B prompt is counted once, the completion once, one stop waiter per stream. Whole-monorepo unit suite 3034 passed; affected integration tests pass.

- **Order (2026-11-05)**: group B (post-check) is done before A6-A9 (`run_stream`, Part B, outage, token accounting), because the stream releases only post-checked sentences.

- **Drift: the REQ-044 lexicon does not exist.** vektra_core safeguards contain no grade-change
  lexicon (only Presidio and passthrough). REQ-097's "extend the REQ-044 lexicon" is therefore
  implemented as a new, assess-owned lexicon in `vektra_assess.llm.postcheck`. The core RAG
  safeguards are unchanged.
- **Drift: ARCH-074 says "through the vektra_analytics write API"** and COMP-006 lists "vektra_core
  public API" as a dependency. Both contradict ARCH-073(b) and the import-linter rules, so they are
  replaced by the injected TraceWriter and LLMProvider.
- **Measured 2026-11-05: litellm in the worker** is already imported since plan 3 (209 MB locally with it); the runner adds under 1 MB. **Risk: litellm import cost in the worker.** Part B needs an LLMProvider in the worker, so
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
