# Implementation Plan: Qdrant parity — the chunk paths that lied

**ID**: 20260714-qdrant-parity
**Status**: completed (2026-07-14)
**Branch**: one branch/PR per item (`fix/bug-023-qdrant-chunk-paths` #102, `fix/bug-024-sparse-provider-alias` #101)
**Created**: 2026-07-14T00:00:00Z
**Updated**: 2026-07-14T00:30:00Z

## Traceability

**Source**: BUG-023, BUG-024 (backlog)
**Decisions**: [ADR-0026](../decisions/ADR-0026-document-chunks-pgvector-internal.md)
**Requirements**: REQ-050 (pluggable vector store), REQ-057 (retention), REQ-064 (reindex)
**Architecture**: ARCH-039 (ProviderRegistry), ARCH-045 (index versioning), ARCH-051 (full-store contract), ARCH-057 (startup validation)
**Evidence**: `vektra-internal/stack/20260714-bug023-qdrant-chunk-paths-evidence.md`

## Overview

In Qdrant mode — the configuration every real deployment runs — the Postgres `document_chunks` table
is empty: only `PgvectorProvider` writes it. Every module that still read chunks from Postgres was
therefore operating on an empty table **and reporting success**. These paths did not fail, they lied,
which is why the defect survived for months in plain sight.

The sprint had one non-negotiable rule, set by the operator: **verify empirically, do not trust the
reports** — the class of bug under investigation is precisely one that produces reassuring reports.
Every claim below was measured against the live stack before and after.

## Design Notes

**The decision came before the code** (ADR-0026): `document_chunks` is an implementation detail
private to `PgvectorProvider`. No other module may read it; chunk text, counts and deletion all go
through the `VectorStoreProvider` Protocol.

The rejected alternative — a provider-neutral table that both providers write — would have made
Postgres a **mandatory co-store under every provider**, i.e. exactly the redundancy the pluggable-store
design exists to avoid, plus a dual write across two systems with no transaction spanning them. The
ADR does not change the standing of the two providers: pgvector and Qdrant remain interchangeable
peers. It restores that property rather than altering it.

The boundary, stated explicitly: **chunk text and embeddings** belong to the active vector store;
**document-level records** (`source_documents`, jobs, conversations, API keys) stay in Postgres. This
is why `/stats` healed cleanly: `document_count` was always correct, only `chunk_count` had to move.

Two facts from the data shaped it:
1. `source_documents.chunk_count` was already correct and provider-neutral (12 / 105 / 562, matching
   Qdrant exactly), so no dual write was needed.
2. `PgvectorProvider.store()` honours caller-supplied deterministic chunk ids (`uuid5(doc_id, position)`)
   and FEAT-017 parent linkage depends on them — which is what forced the reindex id/parent remapping
   design below.

## Tasks

### 1. BUG-023 — route every chunk path through the Protocol (branch `fix/bug-023-qdrant-chunk-paths`, PR #102)
- [x] Reproduce all four reported paths on the live Qdrant stack before touching code
- [x] Read the code and find the paths the backlog did **not** list: `POST /documents/{id}/chunks`
      (wrote to the inactive store), `GET /api/v1/health` (index healthy without looking at its store),
      the REQ-057 retention purge (hard-deleted the Postgres row, left the content in Qdrant,
      untraceable), `vektra-admin/quotas.py` (dead code → DEBT-028)
- [x] Correct the backlog: `GET /documents/{id}/chunks` **did not exist**; `DELETE` was worse than
      "needs verification" (the deleted document still answered queries)
- [x] Record ADR-0026 **before** writing code, as the operator required
- [x] Protocol grows exactly three things, each with a real caller: `list_chunks()`, `count_chunks()`,
      optional `index_version` on `store()`
- [x] Reindex reads and writes through the registry's active store; derives target-version chunk ids
      (`uuid5(doc_id, "{position}:v{target}")`) so it writes *alongside* the live version instead of
      overwriting it in Qdrant, and remaps parent/child links (FEAT-017) through the same map
- [x] Reindex fails loudly when it stores nothing over a non-empty namespace; migration `0007` adds
      `reindex_jobs.chunks_reindexed` so the work it did is observable through the API
- [x] `QdrantVectorStoreProvider.retrieve()` scopes by `index_version`; `delete()` returns a measured
      count instead of the hardcoded `0` that let DELETE report `chunks_removed: 0`
- [x] Retention purge removes the chunks through the provider, and **keeps** the document row if that
      fails: dropping the row while the content survives is the one outcome retention must never produce
- [x] Namespace binding on DELETE and the new GET (found in review — see Notes)
- [x] Verified live: reindex `v2` 0 → 12 points with `v1` intact; `/stats` 12 / 105 / 562; DELETE
      removes the points and the document drops out of search

### 2. Close the test gap that let it survive (same PR)
- [x] Diagnose it: the only Qdrant test **mocked the whole `qdrant_client` module**, and the
      integration suite ran against **pgvector only**. A mocked client cannot notice an empty table.
- [x] `.github/workflows/integration.yml` becomes a **matrix over both providers**, so the same
      assertions run against each
- [x] `tests/integration/test_chunk_lifecycle.py`: the lifecycle through the public API alone, with no
      knowledge of the store behind it — including that **a deleted document is not retrievable**
- [x] Aggregator job carrying the required check name, so branch protection survives the matrix (see Notes)
- [x] Both providers green in CI on the PR

### 3. BUG-024 — the stack would not start with hybrid search on (branch `fix/bug-024-sparse-provider-alias`, PR #101)
- [x] Found by rebuilding the dev stack to verify BUG-023: startup died on
      `Provider 'fastembed-bm25' not registered ... Available: ['default']`
- [x] Confirm it is **not** ours: `main.py` on `develop` registers the sparse provider under one alias
      while `check_provider_registration` looks it up by name. Introduced by `b49ce23`.
- [x] Fix by registering both aliases, as the vector store already does
- [x] Diagnose why nothing caught it: **`vektra-app/tests/` was run by nothing**, neither `make test`
      nor CI — the module that wires every provider together had an unexecuted test suite. And the
      existing check tests hand `check_provider_registration` a mock registry that already contains the
      name, asserting the check against a fiction.
- [x] `test_provider_registration.py` wires the **real** registration step to the **real** check;
      verified to fail with the exact production error when the fix is reverted
- [x] App suite wired into `make test` and a new `test-app` CI job, gated in the aggregator
- [x] Verified live: with the real `.env` (sparse on), the stack starts, registers the provider, healthy

## Acceptance Criteria

- [x] The decision is recorded and justified before any code changes (ADR-0026)
- [x] A reindex in Qdrant mode measurably rewrites the collection — proven by counting points, not by
      reading a status field
- [x] A reindex that stores nothing fails loudly instead of reporting `completed`
- [x] `/stats` returns real counts in Qdrant mode
- [x] `GET /documents/{id}/chunks` returns the chunks in Qdrant mode
- [x] `DELETE` removes the points, and the deleted document is no longer retrievable
- [x] **An integration test that runs against `VEKTRA_VECTOR_STORE_PROVIDER=qdrant`** — the criterion the
      operator singled out, because the absence of exactly this test is what let the bug class live for
      months. Delivered as a CI matrix over both providers, not a single extra test.
- [x] `QdrantVectorStoreProvider.retrieve()` scopes by `index_version`

## Notes

**Found in review (Gemini), fixed in the same PR.** `DELETE /documents/{id}` never enforced namespace
binding: it took the namespace from the query string and never checked it against the key. Verified
against `develop` — the gap is **pre-existing**. But it was survivable only because the delete was a
no-op against the active store. Once the delete actually removes the chunks, the same request becomes a
real **cross-namespace deletion**: a dormant hole armed by its own fix. Closed here, with five tests
that fail against the pre-fix code. (The reviewer proposed `ERR-AUTH-001`, the missing-token code; the
correct one is `ERR-AUTH-003`. Bot suggestions were verified against the code, not applied blind.)

**Renaming a CI job silently breaks branch protection.** The required status check is matched by name.
Turning the integration job into a matrix renamed it, so the required check never reported and PR #102
sat `BLOCKED` with everything green. Fixed with an aggregator job carrying the required name, mirroring
the existing `ci-gate` pattern — adding a third provider tomorrow needs no repo-settings change.

**Test hermeticity is not what DEBT-025 thinks it is.** Its diagnosis was right (litellm's import-time
`load_dotenv()` leaks the repo `.env` into `os.environ`), but the fix reaches three test packages out of
eight, and a scrub alone is insufficient: sub-configs read the `.env` **file** relative to the working
directory, bypassing both the settings object and the scrubbed environment. The new app test was loading
a cross-encoder because of a developer's `VEKTRA_RERANK_ENABLED=true` — i.e. it ran a different code path
locally than in CI. Filed as DEBT-029.

**Spawned**: DEBT-028 (chunk quota counts `document_chunks` with raw SQL; dead code, latent),
DEBT-029 (test isolation incomplete), DEBT-030 (two `vektra-app` test files still run by nothing).
