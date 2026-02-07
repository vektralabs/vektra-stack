# ADR-0020: Composable Jinja2 prompt templates

**Status**: accepted
**Date**: 2026-02-07
**Context**: Pipeline quality analysis 2026-02-07

## Context

REQ-043 requires configurable prompt templates with Jinja2 format and documented variables. ARCH-048 defines prompt versioning (SHA-256 hash). However, no architectural decision defines the template structure: how many templates, what variables each receives, how they compose into a final prompt, or how the QueryPipeline consumes them.

Without this decision, the SimpleQueryPipeline implementation must make ad-hoc choices about prompt structure that affect REQ-043 (configurability), REQ-065 (versioning granularity), and the Phase 2 AdvancedQueryPipeline (which must inherit or override templates).

### The prompt construction problem

A RAG query prompt has distinct logical sections:

1. **System instructions**: LLM role, behavioral constraints, response format
2. **Retrieved context**: formatted chunks from vector search
3. **Conversation history**: previous Q&A turns (if multi-turn)
4. **User question**: the current query

Each section has different change frequency (system rarely changes, context changes per query), different template variables, and different configurability needs (operators want to tune system prompts without touching context formatting).

## Decision

Three composable Jinja2 template files loaded by the QueryPipeline at startup:

```
prompts/
  system.j2          # LLM role and behavioral instructions
  context.j2         # retrieved chunk formatting
  conversation.j2    # multi-turn history formatting
```

### Template variable contracts

| Template | Variables | Note |
|----------|-----------|------|
| system.j2 | `namespace`, `model_name` | Extensible with Namespace metadata (ARCH-047) |
| context.j2 | `chunks` (list of dicts: text, score, doc_id, element_type, metadata), `num_chunks` | Each chunk is a dict |
| conversation.j2 | `turns` (list of dicts: question, answer), `num_turns` | Only rendered if conversation_id present |

### Composition order

Fixed order: system + context (if chunks available) + conversation (if conversation_id present) + user question. The user question is appended directly, not via template.

### Configuration

```
VEKTRA_PROMPT_TEMPLATES_DIR=/app/prompts   # default
```

If a file is not found at the configured path, the built-in default from the Python package is used. Built-in defaults produce correct responses without customization (aligned with REQ-005: 30 minutes from git clone to working query).

### Versioning interaction (ARCH-048)

prompt_version in QueryTrace is the SHA-256[:8] of all three template source files concatenated in fixed order (system + context + conversation). Per-template hashes are recorded in StepTrace metadata for the build_prompt step, enabling per-template quality correlation.

## Options considered

### Three composable template files (chosen)

**Pros**:
- Each section independently customizable (operator changes system prompt without touching context format)
- Per-template versioning enables granular quality correlation
- Phase 2 AdvancedQueryPipeline can add templates (e.g., verification.j2) without changing existing ones
- Clear variable contract per template

**Cons**:
- Three files to manage instead of one
- Fixed composition order may be restrictive for advanced use cases

### Single monolithic template

One file containing all sections with conditional blocks.

**Pros**:
- Single file to manage
- Full control over composition order

**Cons**:
- Cannot version individual sections (system prompt change conflated with context format change)
- Operator must understand and preserve all template logic to change one section
- Phase 2 pipeline must fork the entire template instead of adding a section

### Database-stored templates

Templates stored in PostgreSQL, editable via admin API.

**Pros**:
- No file system dependency
- Runtime hot-reload without container restart
- Per-namespace template overrides possible

**Cons**:
- Adds complexity to Phase 1 (admin API for template CRUD)
- Database dependency for application startup (chicken-and-egg with ARCH-057 step 3)
- Templates are code-adjacent (belong in version control, not database)
- Phase 2 concern at best

## Consequences

### Positive

- REQ-043 fully architecturally covered (template structure, variable contracts, file layout)
- Operators can customize LLM behavior by editing one file without understanding the full pipeline
- Per-template versioning enables precise quality regression detection
- Built-in defaults preserve the 30-minute MVP target

### Negative

- Three files increases the configuration surface (mitigated by built-in defaults)
- Fixed composition order prevents reordering sections (acceptable for Phase 1, AdvancedQueryPipeline can override in Phase 2)
