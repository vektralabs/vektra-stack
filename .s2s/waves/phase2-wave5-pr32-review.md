# Wave 5 PR #32 review — post-implementation gap analysis

**Wave**: 5 (Integration)
**PR**: #32 feat(infra): wire Phase 2 providers, Docker, CI, and operator scripts
**Date**: 2026-03-10
**Reviewer**: Claude Opus 4.6

---

## Context

Wave 5 (infra-phase2) is the final integration wave of Phase 2. Its role is to
wire all component-level plans into a single deployable application: register
providers, mount routers, update Docker/CI, and add operator scripts.

During PR review, bot reviewers (Gemini + CodeRabbit) identified issues across
3 review rounds. After fixing all review comments, a final manual audit
revealed **5 implementation gaps** — features explicitly deferred to
"infra-phase2" in their source plans but not included as tasks in the
infra-phase2 plan itself.

---

## Review rounds summary

### Round 1 — Gemini (6 comments)

| ID | File | Severity | Verdict |
|----|------|----------|---------|
| 2908468467 | scripts/reindex.sh | MEDIUM (security) | Fixed — JSON injection via python3 json.dumps |
| 2908468475 | Makefile | MEDIUM (security) | Won't fix — matches existing Phase 1 pattern |
| 2908468477 | docker-compose.yml | MEDIUM (security) | By design — dev-only default per ADR-0004 |
| 2908468482 | scripts/batch-ingest.sh | MEDIUM | Fixed — capture and display error output |
| 2908468491 | vektra-app/main.py | MEDIUM | Won't fix — Pydantic validation sufficient |
| 2908468495 | vektra-app/main.py | MEDIUM | By design — importlib.resources inapplicable |

### Round 2 — CodeRabbit (7 inline + 1 PR-level nitpick)

| ID | File | Severity | Verdict |
|----|------|----------|---------|
| 2913631640 | docker-compose.yml | MINOR | Fixed — CRLF to LF |
| 2913631649 | docker-compose.yml | MAJOR | By design — TEI profile is preparatory infra |
| 2913631656 | Makefile | MAJOR | Fixed — forward VER and NS to reindex |
| 2913631664 | scripts/reindex.sh | CRITICAL | Fixed — add target_index_version, fix status parsing |
| 2913631670 | scripts/reindex.sh | MAJOR | Fixed — clarify skeleton in completion message |
| 2913631671 | vektra-app/main.py | CRITICAL | Won't fix — pgvector is schema dependency (Vector(384) column) |
| 2913631675 | vektra-app/main.py | MAJOR | By design — matches LLM check non-fatal pattern |
| PR-level | docker-compose.yml | NITPICK | Fixed — TEI volume cache |

### Round 3 — CodeRabbit (1 nitpick + 1 duplicate)

| ID | File | Severity | Verdict |
|----|------|----------|---------|
| PR-level | docker-compose.yml | NITPICK | Fixed — TEI profile usage in header |
| PR-level | scripts/reindex.sh | DUPLICATE | Acknowledged — polling retry safe with 600s timeout |

### CI failures fixed

| Error | Fix |
|-------|-----|
| mypy: 10 type errors in analytics/learn | Added type annotations and type:ignore comments |
| shellcheck SC2001 in batch-ingest.sh | Bash parameter expansion instead of sed |
| Missing executable bit on scripts | git update-index --chmod=+x |

---

## Implementation gap analysis

Post-review audit found 5 features explicitly deferred to "infra-phase2" in
their source plans but absent from the infra-phase2 task list.

### GAP-1: Reindex job — skeleton only (CRITICAL)

**Source**: index-hybrid plan, T15 design note (line 206):
> "Actual re-extraction/re-chunking is a placeholder; full implementation
> depends on ingest pipeline availability (ingest-phase2 plan)."

**Current state**: `run_reindex()` iterates source_documents and updates
progress counters, but does not call extract/chunk/embed/store.

**Impact**: The reindex API accepts requests and reports completion, but no
actual reindexing occurs. Operators could switch index versions believing
data was migrated.

### GAP-2: WebhookEventEmitter — implemented but not registered (HIGH)

**Source**: vektra_shared/events.py module docstring:
> "Phase 1: NoOpEventEmitter. Phase 2: WebhookEventEmitter."

**Current state**: `WebhookEventEmitter` class is fully implemented with
HMAC-SHA256 signing. `WebhookConfig` exists in config.py. But `main.py`
always registers `NoOpEventEmitter()` with no conditional check.

**Impact**: Event webhooks are silently discarded even when configured.

### GAP-3: Analytics retention cleanup job (MEDIUM)

**Source**: analytics service.py (line 213):
> "bounded by retention policy (VEKTRA_ANALYTICS_RETENTION_DAYS via
> infra-phase2 cleanup job)"

**Current state**: `analytics_retention_days` config field exists.
`AnalyticsService.delete_before()` method exists. No scheduled job calls it.

**Impact**: QueryTrace data grows unbounded.

### GAP-4: Soft-delete retention cleanup job (MEDIUM)

**Source**: config.py (line 337-342):
> `retention_days` field with description "Phase 2: arq cleanup job"

**Current state**: Config field exists. No scheduled job performs cleanup
of soft-deleted records.

**Impact**: Soft-deleted records remain in database indefinitely.

### GAP-5: Learn ingestion wiring (MEDIUM)

**Source**: vektra_learn/service.py (line 193):
> "The actual ingestion call is wired in infra-phase2 (via ProviderRegistry,
> not a direct import)."

**Current state**: `build_ingest_metadata()` returns metadata dict but the
learn API endpoint does not call the ingest pipeline.

**Impact**: Course content ingestion via learn API is not functional.

---

## Root cause

See lesson: `.s2s/lessons/s2s-plan-integration-gap-tracking.md`

---

## Commits (11 total on branch)

| Hash | Type | Description |
|------|------|-------------|
| 2b89d5c | chore | Start Wave 5 infra-phase2 plan |
| 8081da4 | feat | Wire Phase 2 providers, routers, Docker, and CI |
| bde3a79 | feat | Add reindex and batch-ingest operator scripts |
| 7d7b399 | fix | Correct widget static path and declare missing app deps |
| bd5f49b | fix | Set executable bit on operator scripts |
| bfb4d48 | style | Fix mypy type errors in analytics and learn |
| 8093c38 | fix | Sanitize JSON payload in reindex.sh, show errors in batch-ingest.sh |
| d163be2 | fix | Resolve shellcheck SC2001 in batch-ingest.sh |
| c3021bf | fix | Align reindex script with API contract and fix CRLF |
| bbd988b | style | Add persistent volume for TEI model cache |
| 50d9b82 | style | Add TEI profile usage example in header |

---

## Gap resolution

| Gap | Severity | Resolution |
|-----|----------|------------|
| GAP-1: Reindex job skeleton | CRITICAL | Implemented: run_reindex now re-embeds chunks via EmbeddingProvider + PgvectorProvider with target_index_version |
| GAP-2: WebhookEventEmitter not registered | HIGH | Implemented: conditional registration in main.py when VEKTRA_WEBHOOK_URL is set |
| GAP-3: Analytics retention cleanup | MEDIUM | Implemented: cleanup_analytics_traces_task arq cron in vektra_analytics/jobs.py |
| GAP-4: Soft-delete retention cleanup | MEDIUM | Already resolved: cleanup_soft_deleted_task existed in vektra_ingest/jobs.py |
| GAP-5: Learn ingestion wiring | MEDIUM | Implemented: run_ingest registered in ProviderRegistry, learn endpoint fetches document_url and calls pipeline |

---

## Status

**PR review**: complete (13 inline threads resolved, all CI green)
**Implementation gaps**: 5 identified, all resolved
