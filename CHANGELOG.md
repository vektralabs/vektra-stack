# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

<!--
Convention (Keep a Changelog 1.1.0):
- Add new entries under "[Unreleased]" using sections: Added, Changed,
  Deprecated, Removed, Fixed, Security.
- At release time: rename "[Unreleased]" to "[X.Y.Z] - YYYY-MM-DD" AND
  add a fresh empty "[Unreleased]" block above it. The file must always
  have an "[Unreleased]" section at the top, even if empty.
- Releases are listed newest-first below "[Unreleased]".
- See CONTRIBUTING.md > Changelog for the full process.
-->

## [Unreleased]

### Added

- **ci**: a structural guard that fails when a test suite is executed by nothing (DEBT-031). Nothing checked this. `make test` and `ci-unit.yml` enumerate the packages in two independent hand-maintained lists, and the DEBT-029 guard verifies only that a package is *isolated*, never that anyone *runs* it — so a `vektra-foo/tests/` added tomorrow was silently unexecuted and no test went red. That is the exact shape of the hole BUG-024 fell through, still open one level up. `vektra-shared/tests/test_suite_execution_coverage.py` reads the runners instead of trusting them: it parses the `test:` recipe and every `pytest` invocation in every workflow, and asserts that each test file would actually be *collected* by one — which means honouring the `-m` filter and not just the paths, because a file can sit in a directory both runners name and still be run by neither, every unit run passing `-m "not integration"`. The unit path and the integration path are therefore checked independently. Workflow YAML is parsed, never grepped: `integration.yml` carries a commented-out `pytest` line, and a guard that counted it as coverage would certify a suite nobody runs. It executes in a `test-structure` job carrying **no path filter**, deliberately — every other unit job is gated on `dorny/paths-filter`, so a PR that only edits the Makefile, only edits a workflow, or adds a new package matches no filter and skips them all, and those are precisely the three changes this guard exists to catch. A guard a path filter can skip is not a guard, so the DEBT-029 guard (until now gated on `vektra-shared/**`) runs there too.
- **ci**: publish versioned container images to GHCR on tag push (INFRA-007). `v*` tags trigger `.github/workflows/publish.yml`, which builds and pushes `ghcr.io/vektralabs/vektra:{version}` and `:{version}-ocr` (GHA build cache, OCI version/revision labels, built-in `GITHUB_TOKEN`, no new secrets). Deployment docs and a new `deploy/docker-compose.image.yml.example` overlay cover the resulting `docker compose pull && docker compose up -d` flow as an alternative to building from source. No `latest` tag. The manual tagging flow is unchanged.
- **index**: `GET /api/v1/documents/{document_id}/chunks` lists a document's stored chunks (text, position, parent link, metadata) at the active index version, read from the active vector store. Any valid scope.
- **index**: `reindex_jobs.chunks_reindexed` (migration `0007`) records how many chunks a reindex actually re-embedded and wrote, exposed on `GET /api/v1/reindex/{job_id}/status`. A job that walked every document and stored nothing used to be indistinguishable from a real one.
- **index**: `DELETE /api/v1/index-versions/{version}` (admin) reclaims a superseded index version, completing the reindex lifecycle REQ-064 always specified ("cleanup of old version afterwards") and nothing implemented (DEBT-032). A reindex writes a second copy of every chunk under the target version and leaves both in the store; until now nothing removed the loser, so **every reindex doubled a namespace's storage permanently** and the only way back was a hand-written delete against the store. The gap was live only because BUG-023 was fixed: while reindex wrote nothing, there was never an old version to clean up. `VectorStoreProvider` grows `delete_index_version(namespace, index_version)`, implemented by both Qdrant and pgvector, and `scripts/reindex.sh --cleanup OLD_VERSION [NAMESPACE]` drives it. **The store refuses to delete the version it is currently serving** (409, `ERR-INDEX-001`), because the failure mode that matters here is not "an old version survives" but "the live index is emptied": the hand-written delete this replaces has no idea which version is live and is run at precisely the moment the operator is least sure. The refusal lives in the providers, not the endpoint, since a store is the only component that knows which version it reads — so it holds for every caller, and it is why the cleanup is a second run of the script rather than a flag on the first: the switch sits between them. The call is idempotent (a second one returns `chunks_removed: 0`) and namespace-bound keys cannot clean up another namespace's version (403). Verified against the live Qdrant stack: reindexing a namespace to v2 doubled it (12 → 24 points), the switch left search byte-identical, the cleanup reclaimed exactly the 12 old points, deleting the *active* version was refused, and the six other namespaces — all sitting at version 1 — lost nothing.

### Fixed

- **config**: `VEKTRA_LLM_PROVIDER=""` was accepted and the stack **booted and served with an empty LLM provider**, deferring the misconfiguration to the first query — which is the one thing the ARCH-057 startup sequence exists to prevent (REQ-011, NFR-009). The field is required, but typed as a bare `str`, and `""` is a perfectly valid `str`, so pydantic never raised and step 1 never fired. It is now `min_length=1`, so an empty value fails at startup with the same structured, remediable error an absent one already produced. Found by running `tests/test_startup.py` for the first time (DEBT-031): the test had asserted this exact behaviour since it was written, believing `docker run -e VAR=` *unset* the variable when it in fact sets it to the empty string — so it had been asserting against a value the product happily accepted. Nobody found out, because nothing ever ran it. This is the third time a suite in this repo turned out to be broken the moment someone executed it. The same test also caught **BUG-025** (filed, not fixed here): the structured error is emitted and then `raise SystemExit(1)` travels out of the ASGI lifespan into uvicorn, which logs a `Traceback` after it — so a typo'd env var yields the good message *and* a stack dump, where NFR-009 asks for the former instead of the latter. Fixing that means validating before `uvicorn.run()` rather than inside the lifespan, i.e. touching the boot path BUG-024 broke, so it is tracked on its own; the assertion is `xfail(strict=True)`, which turns the suite red the moment the fix lands and the marker is left behind.
- **startup**: a failed ARCH-057 startup step printed a raw Python `Traceback` next to the structured `[STARTUP ERROR]`, where NFR-009 asks for the remediable message *instead of* the stack dump (BUG-025, REQ-011). The 11-step sequence ran inside the ASGI lifespan, so an aborting step's `raise SystemExit(1)` travelled out into uvicorn, which logged the exception before "Application startup failed." — and it applied to every step, not just config validation. The sequence now lives in a loop-agnostic `_run_startup()` that raises `StartupValidationError` (never `SystemExit`), and the container serves through a new `main()` entrypoint (`python -m vektra_app.main`, wired in `docker/entrypoint.sh`) that runs the validation in the very event loop that will serve, then starts uvicorn with `lifespan="off"`. A failed step logs its `[STARTUP ERROR]` and exits non-zero with no ASGI and no traceback; sharing one loop keeps the async resources built during validation (DB engine, HTTP clients) bound to the loop that serves, and `config.get_loop_factory()` preserves uvloop (the CLI default that uvicorn 0.36 stopped exposing through `setup_event_loop()`). The lifespan keeps validating and raising `SystemExit` for the in-process/TestClient path, so `test_missing_llm_provider_aborts_startup` is untouched; the `xfail(strict=True)` on `test_no_raw_traceback_on_startup_failure` is removed. Proven on the container for a config-validation failure and a database_connectivity failure (structured error, exit 1, no traceback) and a valid boot (all 11 steps logged, `startup_complete`, `Uvicorn running`). The required check names (`CI gate`, `Integration tests + NFR gates`) are unchanged.
- **tests**: five suites that no runner executed now run, all five found by the guard above the moment it was pointed at `develop` (DEBT-031). Four are `integration`-marked — `vektra-admin/tests/test_integration.py`, `vektra-index/tests/test_integration.py`, `vektra-index/tests/test_benchmark.py`, `vektra-ingest/tests/test_integration.py` — so every unit run excluded them by `-m "not integration"`, while the only job that ran `-m integration` named `vektra-app/tests/` alone: DEBT-030 wired up vektra-app by hand and left the identical hole open in three neighbouring packages. The fifth is `tests/test_startup.py`, the ARCH-057 startup-validation suite, whose own docstring states it requires the stack "started by integration.yml" — it did not run there, because that workflow names `tests/integration/` and nothing named `tests/`. It was watching the precise surface BUG-024 broke, and it was watching nothing. The `app-integration` job becomes `package-integration` and targets `vektra-*/tests -m integration`, a glob on purpose: the hand-written package list is the thing that has now failed twice, and a package added tomorrow is picked up with no edit. `tests/test_startup.py` joins the job that already brings the stack up. Unlike vektra-app's suites in DEBT-030 these four had **not** rotted — 52 integration tests pass — but that was luck, not a property of the arrangement. The five suites under `tests/` were also missing the `integration` marker they require, which is what made them indistinguishable from unit tests `make test` had merely forgotten; `tests/nfr/test_performance.py` remains deliberately unrun (it needs a live LLM that GitHub Actions does not have) and is the single justified entry in the guard's `EXPECTED_UNRUN`, where a reviewer can see it. The required check names (`CI gate`, `Integration tests + NFR gates`) are unchanged.
- **tests**: the local `.env` reached **every** test package, including the three that carried the DEBT-025 scrub fixture (DEBT-029). litellm calls `dotenv.load_dotenv()` at import and finds the repo `.env` by walking up from its own module inside `.venv/`, so the developer's configuration landed in `os.environ` — and the scrub could not stop it, because the scrub runs *before* the test body while the import that re-injects the file happens *inside* it. Measured: after `import litellm` in a test, a config left at its default resolved to the value in the developer's `.env` (`qdrant`) rather than the code's default (`pgvector`) in all four packages checked, `vektra-core` and `vektra-shared` included. Tests therefore ran a different code path locally than in CI, which is the one thing a test must not do. `vektra_shared.testing` now sets `LITELLM_MODE` before litellm can be imported, so litellm skips the dotenv load entirely; all eight test packages import the single shared fixture, and a structural test fails if a package is added without it. Two empty `tests/__init__.py` files (analytics, learn) that made pytest derive the same module name for two conftests were removed.
- **tests**: `vektra-app`'s two Docker-backed test files (the ARCH-057 startup sequence and the REQ-011/NFR-009 error contract) now run in CI, in an `app-integration` job gated by the integration aggregator (DEBT-030). They were run by nothing — and, unrun, they had rotted: both built the test container's URL with `str(make_url(...))`, which masks the password as `***`, so Alembic authenticated with a literal `***` and every test errored at setup. They had never worked. Fixed; 8 tests pass. `vektra-app` also never registered the `integration` marker it relies on.
- **index**: in Qdrant mode, every code path that read chunk text from Postgres worked on an empty table and reported success (BUG-023, [ADR-0026](.s2s/decisions/ADR-0026-document-chunks-pgvector-internal.md)). `document_chunks` is written only by the pgvector provider, so with `VEKTRA_VECTOR_STORE_PROVIDER=qdrant` — the configuration every real deployment runs — it holds nothing, and the paths that queried it did not fail, they lied: `reindex` re-embedded no chunks and reported `completed` (the root cause of the no-op previously attributed to BUG-021), `GET /stats` reported `chunk_count: 0` for populated namespaces, `POST /documents/{id}/chunks` wrote to the *inactive* store, `GET /health` reported the index healthy while the store backing it was unreachable, and — the serious one — `DELETE /documents/{id}` returned `200 {"chunks_removed": 0}`, soft-deleted the Postgres row, left the Qdrant points in place, and **the deleted document went on answering queries**. Retention (REQ-057) had the same defect and was worse: it hard-deleted the document row and relied on the `document_chunks` CASCADE, leaving the content in Qdrant, still searchable and no longer traceable to any document. All of these now go through the `VectorStoreProvider` Protocol, which grows `list_chunks()` and `count_chunks()` and an optional `index_version` on `store()`; `document_chunks` is now formally private to the pgvector provider. A reindex over a non-empty namespace that stores nothing fails loudly instead of reporting `completed`. Verified against the live Qdrant stack: reindex rewrites the collection (measured 0 → 12 points on the target version, source version preserved), stats match the real point counts, and a deleted document is no longer retrievable.
- **index**: `QdrantVectorStoreProvider.retrieve()` now scopes by `index_version` like search and like the pgvector provider, so a chunk from a stale index version is no longer reachable through parent expansion (FEAT-017) when search itself could never return it.
- **index**: `QdrantVectorStoreProvider.delete()` returns the number of points it removed instead of a hardcoded `0`; that placeholder is what let `DELETE` report `chunks_removed: 0` while it was in fact removing nothing.
- **ci**: the integration suite now runs as a matrix against **both** vector stores (`pgvector` and `qdrant`). It ran against pgvector only, which is why this entire class of bug survived for months: the sole Qdrant test mocked the `qdrant_client` module wholesale, and a mocked client cannot notice that a table is empty. `tests/integration/test_chunk_lifecycle.py` asserts the chunk lifecycle through the public API on whichever store is active, including that a deleted document is not retrievable.

### Security

- **index**: `DELETE /api/v1/documents/{document_id}` now enforces namespace binding (H5). It took the namespace straight from the query string and never checked it against the key's binding, so an admin key bound to one namespace could name another and have it honoured. The gap predates this release, but it was survivable only while the delete was a no-op against the active vector store: now that the delete actually removes the chunks (BUG-023, above), the same request would be a real cross-namespace deletion. The new `GET /documents/{id}/chunks` had the mirror-image defect — `namespace` defaulted to the literal `"default"`, so a namespace-bound key that omitted the parameter was rejected on its own documents — and both endpoints now resolve the namespace from the key binding, as `/stats` and `/search` already did. The 403 returns the `ERR-AUTH-003` envelope instead of a bare string. Found in review.
- **index**: `check_provider_registration` (ARCH-057 step 5) is now actually called at startup, and checks the `embedding`/`default` alias instead of a hardcoded `sentence-transformers` name. The function was dead code — nothing called it outside its own unit test — which is why TEI mode (`VEKTRA_EMBEDDING_PROVIDER=tei`) started fine even though the check could never see its provider. Found in review.
- **docker**: the Qdrant healthcheck (`docker-compose.yml`, `qdrant` service) shelled out to `curl`, which the `qdrant/qdrant` image does not ship, so the container was permanently reported `unhealthy` in every deployment using the `qdrant` profile. Replaced with a `bash`-only check against `/healthz` over `/dev/tcp`, verified against the pinned `v1.17.0` image (empirically confirmed to flip the live container from `unhealthy` to `healthy`). The TEI healthcheck was also audited and does ship `curl`, so it is unchanged.
- **docs**: corrected two claims that were false for the default Qdrant deployment. The ingest API reference documented the sync response `status` as `"indexed"` (the code returns `new`, `exists` or `alias`; `indexed` is an *async job* status) and omitted Markdown from the supported formats, although `MarkdownExtractor` has been registered for `text/markdown` all along. The agent instructions (`.claude/CLAUDE.md`) claimed chunk text lives in the Postgres `document_chunks` table: that table is written only by the pgvector provider, so in Qdrant mode it is empty and Qdrant's payload is the sole source of chunk text — which is also the root cause of the `reindex` no-op tracked under BUG-021.
- **docker**: `CMD_TARGET=migrate docker run <image>` (the env-var invocation form) now actually runs the one-shot migration instead of silently booting a full server. The Dockerfile's `CMD ["server"]` was always passed as `$1` to the entrypoint, which the precedence chain favors over the `CMD_TARGET` env var, so the documented env-var form never worked — this hung a production deployment script waiting on a server that was never going to migrate. Removed the Dockerfile `CMD` (the entrypoint already defaults to `server` with no argument); `docker run <image> migrate` continues to work unchanged.
- **tests** (index): `test_pgvector_unit.py`'s parent-chunk tests now exercise the real `DocumentChunkOrm` class via a `session.add` intercept instead of mocking the ORM class and asserting on its constructor call args, per `.coderabbit.yaml`'s instruction to never mock SQLAlchemy ORM classes.
- **app**: the stack failed to start whenever `VEKTRA_SPARSE_EMBEDDING_PROVIDER` was set, so hybrid search could not be enabled at all (BUG-024). Startup validation looks the sparse provider up by its configured name (`fastembed-bm25`), but the app registered it only under `"default"` — the vector store registers both aliases, the sparse provider registered one — so the check that was wired up in the previous release killed every stack with sparse embeddings on. The alias is now registered. The reason nothing caught it is worth recording: `vektra-app/tests/` was run by **nothing**, neither `make test` nor CI, although it covers the module that wires every provider together, and the existing check tests hand `check_provider_registration` a mock registry that already contains the name, asserting the check against a fiction. The app suite now runs in `make test` and in a new `test-app` CI job, and `test_provider_registration.py` wires the real registration step to the real check.

## [0.6.0] - 2026-07-13

RAG quality release: parent chunk expansion, retrieval-filter rescue, per-namespace citations, remote TEI providers.

### Added

- **rag**: remote embedding and reranking via HuggingFace Text Embeddings Inference (FEAT-024). `VEKTRA_EMBEDDING_PROVIDER=tei` embeds through a TEI instance (`VEKTRA_TEI_URL`/`VEKTRA_TEI_API_KEY`, native `/embed` API) instead of in-process sentence-transformers, enabling shared host inference and long-window models (bge-m3: 8192 tokens vs MiniLM's 128, which silently truncates 500-token chunks today). `VEKTRA_RERANK_PROVIDER=tei` reranks through TEI `/rerank` (`VEKTRA_RERANK_TEI_URL`/`VEKTRA_RERANK_TEI_API_KEY`). The Qdrant collection is now sized from the active embedding provider's dimensions instead of a hardcoded 384 (latent bug for any non-384 model), with a clear startup error on dimension mismatch against an existing collection. The `cohere` rerank option now actually passes its API key (`VEKTRA_RERANK_API_KEY`); it was dead as wired.
- **rag**: optional retrieval-filter rescue for multi-part questions (TECH-007, `VEKTRA_RETRIEVAL_RESCUE_TOP_K` + `VEKTRA_RETRIEVAL_RESCUE_FLOOR`, default off). When `VEKTRA_MIN_RELEVANCE_SCORE` empties the candidate set, keep the top-N chunks above an absolute floor instead of refusing: the cross-encoder scores each partial-answer chunk of a comparative/multi-part question below the threshold (it answers only one part), so on the eval corpus 9/10 multi-chunk questions died at the filter with `before=5 after=0` despite 90% raw retrieval hit. With the rescue, borderline sets reach the LLM, which arbitrates via strict grounding. The `retrieval_filter` trace step now records a `rescued` count.
- **rag**: optional per-namespace inline source citations (FEAT-021, `citations_enabled` in the namespace config JSONB via `PATCH /api/v1/admin/namespaces/{id}/config`, default off, advanced pipeline only). When enabled, the system prompt instructs the LLM to add inline `[n]` markers matching the `<source id>` elements, the context template carries a `title` attribute ("filename, p.N"), and each returned source includes a `title` field; the learn widget renders the markers as superscripts with a tooltip. Default-off renders byte-identical prompts; `prompt_version` changes anyway because the template files changed (trace comparability note).
- **rag**: optional parent chunk expansion in the advanced query pipeline (FEAT-017, `VEKTRA_PARENT_EXPANSION_ENABLED`, default off). With `VEKTRA_CHUNKING_STRATEGY=dual`, retrieved child chunks are replaced with their parent chunk's text after the retrieval filter and before token budgeting; children of the same parent collapse into the highest-scored one. Parent-child linkage is now actually persisted (deterministic `uuid5(doc_id, position)` ids, `parent_id` in the Qdrant payload and in the pgvector column), a new `VectorStoreProvider.retrieve()` fetches chunks by id, and the trace records `children_expanded`/`siblings_merged`/`parents_fetched` in a `parent_expansion` step.

### Changed

- **index**: vector search now excludes parent-level chunks (`chunk_level=parent`) in both providers; parents are context material fetched by id during expansion, not retrieval targets. Only affects documents ingested with `dual` chunking, whose parents previously polluted search results.
- **index**: pgvector `store()` honors caller-provided UUID chunk ids (deterministic ids from ingest) instead of always generating random ones; non-UUID ids still fall back to random.

### Fixed

- **docker**: the `INSTALL_UNSTRUCTURED=true` image variant builds again (BUG-022). torchvision (transitive via unstructured-inference) resolved from PyPI with CUDA-built wheels while torch is pinned to the CPU index, crashing the build with `operator torchvision::nms does not exist`. It is now declared in the `ocr` extra and pinned to the pytorch-cpu index; a new path-filtered CI workflow builds the OCR variant so it cannot silently regress.
- **index**: `/api/v1/search` now resolves the embedding, sparse-embedding, and vector-store providers from the ProviderRegistry instead of hardcoding pgvector and reading a never-populated `app.state` attribute (BUG-021). In Qdrant deployments the endpoint returned zero results (it searched the empty `document_chunks` table) and hybrid mode always fell back to dense; the RAG pipeline (`/api/v1/query`) was unaffected. Found by the Sprint 3 baseline `make eval-retrieval` run.
- **tests**: unit tests are now hermetic against the developer's local `.env` (DEBT-025). Importing litellm during pytest collection loads `.env` into the process environment, which made 4 default-assertion tests in `vektra-shared` fail on dev machines while CI stayed green. An autouse fixture in `vektra-shared/tests/conftest.py` scrubs ambient `VEKTRA_*` variables; production settings loading is unchanged. The fixture is replicated in `vektra-core` and `vektra-ingest` (FEAT-017 surfaced the same leak there: ambient `VEKTRA_CHUNKING_STRATEGY=dual` broke 12 chunker and pipeline-default tests).

## [0.5.1] - 2026-07-12

Security hardening: DEBT-024 dependency sweep, workflow permissions, admin login hardening.

### Security

- **deps**: re-lock of all transitive dependencies flagged by Dependabot (DEBT-024, 61 of 62 open alerts). Highlights: litellm 1.83.10 → 1.91.2 (critical: authentication bypass via Host header injection), pyjwt 2.11 → 2.13 (public-key JWK accepted as HMAC secret), starlette 0.52 → 1.3.1 with fastapi 0.129 → 0.139 (StaticFiles SSRF/NTLM credential theft), transformers 5.2 → 5.3 (RCE), plus urllib3, cryptography, Mako, python-multipart, soupsieve, aiohttp (×21 advisories), pypdf (×9), idna, onnx, pydantic-settings, python-dotenv, Pygments. The remaining open alert (torch, low) has no patched release yet.
- **ci**: minimal `permissions:` blocks added to all GitHub workflows (`contents: read`), closing the 12 `actions/missing-workflow-permissions` code-scanning alerts.
- **admin**: login hardening: `POST /admin/login` validates the token against the urlsafe-base64 charset before key-store lookup (closes the `py/cookie-injection` code-scanning alert) and strips accidental surrounding whitespace from pasted tokens.
- **deps-dev**: esbuild 0.25 → 0.28 in the widget build toolchain (Dependabot security update); development-dependencies group refresh; GitHub Actions bumps (checkout v7, dorny/paths-filter v4).

### Changed

- **metrics**: `/metrics` is now served by prometheus-fastapi-instrumentator instead of starlette-prometheus (unmaintained, incompatible with starlette >= 1.0: every request failed with `AttributeError` on included routers). Metric names change accordingly (e.g. `http_request_duration_seconds` replaces the `starlette_*` families); no in-repo dashboards or tests referenced the old names. ARCH-014 updated.

## [0.5.0] - 2026-04-27

Widget production-ready + instructor configuration.

### Added

- **vektra-admin**: `PATCH /api/v1/admin/namespaces/{id}/config` endpoint for instructor configuration. Admin-scoped, whitelisted (v0.5.0 accepts `grounding_mode` and `show_sources`), partial updates, null removes a key. Backs the Moodle block form that lets teachers toggle strict/hybrid RAG and source-citation visibility per course without admin intervention.
- **vektra-learn / widget**: configurable source citation visibility (FEAT-014). Resolution chain: `data-show-sources` attribute (client override) > `namespaces.config.show_sources` (per-course) > `VEKTRA_LEARN_SHOW_SOURCES` env var > default `true`. The learn query response (both JSON and the SSE `sources` event) now carries a `show_sources` flag the widget uses to decide whether to render the citations section. The API always returns the full sources list so analytics and QueryTrace keep complete data.
- **vektra-admin**: `GET /api/v1/admin/namespaces/{id}/config` symmetric to the PATCH. Returns `{namespace_id, config: <stored JSONB>, resolved: <effective after env defaults>}`. The `resolved` block is what the upstream plugin form (e.g. Moodle block edit) needs to render the "Use default" / "Override" toggle without re-implementing the fallback chain.
- **vektra-learn**: `GET /api/v1/learn/conversations/{id}/turns` JWT-scoped endpoint so the widget can restore a conversation after a page reload. Returns decrypted question/answer + created_at; admin-only metadata is not exposed. 403 on namespace mismatch, 404 on missing.
- **vektra-core**: `document_name` field on every source citation, joined from `source_documents.filename` so the widget renders `[1] lecture-07.pdf` instead of chunk UUIDs. Propagates through both `SimpleQueryPipeline` and `AdvancedQueryPipeline`, JSON and SSE paths. Soft-deleted source documents (REQ-057) keep their citation with an `(archived)` suffix so answers stay traceable.
- **vektra-learn**: content-access audit entry (`learn_conversation_turns_read`) written on every successful turns fetch via the shared `vektra_shared.audit` interface (NFR-007).
- **widget**: white-label `data-*` attributes — `data-title`, `data-primary-color`, `data-icon` (emoji or URL), `data-welcome-message`, `data-powered-by`, `data-powered-by-text`, `data-powered-by-url`. All rendered via `textContent` / safe color whitelist to avoid XSS.
- **widget**: tab-scoped conversation persistence via `sessionStorage` keyed by `course_id` (24h cutoff); history replay on load via the new turns endpoint; explicit "New chat" button in the header.
- **errors**: new codes `ERR-ADMIN-005/006/007` (namespace config) and `ERR-LEARN-005/006` (conversation turns).
- **docs**: consolidated [Widget integration guide](docs/guides/widget-integration.md) covering the LMS-agnostic embed flow (server-side JWT issuance, all `data-*` attributes, conversation persistence, token refresh, source citation visibility, error states, and end-to-end examples for PHP and Python backends). Linked from `docs/README.md` under Guides.

### Changed

- **User-facing branding**: README, top-level docs h1s, and external surfaces now use **Vektra RAG** as the product name. Coordinated with [vektralabs/vektra-moodle#14](https://github.com/vektralabs/vektra-moodle/pull/14) which renames its README h1 to "Vektra RAG for Moodle". Repo name (`vektra-stack`), Python package names (`vektra_*`), Docker images, container names, CLI commands, and internal code identifiers are unchanged.
- **widget footer label**: default "Powered by" link text changed from "Vektra" to "VektraLabs" — clearer maintainer-credit pattern (the link points to vektralabs.github.io, the org landing page) and avoids the residual short form. Integrators using the default branding will see the new label after upgrading the bundle; custom `data-powered-by-text` overrides are unaffected.
- **widget styles**: button and accent colors now use `var(--vektra-primary, …)` so `data-primary-color` takes effect without rebuilding the bundle. Hover states use `filter: brightness()` so custom colors still feel interactive.
- **widget primary-color scope (DEBT-018)**: the `--vektra-primary` override is now scoped to widget roots (`.vektra-chat-btn, .vektra-chat-panel`) instead of `:root`, so it no longer clobbers any unrelated `--vektra-primary` declarations on the host page. Repeated widget initialization reuses a single `<style id="vektra-primary-override">` node instead of accumulating duplicates (multi-instance / hot-reload safety).

### Fixed

- **audit log fallback (DEBT-020, NFR-007)**: sensitive admin, learn, and ingest endpoints now always write an audit row, even if the request-id middleware doesn't populate `request.state.request_id`. Each component declares a private `_resolve_request_id` helper that synthesizes a `uuid4()` fallback and emits a structlog warning so middleware misconfiguration is observable. Applied to `POST /api-keys`, `DELETE /api-keys/{id}`, `PATCH /admin/namespaces/{id}/config`, `GET /admin/conversations/{id}/turns`, `GET /api/v1/learn/conversations/{id}/turns`, and the vektra-ingest async + direct audit writers (used by `POST /api/v1/ingest`, batch ingest, deletion). Previous code skipped the audit silently when `request_id` was missing.
- **test fakes**: renamed `self_inner` to `self` in nested `_FakeResult` / `_FakeSession` helpers inside `test_fetch_document_names_marks_archived` (style consistency with PEP 8; no behaviour change).

### Security

- **deps**: bumped `litellm` from `>=1.40,<1.82.7` to `>=1.83.10,<1.83.11` — closes 2 critical CVEs (authentication bypass via OIDC userinfo cache key collision) and several high-severity findings (authenticated command execution via MCP stdio test endpoints, SSTI in `/prompts/test`).
- **deps-dev**: bumped `pytest` from `>=8.0` to `>=9.0.3` (full test suite green on the new major version).
- **deps-dev**: bumped widget `esbuild` from `^0.24` to `^0.25`.

## [0.4.0] - 2026-04-11

QueryTrace observability, eval harness, widget polish, and prompt hardening.

### Added

- **vektra-core**: configurable prompt grounding mode (FEAT-020, DEBT-016). `VEKTRA_PROMPT_GROUNDING_MODE=strict` (default) keeps the LLM tied to retrieved context + history; `hybrid` allows confident fallback to model knowledge. Foundation for the per-namespace override added in v0.5.0.
- **vektra-core**: multilingual reranker upgrade (BUG-016, DEBT-010). Default reranker switched to `BAAI/bge-reranker-v2-m3` with a lower score threshold so non-English queries are no longer over-filtered.
- **vektra-core**: `QueryTrace` persistence and an admin `GET /api/v1/admin/conversations/{id}/turns` endpoint (BUG-013, DEBT-011). Reranker scores, eval-mode metadata, and streaming model name are now captured in traces; gaps in the streaming path closed.
- **vektra-learn**: SSE streaming in the learn query endpoint (FEAT-010). The `done` event now carries `conversation_id` so the widget keeps multi-turn continuity across SSE responses (BUG-018).
- **widget**: Markdown rendering for assistant messages (FEAT-007), with code-block, list, and inline formatting; raw HTML stays sanitized.
- **widget**: token auto-refresh on 401 (FEAT-009) via either an `onTokenExpired` callback or a `data-token-refresh-url` endpoint.
- **widget**: API connectivity check and visible error feedback (FEAT-006) when the backend is unreachable.
- **eval**: retrieval and end-to-end evaluation harness (TECH-002), plus a curated 55-question bilingual EN/IT evaluation dataset.
- **config**: `VEKTRA_LLM_CONTEXT_WINDOW` propagation through `VektraSettings`, `VEKTRA_LLM_API_KEY` wired to litellm, and `VEKTRA_LLM_EXTRA_BODY` support for vLLM thinking-mode flags.

### Fixed

- **vektra-core**: warn (not crash) when `VEKTRA_LLM_CONTEXT_WINDOW` is unset for a model litellm does not know, falling back to 4096 (BUG-017).
- **vektra-core**: conversation row now created before the first turn is persisted (BUG-014).
- **vektra-core**: reranker scores propagated to `SearchResult` so observability and analytics see the post-rerank ranking (BUG-015).
- **vektra-core**: register the conversation store in `ProviderRegistry` so dependent endpoints can resolve it.
- **vektra-core**: distinguish "safeguard blocked" from "no relevant context" in the response so callers can tell apart a refusal from an empty result.
- **vektra-core**: system prompt iteratively hardened to stop the LLM from leaking RAG internals (chunk IDs, scores, role names) in any language.
- **vektra-learn**: audit logging on the conversation turns endpoint (NFR-007) was missing in the initial v0.4.0 release-review build.
- **config**: `grounding_mode` validator added to `VektraSettings` so invalid values fail fast at startup.
- **widget**: streaming token newlines and code-block formatting; centralized input-disabled state across sending and connection events; `<p>` no longer wraps inside `<code>` blocks.
- **build**: removed redundant `uv sync` when `INSTALL_UNSTRUCTURED=true`; eliminated all `uv pip install` invocations outside the lockfile to harden the supply chain.
- **CI**: latency measurement skipped on PR runs (post-merge only); perf-measurement queries reduced from 100 to 20 to keep workflow time bounded.

## [0.3.0] - 2026-03-21

E-learning vertical refinements, widget UX improvements, and Phase 2 stabilization.

### Added

- **vektra-learn**: optional enrollment mode (`VEKTRA_LEARN_REQUIRE_ENROLLMENT`) for LMS integrations where enrollment sync is not yet configured
- **vektra-learn**: collapsible source citations in chatbot widget with accessibility (aria-controls, focus-visible)
- **vektra-learn**: i18n support for source fallback labels (en/it)
- **vektra-core**: `VEKTRA_LLM_API_BASE` config for OpenAI-compatible providers (vLLM, etc.)
- **vektra-ingest**: sparse embedding count validation (fail-fast, consistent with dense path)
- **docker-compose**: parameterized host ports for optional services (Qdrant, TEI)

### Fixed

- **vektra-learn**: auto-generate conversation_id for multi-turn continuity (BUG-010)
- **vektra-learn**: validate course_id claim in JWT before namespace resolution
- **vektra-learn**: show fallback message when no relevant context found
- **vektra-learn**: destructure onNoRelevantContext callback in widget API client
- **vektra-ingest**: generate sparse embeddings for hybrid search (BUG-011)
- **vektra-admin**: guard uninitialized registry in health check (prevents 500 during startup)
- **vektra-core**: pass registry to qdrant_check startup validation step (ARCH-057)
- **widget**: fix snippet truncation and ellipsis detection
- **widget**: improve source citation readability

### Changed

- Default configuration updated to Combo D (RAG tuning report winning config): advanced pipeline, paraphrase-multilingual-MiniLM-L12-v2 embeddings, 2048 response token reserve, 60s fallback timeout
- Streaming responses now emit QueryTrace via SSE (DEBT-002)
- post_retrieval safeguard boundary now called in both execute and stream paths (DEBT-003)

### Security

- API key verification uses TTLCache instead of plaintext LRU cache (DEBT-008)

## [0.2.0] - 2026-03-15

Phase 2: advanced RAG pipeline, multi-tenant isolation, hybrid search, analytics, admin UI, and e-learning vertical.

### Added

- **vektra-shared**: SparseEmbeddingProvider and SparseVector protocols, WebhookEventEmitter, Phase 2 config fields (rewrite, rerank, webhook, learn)
- **vektra-core**: AdvancedQueryPipeline with query rewriting, cross-encoder reranking, and hybrid search routing; Presidio-based PII safeguard with content modification; persistent conversations with pgcrypto encryption; feedback and citation-feedback APIs; streaming QueryTrace
- **vektra-ingest**: OCR support via Unstructured extractor; dual-strategy chunking (text + table preservation); batch ingestion and deletion; Markdown extraction; document version tracking; phase-aware job lifecycle
- **vektra-index**: Qdrant vector store provider; BM25 sparse embeddings via fastembed; hybrid search (dense + sparse); reindex API with atomic version switching
- **vektra-admin**: PostgreSQL RLS enforcement for namespace isolation; scope checking (admin/ingest/query/monitor); rate-limiting middleware; TTL cache for key verification; HTMX + Jinja2 admin dashboard (ADR-0024) with health, keys, namespaces, and audit views
- **vektra-analytics**: QueryTrace storage and retrieval; per-namespace metrics aggregation; retention cleanup
- **vektra-learn**: LMS-agnostic API for enrollment, token generation, and course-scoped queries; JWT-based course isolation; chatbot widget as backend-served JS bundle (ADR-0025)
- **Infrastructure**: TEI and Qdrant Docker Compose profiles; reindex and batch-ingest operator scripts; 11-step startup validation sequence (ARCH-057)
- **Database**: 4 new migrations (Phase 2 tables, RLS policies, hybrid search columns, learn tables)
- **ADRs**: ADR-0022 (SQLAlchemy async), ADR-0023 (query rewriting), ADR-0024 (admin UI), ADR-0025 (learn widget)

### Fixed

- Safeguard pre_query/post_retrieval/pre_response boundaries fully wired
- Conversation persistence made best-effort (non-blocking)
- Qdrant collection creation race condition handled
- Namespace binding enforced on stats and reindex endpoints
- Audit pagination cursor with microsecond precision
- Non-object JSON guard in RLS namespace resolution

### Changed

- Embedding model default: all-MiniLM-L6-v2 to paraphrase-multilingual-MiniLM-L12-v2
- Query pipeline default: simple to advanced
- pydantic capped to <3 across all components

## [0.1.0] - 2026-02-27

Phase 1 MVP: from `git clone` to first RAG query in under 30 minutes.

### Added

- **vektra-shared**: 9 Protocol interfaces, configuration (40 env vars), API key auth with argon2id, ProviderRegistry, startup validation
- **vektra-core**: RAG query pipeline (embed, search, filter, prompt, LLM), multi-turn conversations, streaming SSE, LLM graceful degradation (primary + fallback), token budget allocation
- **vektra-ingest**: PDF extraction via pdfplumber, fixed-size chunking, async jobs for large files (>10 MB), duplicate detection (SHA-256), content type validation via python-magic
- **vektra-index**: pgvector-backed semantic search, embedding generation (all-MiniLM-L6-v2), namespace isolation, relevance threshold filtering, overlap deduplication
- **vektra-admin**: API key CRUD, hierarchical health endpoints (/health, /health/{component}, /health/memory), audit logging, Prometheus metrics on /metrics
- **Docker Compose stack**: single-command deployment (vektra + postgres), optional Ollama profile for local LLM
- **CI pipeline**: GitHub Actions (lint, unit tests x5 components, integration tests, NFR hard gates, shell script validation, auto-labeler)
- **Makefile**: 10 operator targets (test, lint, health, demo, etc.)
- **Operator scripts**: health.sh, create-key.sh, ingest.sh, query.sh, demo.sh
- **Documentation**: Diataxis structure (getting started, API reference, configuration, error codes, architecture overview, contributor guide, n8n workflow)
- **13 error codes**: structured error envelope (ERR-AUTH, ERR-INGEST, ERR-QUERY, ERR-CONFIG) with categories and remediation
- **23 ADRs**: architectural decisions from modular monolith strategy to query pipeline design
