# ADR-0018: SafeguardResult content modification via modified_content field

**Status**: accepted
**Date**: 2026-02-06
**Context**: Integration readiness analysis 2026-02-06

## Context

SafeguardHook (REQ-044) defines three trust boundary points: pre_query, post_retrieval, and pre_response. Each returns a `SafeguardResult` that the QueryPipeline uses to decide how to proceed.

The original SafeguardResult supported two actions:

1. **Blocking**: `allowed=False` rejects the request entirely
2. **Chunk filtering**: `filtered_ids` removes specific chunks from retrieval results

Phase 2 introduces Presidio for PII anonymization (accepted in EX-014) and Guardrails AI for output validation. Both require a third action: **content modification** - changing the text without blocking the request.

### Concrete scenarios requiring modification

**Scenario 1 - Presidio PII anonymization (pre_response)**:
Input: "Il mio nome e' Mario Rossi, email mario@example.com"
Output: "Il mio nome e' `<PERSON>`, email `<EMAIL>`"
Presidio doesn't block - it anonymizes. The pipeline needs the modified text.

**Scenario 2 - Guardrails AI output correction (pre_response)**:
If the LLM response contains data that fails schema validation, Guardrails AI can correct it (re-ask pattern). The pipeline needs the corrected response.

**Scenario 3 - Content redaction (post_retrieval)**:
Remove specific fragments from retrieved chunks (e.g., source code cited from restricted documents) without filtering the entire chunk. The pipeline needs the redacted text.

### How other frameworks handle this

Analysis of five safeguard/guardrail frameworks:

| Framework | Modification pattern | Positional tracking |
|-----------|---------------------|---------------------|
| Guardrails AI | Single optional field (`value_override` on PassResult, `fix_value` on FailResult) | No |
| Presidio | `text` (modified output) + `items` (list of OperatorResult with start/end/entity_type) | Yes |
| LLM Guard | `sanitized_output: str` + `results_valid: bool` + `results_score: float` | No |
| AWS Bedrock Guardrails | Action-based: modified output returned when action != block | No |
| NeMo Guardrails | Context variables: `output_vars` dict with bot_message | No |

Four of five use a single optional string field for modified content. Only Presidio tracks positional modifications (start/end indexes for each entity replaced).

## Decision

Add `modified_content: str | None = None` to SafeguardResult. When not None, QueryPipeline uses it instead of the original text. Modification details (entity types, positions, operator applied) are recorded in the existing `annotations: dict` field for QueryTrace.

```python
class SafeguardResult:
    allowed: bool = True
    reason: str | None = None
    filtered_ids: list[str] | None = None
    modified_content: str | None = None    # modified text replacing original
    annotations: dict = {}                 # carries modification metadata for QueryTrace
```

Phase 1: PassthroughSafeguard never sets modified_content. Zero overhead.

Phase 2 example (Presidio):
```python
result = SafeguardResult(
    allowed=True,
    modified_content="Il mio nome e' <PERSON>, email <EMAIL>",
    annotations={
        "modifications": [
            {"entity": "PERSON", "start": 18, "end": 30, "operator": "replace"},
            {"entity": "EMAIL", "start": 38, "end": 56, "operator": "replace"},
        ]
    },
)
```

Presidio's positional details go in annotations, not in dedicated typed fields. This keeps SafeguardResult simple while preserving full audit information for QueryTrace.

## Options considered

### Single modified_content field (chosen)

**Pros**:
- Aligns with 4 of 5 industry frameworks (Guardrails AI, LLM Guard, Bedrock, NeMo)
- Minimal type surface - one nullable field
- annotations dict handles arbitrary metadata (positions, entity types, scores)
- Forward-compatible: doesn't constrain Phase 2 implementation choice

**Cons**:
- No typed positional tracking (positions are in annotations as untyped dict)
- If multiple hooks modify content sequentially, only the final text is visible (intermediate states lost)

### Structured modification list (Presidio-style)

```python
class Modification:
    entity_type: str
    start: int
    end: int
    original: str
    replacement: str
    operator: str

class SafeguardResult:
    # ... existing fields ...
    modifications: list[Modification] = []
```

**Pros**:
- Full positional audit trail with typed fields
- Supports undo/replay of modifications
- Multiple modifications explicitly tracked

**Cons**:
- Overfit to Presidio's model - other frameworks (Guardrails AI, LLM Guard) don't produce positional data
- Adds a new type to vektra_shared for a Phase 2 feature
- The pipeline only needs the final text, not the modification list
- Positional data can go in annotations when needed

### No modification support (status quo)

**Pros**:
- Simpler SafeguardResult

**Cons**:
- Presidio anonymization would require a separate code path outside SafeguardHook
- Guardrails AI correction pattern impossible via SafeguardHook
- Breaks the single-responsibility of SafeguardHook as the content safety layer

## Consequences

### Positive

- Presidio, Guardrails AI, and content redaction are implementable as SafeguardHook without pipeline changes
- Single field aligns with industry standard (4/5 frameworks)
- annotations dict provides flexible metadata without type proliferation
- Zero Phase 1 overhead (field is None, never checked)

### Negative

- Sequential hook modifications lose intermediate state (mitigated by logging each hook's annotations separately in QueryTrace)
- Positional modification data is untyped (dict in annotations, not a dedicated type)
