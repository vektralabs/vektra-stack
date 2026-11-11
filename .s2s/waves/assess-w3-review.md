# Wave W3 review: session end to end (assessment discussion bot)

Date: 2026-11-08. Gate run on `develop` at `b6a1515` (merge of PR #168). PRs: #153 and #154 (session
lifecycle, split 8a M1-M2 and 8b M3-M5), #155 (Moodle WS fixtures re-recorded after the dev-site reset),
#157 (discussion UI), #159 and #168 (transcripts, split 10a generation/download/resend and 10b SMTP
delivery). Plugin side (vektra-moodle-discuss, owned by the other session): P2a merged, P2b open as a draft.

## Plans

| Plan | Result | Review |
|------|--------|--------|
| assess-session-lifecycle (47 tasks) | completed, PR #153 (M1-M2) + PR #154 (M3-M5) | `/code-review` on each part, findings fixed; CodeRabbit #153 1 round, #154 3 rounds |
| assess-discussion-ui (29) | completed, PR #157 | `/code-review` findings fixed; CodeRabbit 3 rounds. Item picker (SHOULD) deferred, see below |
| assess-transcript-delivery (26) | completed, PR #159 (10a) + PR #168 (10b) | `/code-review` high on 10b: 10 findings, all fixed with a failing-first test. CodeRabbit #159 5 rounds, #168 5 rounds (4 with findings, the 5th clean) |

## Gate

| Gate item | Result | Evidence |
|-----------|--------|----------|
| Burst test repeated on the integrated stack | **BLOCKED** | No integrated target: API + mint key + DSN + dev Moodle + reference model (OQ-026). The in-process variant passes: `test_session_burst_inprocess.py` (40 Starts, K=10: 10 commits, 30 capacity refusals, 0 rows left). The run of 2026-11-06T20:15:27Z recorded BLOCKED; its result file is no longer committed (`tests/nfr/results/*.json` is gitignored). No NFR-018 latency or NFR-020 K evidence exists yet. |
| Six close reasons plus rollback by row counts | PASS | `tests/chaos/assess/test_lifecycle_chaos.py::test_each_close_reason_by_row_count` and `::test_kill9_mid_preflight_commit_leaves_the_student_eligible`. CI job "Assess chaos" ran green on PR #168 head `79b8188` and runs again on the `develop` merge. |
| IDOR negative tests | PASS (recordings and live) | `vektra-assess/tests/test_idor.py::test_idor_matrix` on the recorded 4.3 and 5.2 responses (every case rejected and audited, 0 cross-student bytes), and `test_idor_live.py::test_idor_matrix_live` on the live 4.3 and 5.2 dev sites: 2/2. The live subset (`-m moodle`) was run after the gate, on 2026-11-08 at `9534fef` (develop `b6a1515` + plan 11a): 86 passed, 0 skipped. That subset includes REQ-073 byte-identity: `test_req073_live.py`, 4/4. The first version of this review marked live IDOR BLOCKED; that was wrong, because the dev sites were up. |
| No token accepted in a query string | PASS | `test_route_credentials.py::test_no_route_accepts_a_query_string_or_cookie_credential` |
| Login-as refused end to end | PASS on each side, joined run **BLOCKED** | Vektra: `test_session_loginas.py` (pre-session and session tokens without `loginas:false` start, read and write nothing; `loginas_rejected` audited). Plugin (4.3 and 5.2 CI): Behat `student_loginas.feature` (2 scenarios: the button is disabled and the Vektra stub receives 0 requests) and PHPUnit `test_loginas_makes_no_call_and_mints_nothing`. A run of the plugin against a real Vektra needs the local F1 stack (https UI origin, `moodle_origin`), which is not set up yet. |
| NFR-016 accessibility (axe) | PASS (automated), manual **BLOCKED** | UI: `test_ui_e2e.py` runs `a11y.e2e.ts`, with 0 WCAG 2.1 A/AA violations in every state, reflow at 320 px and at 200% zoom, and a keyboard-only flow. Transcript: `test_transcript_a11y.py`, with 0 axe violations and 0 Nu errors, plus a broken control document. The manual NVDA/VoiceOver read-through needs a person with a screen reader. |
| The transcript post-render allowlist rejects every listed vector | PASS | `test_transcript_render.py`, plus `test_transcript_inert_browser.py`: 7 payload files opened in Chromium give 0 requests, 0 scripts and 0 executed payloads, with a guard on the file count. The served headers (attachment, `CSP: sandbox`, nosniff, no-store) are checked in `test_transcript_api.py`. |
| Mail sink receives exactly one SENT per recipient | PASS | `test_delivery_handler.py`: 2 h of SMTP down then up gives 1 message per recipient. `assert_sent_rows_are_on_the_sink` runs after every scenario. A crash between the 250 and the SENT commit gives at most 1 duplicate, audited. The relay is an aiosmtpd TLS mail sink with fault toggles. |
| NFR-021 timing | **BLOCKED** | The benchmark exists: `scripts/assess_transcript_timing.py` and `tests/nfr/test_assess_transcript_timing.py`. It records a BLOCKED result, because a compose stack whose relay is a recording TLS mail sink does not exist yet (target VM, plan 13). |
| Plugin Behat green on 4.3 and 5.2 | PASS | vektra-moodle-discuss "Moodle Plugin CI": develop `ed7b429` (P1 + P2a), run 42334015234, Behat 26/26 scenarios (299 steps) on 4.3 and on 5.2, PHPUnit 130/130. P2b draft PR #3 at `ba2dc60`, run 42383208372, Behat 39/39 (458 steps) on both, PHPUnit 130/130. Figures as reported by the plugin session. |

Test totals at the gate on `develop` (this machine): `make lint` clean (ruff, mypy, 21 import contracts kept);
`make test` 3645 passed, 78 skipped; `-m integration` for vektra-assess and vektra-app against Postgres
(testcontainers) 523 passed, 84 skipped (the live-Moodle subset, as in CI). CI on the merge commit `b6a1515`
(integration run 42450710680): pgvector, qdrant, assess stack, assess chaos and assess UI e2e jobs green; the
package integration job was still running when this was written (see this PR's checks).

## "Working at the end"

Not yet, end to end. Every piece exists and is tested on its own side:
- the plugin's button and pre-session screen (P2a, Behat on 4.3/5.2);
- token mint, Start, preflight and commit, turns, close (plan 8);
- the cross-origin UI (plan 9);
- transcript generation, download and SMTP delivery to a mail sink (plan 10).

No run has gone from a student on a real Moodle results page, through a real model, to a mailed transcript. That needs:
- the integrated stack (F1);
- the reference model (OQ-026);
- P2b's transcript pieces (E1-E3, D5), which wait on the plan 10 contracts. Those contracts were sent to the plugin session on 2026-11-08.

## Deliberately open

- **Integrated-stack runs** (burst, login-as joined, NFR-021): all need
  the same environment, which is the target VM / compose stack with dev Moodles, a model and a recording mail
  sink. Plan 13 (hardening-release) owns the NFR campaign. The local F1 stack for the plugin session is still
  queued.
- **OQ-026 / OQ-027**: reference model and eval-suite owner, as in W2. No NFR-018 or NFR-022 number is measured.
- **OQ-029**: product/QA confirmation of the exactly-once reading. The design default is in place (resend with
  the same Message-ID, `delivery_possible_duplicate` audited).
- **REQ-082 amended (2026-11-07)**: the teacher enters the transcript recipients (1-3) in the activity
  configuration, under a mandatory domain allowlist. The service account gets no `moodle/course:useremail`.
  Plan 11 must write the sealed config as one JSON object `{solution, transcript_recipients}` under the
  binding `config`. Plugin P3 owns the form field.
- **`/state` follow-up**: `selected_attempt` and `pre_session.files` are not served. They need capture-side
  Moodle reads and parser calls on the pre-session path, a change to plans 6/8. The plugin shows both as
  absent.
- **Item picker (plan 9, SHOULD)**: not built. It needs item labels in `GET /sessions/{sid}`.
- **Plan 12 requests**: `/health/assess` must read the `smtp` heartbeat row; shredded transcripts need ABANDONED
  delivery rows and a tombstone for `/state.transcripts[].purged`.
- CodeRabbit's docstring-coverage pre-merge check is still not addressed (as in W2).

## Findings worth knowing

- **CI on a PR can be red from its first push without anyone looking**: #168 had 4 failed integration jobs
  across 4 pushes until the merge check. One cause was a comment with an apostrophe inside the single-quoted
  `python -c` of the BUG-027 import step. The other two were a chaos test that needed the new SMTP settings,
  and a pre-existing 1-in-256 flake in the snapshot tamper test (it overwrote a byte with 0xff, a no-op when
  the byte already was 0xff). Check `gh pr checks` before each CodeRabbit round, not only before merging.
- A config module that imports a delivery module crossed the parser's import ceiling and stopped the parser at
  startup. Unit tests did not see it; a fresh-process startup test now does.
- CodeRabbit re-request: after an auto-pause, `@coderabbitai review` replies "Review finished" when the head
  commit was already reviewed. The "Last reviewed commit" line in the summary comment is the reliable signal.
