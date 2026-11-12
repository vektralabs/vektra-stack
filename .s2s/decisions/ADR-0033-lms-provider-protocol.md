# ADR-0033: LMS provider Protocol: the assessment vertical stays authoritative, the LMS client becomes an adapter

**Status**: accepted (2026-11-11). The product owner decided OD-1 to OD-6 on 2026-11-11 (section 6). Nothing stays open at the decision level. OD-1 carries a validity condition, re-checked at the 2a-3 cut.
**Date**: 2026-11-11
**Amends**: [ADR-0027](ADR-0027-assessment-discussion-as-a-top-level-vektra-assess.md) (ARCH-065: who owns the WS client and the setup check; ARCH-073: the names that are costly to reverse)
**Deviates from**: [ADR-0009](ADR-0009-namespace-isolation-rls.md). The deviation is recorded in D9.
**Extends**: ARCH-039 with an 11th Protocol and an `lms` slot in `ProviderRegistry`
**Context**: the 2026-11-08 external review of the LMS boundary, and the structural guard `vektra-shared/tests/test_lms_boundary.py` with its baseline `lms_boundary_baseline.toml`
**Plan**: [20261111-185453-assess-lms-separation](../plans/20261111-185453-assess-lms-separation.md)
**Measured at**: `origin/develop` `f95c148b` (2026-11-11). PRs #199 and #200 were still open at that time. #199 touches three baseline files (`assess_wiring.py`, `assess_worker.py`, `netguard.py`) and migration 0009, and its diff does not change any file's mention count. Every number below comes with its predicate in section 7, so it can be re-run instead of trusted.

## 1. Context

### The question that was not asked

ADR-0027 asked "sub-package or top-level package?" (ARCH-073) and answered it well. It never asked "platform or adapter?". `.s2s/CONTEXT.md` has said since its first version that the e-learning verticals are LMS-agnostic and that the Moodle specifics live in adapter repositories. `vektra-learn/src` has kept that rule for its whole history: no commit has ever added the string. `vektra-assess` did not keep it, because the rule was written only in prose and nothing measured it. Since 2026-11-08 the guard measures it.

The decisive fact is the **call direction**. An LMS calls in to `vektra-learn`. `vektra-assess` calls **out** to one LMS product's web services, with a per-site breaker and a two-version model matrix. That call direction is what makes the platform stop being generalist, more than where any file sits.

### What the measurement found

Counts use the guard's own matcher, which also catches `MoodleWsClient`, `moodle_origin` and `VEKTRA_ASSESS_MOODLE_*`:

| Where | Files | Mentions | Baseline entry |
|---|---:|---:|---|
| `vektra-assess/src/vektra_assess/moodle/` (the WS client, 6,049 lines) | 35 | 247 | the `vektra-assess` directory entry |
| `vektra-assess/src` outside `moodle/` | 59 | 294 | the `vektra-assess` directory entry |
| `vektra-assess/ui/src` (the iframe bundle) | 2 | 10 | the `vektra-assess` directory entry |
| platform files (`vektra-shared`, `vektra-app`, `vektra-admin`) | 7 | 31 | 7 file entries |
| `migrations/versions/` | 18 | 37 | 18 file entries |

Outside its own sub-package, the client's real surface is small and already almost behind a port:

- **Imports**: 20 import statements in 8 files. Seven are in `vektra-assess` (`context/capture/{capture,readers,selection}.py`, `worker/{start_source,setup_check,setup_items,parser_client}.py`). The eighth is `vektra-app/src/vektra_app/assess_worker.py`, with two lazy imports.
- **Readers**: 18 distinct reader functions, called from 35 call sites. 24 of those sites are in `worker/start_source.py` and 11 in `context/capture/readers.py`.
- **Types**: 13 domain types (`moodle/domain.py`), plus `ActivityRef`, `SiteRef` and `MoodleWsClient`. The read outcome types (`Readable`, `Unreadable`, `Reading`) already live outside the package, in `vektra_assess/readable.py`.
- **Setup check**: 12 symbols of `moodle/setup_check.py` are imported by the worker.
- **Ports that already exist**: `StartSource` (`worker/preflight.py:150`, three methods), `CaptureReaders` (`context/capture/capture.py:90`, eleven methods), and four methods that are duck-typed on `MoodleStartSource` (`graders`, `teacher_facts`, `close_facts`, `forget`).

The leaks into the vertical are mostly **names**, not logic:

- 28 distinct identifiers contain "moodle", for example `MoodleGateFacts`, `moodle_release`, `moodle_origin`, `moodle_reasons` and `moodle_rubric`;
- three DB columns name it: `assess_sites.moodle_site_uuid`, `assess_sites.moodle_origin` and `assess_setup_check_reports.moodle_release`;
- two values inside sealed, hash-anchored snapshots name it: `SnapshotHeader.moodle_release` (`context/snapshot.py:214`) and the `rubric_source` literal `"moodle"` (`:207`);
- seven wire values name it, for example the `rubric_source` value `"moodle"` and the reason code `moodle_rubric_missing`;
- 11 settings name it or its transport.

Two leaks carry **logic**:

- Moodle's question states (`context/qtypes.py:28`);
- Moodle's review-HTML CSS classes, inside the network-less parser sidecar (`parser_sidecar/html_review.py:260,263`). This allowlist is security-relevant under BR-011.

Section 7 has the full list.

### Why the names are cheaper to change now than ADR-0027 assumed

ARCH-073 lists names as the costly things: the route baked into the deployed plugin, table names under encrypted and hash-anchored data, settings and error codes. That holds once something has been deployed. Today nothing has been:

- v0.8.0 was cut **without** `vektra-assess` (#180), so no published Vektra release contains an `assess_*` revision;
- `vektra-moodle-discuss` has **no tag**. Its `version.php` says `0.1.0`, and its plugin plans P2 and P3 are still in progress and pending (`INDEX-ASSESS.md`). The plugin calls `/api/v1/assess/tokens`, `/state` and `/transcripts/*` (`classes/local/student_endpoint.php:45,48`, `transcript_proxy.php:99,135`), and none of the names that carry "moodle";
- the pilot go/no-go is NO-GO (plan 13), so no pilot database holds exam data.

So the window in which renames cost an edit, rather than an alias plus a deprecation, is open now. It closes at the first release that ships `vektra-assess`, or at the first plugin tag. The product owner confirmed on 2026-11-11 that the window is open (OD-1), and the confirmation is re-checked at the 2a-3 cut.

## 2. Decision

### D1. Platform or adapter? Authority stays in Vektra, transport moves to an adapter

Authority and transport are separate concerns. ADR-0027 chose a Vektra-authoritative design for reasons that still hold:

- REQ-067: nothing the LMS renders is authoritative;
- BR-006(d): cohort-wide override reads are invisible inside the student's own session;
- REQ-070 / REQ-086: client-less, sub-minute scheduling.

Those reasons require Vektra to **decide**, to **re-resolve** every id it is given, and to **schedule**. They do not require Vektra to contain the client.

| Stays in `vektra-assess` (the vertical, the authority) | Moves to the Moodle provider (the adapter, the transport) |
|---|---|
| session state machine, START and IN-SESSION gates (`gates.py`) | `MoodleWsClient`, REST encoding, token redaction (`moodle/client.py`) |
| attempt selection (BR-012), gate-fact derivation (`worker/start_source.py`, renamed `LmsStartSource`) | per-release strict models `models/v4_3`, `models/v5_2`, generator `scripts/assess_gen_ws_models.py` |
| the decision to re-resolve ids (REQ-095), the resolution cache | the lookup that resolves them (`moodle/readers/resolve.py`) |
| disclosure policy and snapshot projection (ADR-0032 layer 1 policy, layer 2) | the reads, normalised into neutral types (`moodle/readers/*`) |
| capability matrix storage, freshness and fail-closed rules (`capabilities.py`) | the capability probes (`moodle/probes.py`) |
| setup-check runner, report lifecycle, generic items | the service-account items of REQ-079 (13 of the 15 built-in items) |
| parser sidecar, as a generic, profile-driven HTML extractor | the review-HTML extraction profile (CSS classes) and its oracle suite |
| clock, outage, sweeper, LLM runner, transcript, delivery, audit, purge | breaker, rate limiter, per-site WS token secrets (`moodle/{breaker,ratelimit,tokens}.py`) |

A deployment with no LMS registers no provider, and the assessment vertical is unavailable: `VEKTRA_ASSESS_ENABLED=true` without a provider fails closed at worker startup and in the setup check. A deployment on another LMS writes one provider. This is the "configuration over fork" constraint in `.s2s/CONTEXT.md`.

### D2. The `LmsProvider` Protocol

**Location.** The Protocol goes in a new module, `vektra_shared/lms/` (`protocol.py`, `types.py`), and is re-exported from `vektra_shared.protocols` as the 11th Protocol. It is registered under the `lms` category of `ProviderRegistry`. The reason for this location is ARCH-039 and ADR-0005: every Protocol lives in the shared kernel, so an implementation depends only on `vektra_shared` and never on a component. An adapter package that imported `vektra_assess` would make an external repository depend on a vertical's internals. The vocabulary in `types.py` (course, activity, attempt, rubric) is the LMS domain's vocabulary, not Vektra's internals, and it names no product. The rejected alternative is O3.

**Discovery.** `vektra_app` does not name the provider. It reads `VEKTRA_ASSESS_LMS_PROVIDER` (for example `moodle`) and loads that name from the entry-point group `vektra.lms_providers` (`importlib.metadata`). This is the "extensible to entry_points plugin discovery" that ARCH-039 anticipated. It then registers the instance under `("lms", name)`. In v1 a deployment has one provider. `ProviderRegistry` can already hold several by name, and a per-site provider column is deferred until a second LMS exists.

**The Protocol.** Every read returns `Reading[T]`, and no exception escapes it (the discipline of `vektra_assess/readable.py`, which moves into `vektra_shared.lms.types`). All ids are the LMS's own integers, as today. The limitation is recorded under Consequences.

```python
@runtime_checkable
class LmsProvider(Protocol):
    @property
    def name(self) -> str: ...                     # registry key, metrics and log label

    @property
    def setup_items(self) -> tuple[str, ...]: ...  # the service-account items it returns (D3)

    @property
    def required_secrets(self) -> tuple[str, ...]: ...  # its secrets; also REQUIRED_SECRETS on the entry point (added in 2a-2, static in 2a-4)

    @property
    def call_deadline_s(self) -> float: ...          # one call's deadline, budgets sequential-call jobs (added in 2a-4)

    # lifecycle
    def startup_problems(self, development: bool) -> tuple[StartupProblem, ...]: ...   # was assess_worker.py:229; the resolved flag (an explicit VEKTRA_ENV=development), never the raw env (amended in 2a-1)
    async def aclose(self) -> None: ...

    # site and service account (REQ-079, ARCH-070)
    async def site_info(self, site: LmsSite) -> Reading[LmsSiteInfo]: ...
    async def service_account_check(self, site: LmsSite, info: Reading[LmsSiteInfo],
                                    manifest: Mapping[str, Any] | None) -> tuple[SetupItem, ...]: ...
    async def probe_capabilities(self, site: LmsSite, manifest: Mapping[str, Any] | None,
                                 flags: Sequence[str]) -> tuple[CapabilityResult, ...]: ...

    # resolution (REQ-095): Vektra decides to re-resolve, the provider looks up
    async def resolve_activity(self, site: LmsSite, *, user_id: int, activity_id: int,
                               kind: ActivityKind, course_hint: int | None) -> Reading[ActivityRef]: ...
    async def resolve_in_course(self, site: LmsSite, *, activity_id: int, kind: ActivityKind,
                                course_id: int) -> Reading[ActivityRef]: ...

    # people
    async def enrolment(self, site: LmsSite, course_id: int, user_id: int) -> Reading[Enrolment]: ...
    async def course_staff(self, site: LmsSite, course_id: int,
                           role: StaffRole) -> Reading[tuple[StaffMember, ...]]: ...

    # quiz
    async def quiz_settings(self, site: LmsSite, course_id: int, quiz_id: int) -> Reading[QuizSettings]: ...
    async def review_options(self, site: LmsSite, quiz_id: int, user_id: int) -> Reading[ReviewOptions]: ...
    async def attempts(self, site: LmsSite, quiz_id: int, user_id: int) -> Reading[tuple[AttemptSummary, ...]]: ...
    async def attempt_review(self, site: LmsSite, attempt_id: int) -> Reading[AttemptReview]: ...

    # assignment
    async def assignment_settings(self, site: LmsSite, course_id: int, assignment_id: int) -> Reading[AssignmentSettings]: ...
    async def assignment_dates(self, site: LmsSite, assignment_id: int, user_id: int) -> Reading[AssignmentDates]: ...
    async def submission(self, site: LmsSite, assignment_id: int, user_id: int) -> Reading[Submission]: ...
    async def submission_file(self, site: LmsSite, assignment_id: int, ref: SubmissionFileRef,
                              owner_user_id: int, submission_id: int) -> Reading[bytes]: ...
    async def assignment_release(self, site: LmsSite, course_id: int, assignment_id: int,
                                 user_id: int) -> Reading[AssignmentRelease]: ...

    # grading
    async def grade_item(self, site: LmsSite, course_id: int, user_id: int, activity_id: int) -> Reading[GradeItem]: ...
    async def grading_definition(self, site: LmsSite, activity_id: int) -> Reading[GradingDefinition]: ...
    async def rubric_fillings(self, site: LmsSite, definition_id: int) -> Reading[tuple[RubricFilling, ...]]: ...
```

The Protocol has 23 members. Each maps onto measured code: the 18 reader functions called outside `moodle/`, plus `site_info`, the service-account items (and their declared names), the probes and the startup check. The six methods named in the review are covered as follows:

| Review method | Protocol members |
|---|---|
| `enrolments` | `enrolment`, `course_staff` |
| `attempt` | `attempts`, `attempt_review`, `review_options` |
| `grade` | `grade_item`, `assignment_release`, `grading_definition`, `rubric_fillings` |
| `submission_files` | `submission`, `submission_file` |
| `overrides` | inside `quiz_settings.overrides` and `assignment_dates` |
| `service_account_check` | `site_info`, `service_account_check`, `probe_capabilities` |

The measurement shows two needs the review did not list: **resolution** (`resolve_activity`, `resolve_in_course`) and **startup validation** (`startup_problems`). The **origin** is not a Protocol method. It is a property of the site row (`lms_origin`), read by Vektra for CSP `frame-ancestors`, and only defaulted from `base_url` by Vektra itself (`ui/origins.py:79`).

**Neutral types** (`vektra_shared.lms.types`). The neutral types are the frozen dataclasses of `moodle/domain.py`, renamed and normalised:

| Neutral type | Was | What changes |
|---|---|---|
| `LmsSite` | `SiteRef` | unchanged fields |
| `LmsSiteInfo` | `SiteInfo` | only `release` and `branch` are exposed. The function list and the admin flags stay inside the provider, which uses them for its own items. |
| `QuizSettings` | `QuizCloseInputs` | `grademethod` becomes the enum `GradeMethod`; `overduehandling` becomes the enum `OverdueMode`, values `AUTOSUBMIT`, `GRACE`, `ABANDON` |
| `AttemptState` | same | the provider maps its own values onto the enum |
| `ReviewItem` | `ReviewSlot` | `qtype` becomes `question_kind`, a Vektra-defined `QuestionKind`; `state` becomes `QuestionState`, values `FINAL_MARKED`, `FINAL_NO_MARK`, `PENDING`, `UNKNOWN`. The Moodle state list at `qtypes.py:28-44` moves to the provider, and `UNKNOWN` fails closed as today. `html` becomes `ReviewHtml(html, profile)`, see D4. |
| `AssignmentSettings`, `AssignmentDates`, `Submission`, `AssignmentRelease`, `GradeItem`, `GradingDefinition` | `Assign*`, `SubmissionStatus`, `GradeReleaseState`, `GradingView` | renamed. `SubmissionFileRef.fileurl` becomes the opaque `locator`. |
| `StaffMember` + `StaffRole` | `GraderView` + three readers | `StaffRole` has the values `GRADER`, `EDITOR` and `OVERRIDE_MANAGER` |
| `ShapeEvidence` | same | the field `wsfunction` becomes `call` |
| `CapabilityResult`, `SetupItem` | `FlagResult`, `ItemResult` | unchanged |

`ActivityKind` stays `Literal["quiz", "assign"]`. That is vertical vocabulary, and renaming `"assign"` would touch CHECK constraints and sealed literals for no boundary gain.

Three invariants that the conformance kit tests (D10):

1. **The Reading discipline.** An exception, an unknown shape, a timeout or an open breaker all come back as `Unreadable`. The vertical also wraps every call and maps an escaped exception to `Unreadable(UNKNOWN_SHAPE, "provider_error")`, so a faulty provider fails closed rather than crashing a pre-flight.
2. **Type-level minimisation (ADR-0032 layer 1).** No neutral type has a field for a solution, a right answer, hidden feedback or an unreleased grade value. A provider cannot hand the vertical what the vertical has no slot for. The one free-text channel is `ReviewHtml`, and D4 covers it.
3. **Re-resolution.** `resolve_activity` for an activity outside the user's courses returns `Unreadable` with `detail_code="activity_not_in_user_courses"`. It never returns the hinted course.

### D3. The setup check splits into a generic runner and a provider part

| Generic, `vektra_assess.setup` (new; moved out of `moodle/setup_check.py`) | Provider |
|---|---|
| `ItemRegistry`, `Outcome`, `ok` / `missing` / `unverifiable`, `run_items`, `SetupCheckStale`, `_code` (renamed `reason_code`) | `service_account_check` returns the 13 service-account items: `token_works`, `required_reads_{quiz,assign,grade,grading,enrol,files}`, `interactive_login_disabled`, `not_site_admin`, `not_ingestion_account`, `denied_function_probe`, `no_grade_write`, `ws_tls_ok` (renamed `lms_tls_ok`) |
| report lifecycle (`run_setup_check`'s database part: claim, `observe_release`, persist the matrix, audit of a rejected token, keep 30) | `probe_capabilities` returns one result per flag of `capabilities.FLAGS`. A missing flag is recorded `not_probed`, which fails closed. |
| generic items: `ui_not_same_origin`, `ui_origin_distinct`, `parser_sidecar_running`, `smtp_probe`, `db_log_statement_none`, `privacy_config`, and the new `lms_provider_registered` | `site_info` supplies the release and tells the runner whether the token was rejected |

The provider declares its item names up front (`provider.setup_items: tuple[str, ...]`). The runner validates them against the existing `[a-z][a-z0-9_]{1,48}` rule, and a declared item that the provider does not return is recorded `MISSING/not_returned`, which is the same fail-closed rule as `not_registered` today. `capabilities.FLAGS` stays in Vektra because the disclosure policy consumes the flags. Their names are already neutral.

### D4. Review HTML: the sidecar stays generic, the profile belongs to the provider

`parser_sidecar/html_review.py` keeps `qtext` and `ablock` and drops 15 classes, `rightanswer` and `feedback` among them. That is Moodle's markup, and it is also the BR-011 line for quiz review HTML. The sidecar's `review_item` op gains a declarative `HtmlExtractionProfile` parameter: `keep: {block: css_class}`, `drop_classes`, `profile_version`. The provider supplies the profile with each `ReviewHtml`. The sidecar keeps the parsing, the isolation and the escaping, and it contains no class names.

BR-011 for LMS-rendered HTML therefore becomes a **joint** guarantee, and this ADR says so rather than hiding it:

- Vektra guarantees the projection, the typed `ContextBundle` and the canary sinks (exact request bytes, logs, traces, audit);
- the provider guarantees that its profile removes what its LMS hides, with its own per-type oracle (`tests/test_review_block_extraction.py` moves with it);
- the conformance kit runs Vektra's canary sinks over the provider's recorded reviews. A provider that does not pass that run is not registrable for production (D10).

### D5. Names: what is renamed, what is kept, and the migration path

The product owner decided **OD-1 = yes** on 2026-11-11: no assess data has to survive the separation, so the "OD-1 = yes" column applies. The right-hand column is kept as the fallback if the validity condition of section 6 fails at the 2a-3 cut, and D10 explains what that does to the ratchet.

| Name | Where | Decision | Cost if OD-1 = yes | Cost if OD-1 = no |
|---|---|---|---|---|
| `/api/v1/assess` route prefix | `api/router.py:32`, the plugin | **keep**: already neutral | none | none |
| `ERR-ASSESS-*` codes | `vektra_shared/errors.py` | **keep the codes**: none names a product. Reword 10 messages and remediations (`vektra_assess/errors.py:12-382`, for example "Reload the page in Moodle" becomes "Reload the course page") and the comment at `vektra_shared/errors.py:175`. | text only, plus `docs/reference/error-codes.md` | same |
| audit events `assess_ws_token_rejected`, `assess_loginas_rejected` | audit, NFR report | **keep**: no product name, referenced by REQ-085 and `scripts/assess_nfr_report.py` | none | none |
| `assess_sites.moodle_origin`, its CHECK `ck_assess_sites_moodle_origin` | 0033, `ui/origins.py`, `assess_wiring.py` | **rename** to `lms_origin` | in the assess schema base (D7) | rename revision plus compat; see D10 |
| `assess_sites.moodle_site_uuid` | 0010 and 15 later revisions' grant lists | **rename** to `lms_site_uuid` | in the schema base | as above |
| `assess_setup_check_reports.moodle_release` | 0016, `api/setup_check.py:146,163` | **rename** to `lms_release`, also in the GET report JSON | schema base plus one field | as above, plus a JSON alias |
| `SnapshotHeader.moodle_release` | sealed, hash-anchored (`snapshot.py:214`) | **rename** to `lms_release` | in place, `schema_version` stays 1 | `schema_version` 2 for new snapshots, the v1 reader kept until the longest `retention_days` has passed |
| `rubric_source` value `"moodle"` | wire (teacher config PUT and GET), sealed `FrozenFlags` (`snapshot.py:207`), `teacher/schemas.py:28`, `validation.py:40,99,485` | **rename** to `"lms"` | in place; P3 has not started, so no plugin sends it | input alias `"moodle"` normalised on write, plus the v1 snapshot reader as above |
| reason codes `moodle_rubric_missing`, `moodle_facts_unreadable`, `moodle_origin_invalid`, `moodle_origin_not_https`, `ui_origin_equals_moodle_origin` | API refusals, setup items, startup | **rename** with `moodle` replaced by `lms` | in place | dual-emit for one minor release |
| JSON keys `moodle_reasons` (`assess_gate_inputs.inputs`, 7-day TTL), `moodle_rubric` (teacher facts) | worker to API | **rename** | in place | dual-read for 7 days |
| operator notify kind `moodle_unreachable` | outbox rows, alert e-mails (`notify.py:83`, `start_guard.py:110-155`) | **rename** to `lms_unreachable` | in place, plus the alert docs | dual-match in alert rules |
| `MoodleGateFacts`, `MoodleStartSource`, `MoodleCaptureReaders` and 25 other identifiers | Python only | **rename** | code only | code only |
| page config `moodleOrigin` | `ui/src/main.ts:8`, `bridge.ts:50-66`, rendered by `ui/router.py` | **rename** to `parentOrigin` | code only (same release) | code only |
| `VEKTRA_ASSESS_MOODLE_{CA_FILE,ALLOW_HTTP,ALLOWED_CIDRS}`, `VEKTRA_ASSESS_WS_*` (6), `VEKTRA_ASSESS_BREAKER_{THRESHOLD,COOLDOWN_S}` | `config.py:236-295` | **move** to the provider's own settings class. The prefix is OD-4. | docs plus compose files | aliases for one minor release |
| `cmid`, `loginas`, `realuser_id` (wire); `cmid` (14 mapped tables in `db/models.py`, 9 route paths, the token claim) | plugin contract, 586 occurrences in 44 files | **keep for v1** (OD-2): not product names, the guard does not see them, and the plugin already sends them. The neutral Protocol uses `activity_id` and maps at `LmsStartSource`. | none | none |
| secret file `assess_ws_tokens` | the provider's secret | **move** with the provider, name owned by it | none | none |

**Not costly after all, and why.** ADR-0027 was right that names under encrypted, hash-anchored data are costly, but the cost is a property of the **data**, not of the name. AEAD binds row values, not column names: the `teacher-config` AAD is `[site_id, cmid, field]` (`crypto/envelope.py:20`, `crypto/rotation.py:155`), so a column rename does not touch any ciphertext. Only the JSON keys inside sealed snapshots enter a content hash (`context/snapshot.py:266-278`). They can be renamed in place only while no retained snapshot exists. That is OD-1 again.

### D6. Where the provider lives in each phase

The `vektra-assess` directory entry clears only when **nothing under `vektra-assess/src` or `vektra-assess/ui/src` names an LMS** (`test_lms_boundary.py`, the directory-entry rule). So "the implementation still in-tree behind the interface" cannot mean "still in `vektra_assess/moodle/`". This ADR resolves the point as follows:

| Option for the provider's location during 2a | Verdict |
|---|---|
| stays in `vektra-assess/src/vektra_assess/moodle/` | the directory entry cannot be removed, so 2a cannot finish. Allowed only **during** 2a, between PR 2a-2 and PR 2a-4. |
| a new workspace member `vektra-lms-moodle/` in this repository | it is inside the guard's perimeter and would need a **new** baseline entry. Rejected. |
| **`adapters/vektra-lms-moodle/`**: its own `pyproject.toml`, **not** a uv workspace member, installed in development through a dependency group and into the image only by an opt-in build argument (the `INSTALL_UNSTRUCTURED` precedent) | **chosen for the end of 2a.** It is outside "what the platform ships" both by the guard's definition and in fact: the default image does not contain it. |
| directly in its final repository | that is 2b. Doing it in 2a ties a cross-repository move to the interface change, and the pilot would be without a provider in between. |

Two guard additions make the `adapters/` choice honest rather than a perimeter trick, and they are tasks of the plan:

1. no workspace member may depend on a `vektra-lms-*` distribution;
2. `test_image_content_boundary.py` asserts that the default image contains no `vektra_lms_*` package and no file from `adapters/`.

In phase 2b, `adapters/vektra-lms-moodle/` moves to its home repository (OD-5) and is published as a wheel, and the opt-in image variant installs it by pinned version and hash (OD-6).

### D7. Migrations: a separate assess branch, gated by the feature switch

**Today.** Revisions 0001 to 0008 are the platform's. Revisions 0009 to 0044 (36 files) are all assess, contiguous after 0008, and `docker/entrypoint.sh:21,33` runs `alembic upgrade head` on every start, so a deployment that never enables assess still gets its tables, roles and functions. Two assess revisions touch platform tables: 0009 rewrites `ck_api_keys_scopes` on `api_keys`, and 0020 creates a policy on `query_traces`. 18 of the 36 files name Moodle, mostly as SQL identifiers (`moodle_site_uuid` in the repeated column-grant lists). History is immutable, so those files never stop naming it while they exist in the perimeter.

**Decision.** The assess schema becomes its own Alembic branch (`branch_labels=("assess",)`, `depends_on="0008"`), in a version location owned by the package: `vektra-assess/src/vektra_assess/migrations/versions/`, added to `version_locations` in `alembic.ini`. The entrypoint always runs `alembic upgrade platform@head`, where `platform` is a branch label added to revision 0001. That is a metadata-only edit, because `alembic_version` stores only ids. It also runs `alembic upgrade assess@head` only when `VEKTRA_ASSESS_ENABLED=true`. The privilege requirement (CREATEROLE, the `SET ROLE vektra_assess_definer` membership) then exists only for deployments that enable assess. The review measured that it kills the container today, for a disabled feature.

**How the 36 revisions get there** depends on OD-1:

- **OD-1 = yes (decided 2026-11-11): squash.** The 36 revisions are replaced by one assess base revision that creates the head schema with the neutral names from D5 and fixes the 0009 error message so that it names all three roles. Equivalence is proven, not asserted: the old chain and the squash are run on two empty databases, and the normalised `pg_dump --schema-only`, the effective privileges (the grant matrix's existing two-way drift test) and the definer functions must be identical except for the renamed identifiers. The only database known to hold assess data is the local F1 stack's Postgres (the `vektra-f1-postgres` container on the developer machine), and it is re-created. Testcontainers are ephemeral. The deploy VM is believed to run v0.8.0, which has no assess. A read-only check confirms that at the cut, and only if it finds `assess_*` tables does it become a recreate request. All 18 migration entries clear at once.
- **OD-1 = no: move and rename.** The 36 files move unchanged by `git mv` (revision ids kept, so an existing database at 0044 still finds them), and a 37th revision renames the columns. The files still name Moodle, so the 18 entries stay and one more appears for the rename revision. The second ratchet can then only close by a squash after every retained database has been migrated: one assess release later at the earliest.

The migration ratchet closes in **2a-3 under OD-1 = yes**, in the same PR series as the code. With OD-1 = no it moves to phase 3 and later, as the review planned.

### D8. The three assess-only modules in `vektra_shared`

| Module | Decision |
|---|---|
| `netguard.py` (5 mentions, one non-test importer: the Moodle client) | **Keep in `vektra_shared`, reworded as a generic egress guard.** An external provider needs a shared egress primitive it can import, and `vektra_learn.api._resolve_and_validate_url` is its second prospective user. Unifying the two stays a DEBT item, owned by the author. The mentions go in 2a-4. |
| `log_safety.ASSESS_LOGGER_PREFIX = "vektra_assess"` (`log_safety.py:31,113,271`) | **Replace the hard-coded string** with `register_strict_logger_prefix(prefix)`, called by `vektra_assess` and by each provider. This must land in **2a-1, before the move**: once the client leaves the `vektra_assess.*` logger namespace, it would silently lose the strict redaction of its logs. That is a dormant gap the move would arm. |
| `testing_recorders.py` (396 lines in the production wheel) | **Move to a test-only distribution, `vektra-testkit`** (dev dependency group), together with the LMS conformance kit (D10), in phase 3. The `ProviderRegistry` test-double guard stays as defence in depth. |
| `JSONRenderer(ensure_ascii=False)` on every deployment | **Announce it** in the CHANGELOG under `Changed`, in phase 3. The behaviour is kept. |

`tokens.py` is shared by `vektra-learn` and `vektra-assess` and stays where it is.

### D9. No RLS on `assess_*`: the deviation from ADR-0009, and the decision

**Fact.** ADR-0009 makes PostgreSQL RLS the platform's isolation mechanism, "not application-level WHERE clauses". No `assess_*` table has a policy: no assess revision contains `ENABLE ROW LEVEL SECURITY`. Isolation between sites rests on three things:

1. column-level grants that leave the API role no privilege on snapshots, turns or session keys;
2. three `SECURITY DEFINER` functions as the only route to session material;
3. AEAD binding of every ciphertext to its row identity, plus `site_id` predicates in every route, covered by the INT-001 IDOR tests.

It is not a live hole: the routes do check. The gap is that one missing `site_id` predicate in a future route would be a cross-site read with nothing in the database to stop it. ADR-0009's own RLS binds only under `VEKTRA_MULTI_TENANT=true`, so assess is not held to a lower standard than its surroundings. It is, however, the only multi-site component.

**Decision.**

1. **Record and accept the deviation now**, with a structural backstop as a 2a-2 task: a test that parses every SQL text in `vektra_assess.api` and fails if a statement reads a site-scoped `assess_*` table without a `site_id` predicate. The column-grant and definer layers stay the primary control for session material.
2. **Add RLS for the API role in phase 3**, on the site-scoped tables the API reads. Every `assess:*` key is bound to exactly one site, so the API can `SET LOCAL app.assess_site_id` from the key's binding at the start of each request transaction. The policy binds `vektra_assess_api` only, unconditionally, and not behind `VEKTRA_MULTI_TENANT`, because assess is multi-site by design. The worker role stays without RLS: it is multi-site by construction, it re-validates every id from rows, and per-job binding would add a failure mode for no isolation gain. The RLS test asserts that an API connection bound to site A reads 0 rows of site B with the route predicate removed. That is the only test that proves the backstop rather than the predicate.

### D10. Phases and the two ratchets

| Phase | What | Ratchet | Done when |
|---|---|---|---|
| **2a** | Protocol and neutral types; the vertical depends only on the Protocol; neutral names; provider staged in `adapters/`; composition by entry point; the schema base (under OD-1 = yes) | **Ratchet 1**: the 8 non-migration entries (the `vektra-assess` directory and the 7 platform files) | `lms_boundary_baseline.toml` contains no `directory` entry and no file entry outside `migrations/`; `test_lms_boundary.py` is green; no PR of the phase added an entry. **Ratchet 2 closes here too (OD-1 = yes, decided)**: the baseline file is empty after 2a. |
| **2b** | `adapters/vektra-lms-moodle/` moves to its home repository and is published; the opt-in image installs the pinned wheel; the 328 recorded fixtures and `assess-moodle-contract.yml` go with it | none (it is a relocation) | `adapters/` is gone from this repository; the provider's CI runs the conformance kit against a released `vektra-stack` |
| **3** | migrations gated (and, under OD-1 = no, the second ratchet); `testing_recorders` and the conformance kit in `vektra-testkit`; `netguard` unification DEBT; RLS for the API role (D9); JSONRenderer announced; the managed-Postgres note proven on its positive case | **Ratchet 2**: the 18 `migrations/versions/*` entries | the baseline file is **empty** and the guard is green; with `VEKTRA_ASSESS_ENABLED=false`, a clean cluster has 0 `assess_*` tables, 0 `vektra_assess_*` roles and 0 assess functions after the entrypoint ran (counted with `\dt`, `\du`, `\df`) |
| **4** | this ADR | none | accepted, with ADR-0027 annotated |

The baseline only ever shrinks. No PR in any phase may add an entry. Where a step seems to need one, for example a rename revision under OD-1 = no, the step is redesigned or the open decision is reopened, and the entry is not added.

**The conformance kit** is introduced in 2a-1 under `vektra-shared/tests/lms_conformance/` and moves to `vektra-testkit` in phase 3. It is a provider-parametrised pytest suite that covers:

- every Protocol member returns a `Reading`, and no exception escapes;
- an unknown shape, a timeout and an open breaker are `Unreadable`;
- the re-resolution refusal;
- canaries planted in every LMS-side forbidden field of the provider's recorded fixtures are absent from every returned value and from the exact LLM request bytes of a full capture;
- no secret appears in logs at any level.

A provider is registrable for production only with a green conformance run against the platform version it declares.

**Tests and the 328 fixtures.**

- The vertical's suites move to a `FakeLmsProvider`: neutral values in memory, an `Unreadable` scriptable per member, and a call recorder for "never read" assertions. The vertical's tests then carry zero Moodle fixtures.
- The 328 files under `vektra-assess/tests/fixtures/moodle_ws/{4.3,5.2}` (158 + 170) are the provider's contract fixtures. They move with it, unchanged, in a rename-only PR (2a-4b).
- The 11 `test_moodle_*` files and `test_review_block_extraction.py` also move with the provider, and so do the live suites (`test_moodle_live`, `test_moodle_setup_live`, `test_context_live`, `test_idor_live`, `test_req073_live`). The live suites run as the provider's live suites against a `vektra-stack` with the provider installed.
- The fake is a test double. A production `ProviderRegistry` refuses it, and the chaos and e2e workers opt in only with `VEKTRA_ENV=development`.

## 3. Options considered

- **O1. Invert the direction: the LMS pushes the facts to Vektra.** It removes the client from Vektra entirely and needs no Protocol. **Rejected on the requirements, which remain the reasons of ADR-0027:**
  - REQ-067: pushed data is rendered and asserted by the LMS, so it cannot be authoritative;
  - BR-006(d): cohort-wide override reads are invisible in the student's own session, which is the only context a pushing plugin has at Start;
  - REQ-070 / REQ-086: client-less, sub-minute scheduling cannot depend on LMS cron.

  It would also move the gate logic into PHP twice, once per supported release. Authority must stay where the scheduling and re-resolution are.
- **O2. Keep the client in the vertical and document the deviation.** No work. Rejected: the guard's baseline would never empty, a company with another LMS would have to fork, and the platform stays non-generalist for as long as the package exists. The review's non-negotiable points (`separation-handoff.md` §6) rule it out.
- **O3. The Protocol owned by the vertical (`vektra_assess.lms`) instead of `vektra_shared`.** Cleaner hexagonal ownership (the port belongs to its consumer) and no assessment vocabulary in the shared kernel. Rejected: an adapter in another repository would depend on `vektra_assess` internals and on its release cadence, against ADR-0005, and `vektra-learn` could not reuse `enrolment` or `course_staff` later. The vocabulary that moves to `vektra_shared` names LMS concepts, not Vektra internals, and no product.
- **O4. A coarse Protocol: `gate_facts`, `capture() -> Snapshot`, `close_facts`.** Fewest methods, and very close to the existing `StartSource`. Rejected: the provider would own attempt selection (BR-012) and the disclosure projection (ADR-0032 layer 1 policy), so authority would move into the adapter, which is O1 by another route. The capture policy must stay in one place, in Vektra, for every LMS.
- **O5. The provider as a workspace member of this repository.** The simplest move. Rejected: it is inside the guard's perimeter and inside the default image, so the guard would need a new entry, and the platform would still ship an LMS client (D6).
- **O6. Aliases and compatibility readers for every renamed name, permanently.** Safe for data that may exist. Rejected as the default: no deployed data or plugin needs them yet, and a compatibility reader that keeps naming Moodle keeps the directory entry alive. Kept as the fallback path if OD-1 is answered no.
- **O7. Gate each assess revision on `VEKTRA_ASSESS_ENABLED` inside the root chain.** Rejected: `alembic_version` would then record revisions as applied that created nothing, and enabling the feature later would never create the schema.

## 4. Consequences

- (+) The platform is generalist again. The guard's baseline drains to empty, and a second LMS is one provider plus a green conformance run.
- (+) The vertical's suites stop depending on 328 recorded files across two version branches. Moodle's version matrix lives in one adapter, next to the plugin that has the same matrix.
- (+) With assess disabled, a deployment gets no assess schema and needs no new privilege.
- (+) The RLS deviation is written down, has a structural backstop now and a database backstop in phase 3.
- (−) BR-011 for LMS-rendered HTML becomes a joint guarantee, and the conformance kit is a maintained asset (it joins the OQ-027 canary matrix).
- (−) Ids are the LMS's integers (`BIGINT` columns, int claims). An LMS with string ids needs a schema change. This is recorded, not solved.
- (−) The image gains an opt-in variant, so the release process has one more artefact (OD-6).
- (−) At the 2a-3 cut (OD-1), the local F1 stack's Postgres (the `vektra-f1-postgres` container on the developer machine) is re-created, and its test data is lost. This is a destructive step outside the repository and it runs only after an explicit go from the owner. The deploy VM is believed to run v0.8.0, which has no assess. The internal-ops session checks it read-only, and it becomes a recreate request to that session only if `assess_*` tables exist there.
- (−) The rename churn is large. The neutral rename touches about 60 files outside `moodle/`, plus the tests. That is why the plan splits it into PRs of about 100 files.

## 5. Amendment to ADR-0027

- ARCH-065 "VEKTRA owns … the WS client and setup check": Vektra owns the setup-check runner and the generic items; the WS client and the service-account items belong to the LMS provider (D1, D3). Everything else Vektra owns under ARCH-065 is unchanged, authority included.
- ARCH-073 "Names": keep `/api/v1/assess`, `assess_*` and `ERR-ASSESS-*`. The Moodle-named columns, values and settings get neutral names as in D5. "Owned revision files in the single root Alembic chain (0009+)" is replaced by the separate `assess` branch of D7. Contract (c) becomes "only `vektra_assess.worker` imports an `LmsProvider` implementation's composition"; in practice `vektra_assess` imports no provider at all, and the import-linter rule forbids `vektra_lms_*` everywhere in the platform.
- The consequence "(−) Vektra depends on Moodle WS availability" becomes "Vektra depends on the configured LMS provider's availability", with the same fail-closed behaviour.

## 6. Decisions of the product owner (2026-11-11)

Each was put to the product owner as an open decision with a recommended default. The options and their costs stay below as the record. OD-1 was decided explicitly ("they are test data, recreate"). For OD-2 to OD-6, the owner asked the coordinator for a recommendation and took the defaults.

**Validity condition of OD-1.** OD-1 = (a) holds only while both of these are true:

1. no published Vektra release ships `vektra-assess`;
2. `vektra-moodle-discuss` has no tag.

Both are checked at the moment of the 2a-3 cut (plan task 2a-3.0). If either is false, 2a-3 stops and OD-1 is decided again. The "OD-1 = no" paths in section 2 stay as the fallback for that case.

| ID | Decision | Options and their cost | Decided |
|---|---|---|---|
| **OD-1** | Must any assess data survive the separation? | **(a) No**: the local F1 assess database is re-created (the only one known to hold assess data); names change in place; the migrations are squashed; both ratchets close in 2a. Cost: the local test data, and a squash-equivalence proof. **(b) Yes**: aliases, dual reads, a v2 snapshot schema with the v1 reader kept for the longest `retention_days`, a rename revision, and the 36 files moved unchanged. Cost: the directory entry and the migration entries stay until the compatibility code can be deleted, at least one assess release later; and 2a cannot meet "no new entries" without a dated exception for the rename revision. | **(a) No assess data survives.** At the 2a-3 cut, the local F1 assess database is re-created after an explicit go from the owner. The internal-ops session runs a read-only check for `assess_*` tables on the deploy VM, and it is asked to recreate only if the check finds any. The 36 revisions are squashed into the `assess` branch with `lms_*` names, and both ratchets close in 2a. Decided explicitly, 2026-11-11. |
| **OD-2** | Rename the plugin-facing wire vocabulary `cmid`, `loginas`, `realuser_id`? | **Keep**: no cost, the guard does not see them, and the Protocol maps `activity_id` to `cmid` in one place. **Rename to `activity_id` / `impersonated`** with the old names accepted for one plugin release: about 586 occurrences in 44 files, 9 route paths, the token claim (`tokens.py:42`), 4 plugin PHP files and the IDOR suites. | **Keep** `cmid`, `loginas` and `realuser_id`. Decided (default, owner consulted 2026-11-11). |
| **OD-3** | `rubric_source` wire value `"moodle"` (teacher config API, used by P3 when it is written) | **Rename to `"lms"` now**: in place under OD-1(a), and P3 is written against the new value. **Keep**: one Moodle word in a wire value, and the directory entry cannot clear. | **`rubric_source` becomes `"lms"` in place.** Decided (default, owner consulted 2026-11-11). |
| **OD-4** | Prefix of the settings that move to the provider (`VEKTRA_ASSESS_MOODLE_*`, `VEKTRA_ASSESS_WS_*`, `VEKTRA_ASSESS_BREAKER_*`) | **Keep the names**, read by the provider: zero churn for operators and dev environments. **Rename to `VEKTRA_LMS_MOODLE_*`**: the prefix says who reads them; docs and compose files change, with no alias needed because nothing has been released. | **`VEKTRA_LMS_MOODLE_*`, no alias**, at the move (2a-4b). Decided (default, owner consulted 2026-11-11). |
| **OD-5** | Home repository of the provider in 2b | **`vektra-moodle-discuss`** (the review's target): the PHP plugin and the Python provider share the 4.3/5.2 matrix and change together. Its release zip must exclude the Python tree, and its CI gains a Python job. **A new repository `vektra-lms-moodle`**: separate lifecycle and toolchain, per ADR-0002's split criteria, but a third Moodle repository. **`vektra-moodle`**: rejected, because it is `block_vektra`'s repository with its own cadence. | **`vektra-moodle-discuss`**, with the Python provider in a top-level `provider/` directory that is excluded from the plugin zip. Decided (default, owner consulted 2026-11-11). |
| **OD-6** | How the provider reaches a deployment | **(a)** `publish.yml` builds a second image tag (`<version>-lms-moodle`) with the pinned wheel; **(b)** operators build with `--build-arg INSTALL_LMS_PROVIDER=moodle`; **(c)** install at container start: rejected (network at start, no pinning). | **(a) A second image tag** (`<version>-lms-moodle`) with the pinned wheel. Decided (default, owner consulted 2026-11-11). |

## 7. Measurement (predicates and references)

All predicates are run at the repository root on `origin/develop` `f95c148b`. Guard-exact counts come from the guard's `mentions_in` over `tracked_tree` (`vektra-shared/tests/test_lms_boundary.py`). The `rg` predicates exclude the sub-package with `--glob '!**/vektra_assess/moodle/**'`.

**Imports of the client from outside `moodle/`**: `rg -n 'vektra_assess\.moodle' vektra-assess/src vektra-app/src --glob '!**/vektra_assess/moodle/**'` (comments excluded by reading):

- `context/capture/capture.py:68,82`
- `context/capture/readers.py:5,6,7,20`
- `context/capture/selection.py:20,21`
- `worker/start_source.py:52,53,54,55,60`
- `worker/setup_check.py:34,35`
- `worker/setup_items.py:33,43`
- `worker/parser_client.py:28`
- `vektra-app/.../assess_worker.py:323,324`

**Reader calls**: `rg -no '\br\.(read_\w+|fetch_\w+)' vektra-assess/src/vektra_assess/{context,worker}`, plus `resolve_activity` / `resolve_in_course` in `worker/start_source.py:167,214,246`. That gives 35 call sites over 18 functions:

- `read_{enrolment,graders,editors,override_managers}`
- `read_{quiz_close_inputs,user_review_options,user_attempts,attempt_review}`
- `read_{grade_release,assign_settings,assign_release,assign_close_inputs,submission_status,grading_view,rubric_fillings}`
- `fetch_submission_file`, `resolve_activity`, `resolve_in_course`

**Domain types used outside**:

- `AssignReleaseState`, `AssignSettings`, `AttemptReview`, `AttemptState`, `AttemptSummary`
- `GradeReleaseState`, `GradingView`, `QuizCloseInputs`, `RubricFilling`
- `SubmissionFileRef`, `SubmissionStatus`, `UserReviewOptions`, `GraderView`

**Where Moodle concepts leak** (outside `moodle/`):

| Concept | References |
|---|---|
| state machine and gates | `worker/preflight.py:128` (`MoodleGateFacts`), `:150` (`StartSource`); `worker/render.py:49,170,227`; `gates.py:3,97` (docstrings) |
| snapshot | `context/snapshot.py:207,214`; `context/capture/capture.py:68-82,307,485` |
| context builder | `context/qtypes.py:28-44` (question states); `parser_sidecar/html_review.py:260,263` (CSS classes) |
| setup check | `worker/setup_check.py:34-38`, `worker/setup_items.py:33-43`, `worker/parser_client.py:28` |
| API routes | `api/teacher_ops.py:251-287` (`moodle_reasons`), `api/setup_check.py:146,163` (`moodle_release`), `api/teacher_config.py:138` (`moodle_rubric`). No route path names Moodle; 9 routes take `{cmid}`. |
| settings | `config.py:236-295` (11 settings) |
| DB columns | `db/models.py:49,59,669`; `db/grants.py:121`; migrations 0010, 0016, 0033 |
| error codes | none in code names; messages at `vektra_assess/errors.py:12,174,207,288,297,298,342,352,381,382` |
| audit | no event name; `audit.py:15` docstring |
| operator alerts | `worker/start_guard.py:110-155`, `delivery/notify.py:11,83` |
| UI | `ui/origins.py:79-127`, `ui/src/main.ts:8`, `ui/src/bridge.ts:2,4,50-66` |

**`cmid`**: `rg -o cmid vektra-assess/src --glob '!**/vektra_assess/moodle/**'` gives 586 occurrences in 44 files. `rg -l cmid migrations/versions` gives 21 files.

**The 7 platform baseline files, and why each names Moodle:**

| File | Mentions | Why | 2a remedy |
|---|---:|---|---|
| `vektra-shared/src/vektra_shared/auth.py:80` | 1 | comment describing the `assess:*` scopes as "the Moodle discussion plugin" | reword |
| `vektra-shared/src/vektra_shared/errors.py:175` | 1 | comment on `ERR_ASSESS_006` ("Moodle 'login as'") | reword |
| `vektra-shared/src/vektra_shared/netguard.py:3,5,13,95` | 5 | docstring: written for, and exemplified by, the Moodle WS client | reword as a generic egress guard (D8) |
| `vektra-app/src/vektra_app/assess_wiring.py:222,238-260,274` | 10 | reads `assess_sites.moodle_origin` for CSP `frame-ancestors`; operator remediation text | column `lms_origin` (D5, D7), reword |
| `vektra-app/src/vektra_app/assess_worker.py:229-236,319-329,339,352,366,962` | 12 | composes `MoodleWsClient` and `MoodleStartSource`; validates `VEKTRA_ASSESS_MOODLE_ALLOW_HTTP` | entry-point load (D2), `provider.startup_problems()` |
| `vektra-app/src/vektra_app/main.py:694` | 1 | `FrameDenyMiddleware` docstring | reword |
| `vektra-admin/src/vektra_admin/templates/keys.html:19` | 1 | help text of the `assess` scope fieldset | reword |

Five of the seven (9 mentions) are comments or help text. The two composition files in `vektra-app` (22 mentions) are the structural coupling, and the entry-point load removes them.

**Tests**:

- `git ls-files vektra-assess/tests/fixtures/moodle_ws | wc -l` gives 328 (4.3: 158, 5.2: 170);
- 38 test files reference the client, its fixtures or `assess_moodle_support.py`;
- 19 reference `MoodleStartSource` or `MoodleCaptureReaders`.

**Migrations**: `ls migrations/versions` gives 44 revisions, of which 0009 to 0044 (36) are assess. `rg -n 'api_keys' migrations/versions/0009_assess_scopes_roles.py` and `rg -n 'POLICY' migrations/versions/0020_assess_worker_trace_grant.py` show the two that touch platform tables. `rg -n -i 'ENABLE ROW LEVEL' migrations/versions` finds only 0003, which is the platform's.

**Plugin consumption** (read-only, `vektra-moodle-discuss` working tree): no tag under `.git/refs/tags` or in `packed-refs`; `version.php:32` release `0.1.0`; the endpoints it calls are listed in section 1.
