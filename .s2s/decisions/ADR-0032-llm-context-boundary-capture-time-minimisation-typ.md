# ADR-0032: LLM context boundary: capture-time minimisation, typed ContextBundle, direct DiscussionTurnRunner outside QueryPipeline, allowlist retrieval locked by default

**Status**: accepted
**Date**: 2026-11-01
**Context**: design session `20261101-143802-design-assessment-discussion-chatbot` (assessment discussion chatbot); covers ARCH-069, ARCH-074, ARCH-072

## ARCH-069: BR-011 disclosure boundary enforced at snapshot capture and in a single typed assemble_context

### Context

BR-011 and NFR-017: 'a prompt instruction is not a control'. Excluded data must be provably absent from model input, from any later code path, and from preview.

### Decision

LAYER 1, capture-time minimisation: with "mostra soluzione" OFF, the WS side neither fetches
nor stores the following:
- the solution text;
- correct answers, marks or feedback hidden by review options;
- unreleased grades;
- rubric descriptors that are not visible to students, or whose visibility is unreadable.
The snapshot stores only the permitted projection, which is safe because it is immutable.
LAYER 2: one pure function, assemble_context(snapshot, frozen_policy), returns a ContextBundle.
The discussion LLM adapter accepts ONLY that type, and an import-linter rule forbids any other
caller.
VERIFICATION: canary fixtures cover every forbidden field, question type and assignment shape.
They are checked on the exact request bytes captured at a recording LLMProvider, plus logs,
QueryTrace, transcripts and the audit store, in CI and in the NFR-023 gate. An "exclusion
applied" audit event records the excluded-field-set hash, never content.

### Rationale

Converged (SA, SEC explicit; TL a fail-closed field allowlist; DevOps a log-canary test).

### Options considered

- **Capture-time + single typed assembly** — pros: data never present cannot leak via injection, logs or preview; one verifiable choke point; cons: toggle changes after Start cannot widen the context (consistent with BR-008)
- **Assembly-time filtering only** — pros: simpler capture; cons: excluded fields persist in the snapshot, a dormant leak

### Consequences

- (+) GDPR Art. 25 minimisation
- (+) NFR-017 verifiable on bytes
- (−) the canary fixture matrix is a maintained asset (OQ-027 owner)

## ARCH-074: Direct DiscussionTurnRunner (not a QueryPipeline), emitting QueryTrace, with an evaluation adapter

### Context

CONF-002. QueryPipeline.execute(QueryRequest) -> (QueryResponse, QueryTrace) is question-centred. Carrying a ContextBundle there breaks Liskov (SimpleQueryPipeline would silently run RAG), weakens ARCH-069 to a runtime check, and creates a registrable dormant route from /api/v1/learn/query.

### Decision

- vektra_assess.llm.DiscussionTurnRunner provides run(bundle: ContextBundle, turn: StudentTurn)
  -> (TurnResult, QueryTrace) and run_stream(...). It uses LLMProvider.stream with SafeguardHook
  pre_query/pre_response and the sentence-buffered REQ-096/REQ-097 post-check.
  post_retrieval is called only on the RAG-ON branch.
- It is NOT a QueryPipeline and NOT registered in the ProviderRegistry.
- It writes an existing-model QueryTrace (pipeline='assess-discussion', retrieval fields
  explicit, content-free) through the vektra_analytics write API. A 0.5-day spike checks that
  the QueryTrace retrieval fields are optional; if not, add nullable fields in the root chain,
  with no new trace type.
- NFR-015 evidence: the structural absence of a VectorStoreProvider reference, plus a
  recording VectorStoreProvider and a recording LLMProvider (exact request bytes) in CI and
  in the NFR-023 gate. The trace's self-reported counts are never the evidence.
- The NFR-023 harness drives the runner through tests/eval/assess_adapter.py, which builds
  bundles from seeded snapshots via assemble_context. If a Protocol is wanted later, it is a
  NEW Protocol in vektra_shared, never the query_pipeline slot.

### Rationale

Resolved 4/4 (TL conceded on the real signature; SA conceded the trace).

### Options considered

- **Direct typed runner + trace + eval adapter** — pros: ARCH-069 choke point stays static; no dormant registry route; traced and evaluable; cons: one adapter in the eval harness
- **DiscussionPipeline under QueryPipeline** — pros: harness reuse unchanged; cons: Liskov failure; runtime type check; registrable from /query; or an ADR-0014 change

### Consequences

- (+) single typed path, verifiable on bytes
- (−) possible additive QueryTrace schema change

## ARCH-072: Discussion retrieval isolation and RAG activation preconditions

### Context

FEAT-026 keeps hidden chunks retrievable and in the prompt. Moodle WS visibility is relative
to the caller (the service account), not the student. Solution tagging is a denylist, so
untagged chunks pass. NFR-015 requires exclusion before ranking, evaluated at query time.

### Decision

Round-1 mechanism stands (no VectorStoreProvider reference with RAG OFF; separate retrieval
function with a mandatory per-student cmid allowlist; unbound chunks excluded; FEAT-026 and
raw_filters forbidden; canaries proved in QueryTrace on pgvector and Qdrant). Amendments:
- Visibility is read UNCACHED on every RAG turn (TTL 0), per NFR-015's "next query".
- ACTIVATION (CONF-005): the REQ-077 field ships. A teacher may save RAG ON only if ALL of the
  following hold:
  (1) the operator feature flag is ON;
  (2) the per-student visibility reader is READABLE in the ARCH-070 matrix (OQ-031(1));
  (3) the canary suite passes on that site's active provider AND active index_version;
  (4) the target namespace is dedicated, has a cmid binding on every chunk, and contains no
      FEAT-026 and no unbound chunks.
- Otherwise BR-013 validation REJECTS the save with a reason. ON is never silently run as OFF.
- If a precondition later becomes false (new index_version, Moodle release change, failed
  canary re-run), RAG is disabled for new turns and the change is audited. Canaries re-run on
  every index_version activation.
- Shipped and default state: OFF. In practice the pilot runs RAG OFF.

### Rationale

CONF-005 resolved 4/4. No scope change, because REQ-077's field and its default OFF are preserved.

### Options considered

- **Allowlist per-student visibility** — pros: untagged and legacy chunks excluded by construction; respects ADR-0026; cons: one WS call per RAG turn; depends on per-student availability readability
- **P7 as written (service-account visibility + solution tag denylist)** — pros: simple; cons: caller-relative visibility and denylist both fail open

### Consequences

- (+) NFR-015 verifiable in QueryTrace
- (−) RAG ON may stay unavailable if per-student visibility proves unreadable

### Amendment 2026-11-04: allowlist extraction of question text and response

Status: accepted by the coordinator on 2026-11-04 (plan assess-context).

Context: plan 4's readers expose per quiz question only number, type, state, status, mark and
maximum; the question text and the student's answer exist only in the review HTML, which carries
teacher-visibility material (right answer, all feedback) whatever the student's review options. The
structured-only fallback of BR-011 therefore removed the question content from every quiz
discussion.

Decision: the sidecar's sanitizer gains an ALLOWLIST mode. Capture sends each question's review
HTML with the question type, and the sidecar returns only the text of the `qtext` block and of the
student's response (`answer` for the response-entry types, the selected choice for the choice
types), as plain text; every other block is discarded, and so is anything it does not recognise.
The HTML still never leaves the capture frame towards a model, log or store: only the extracted
text enters the snapshot. Types whose kept blocks can embed answer material (match, multianswer,
gapselect, ddwtos and other drag types) are enabled only once a per-type canary proves the right
answer and the hidden feedback absent; until then they stay structured-only and are flagged.

Consequences:
- (+) quiz discussions regain the question and the answer the student gave
- (+) still capture-time minimisation: the extraction is an allowlist, an unknown block is dropped
- (−) depends on Moodle's question markup (CSS classes); a Moodle upgrade can change it, so the
  per-type canaries run on every recorded release and a shape change fails closed to
  structured-only

