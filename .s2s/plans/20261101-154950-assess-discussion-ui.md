# Implementation Plan: Assess discussion UI (Vektra-origin page in a cross-origin iframe)

**ID**: 20261101-154950-assess-discussion-ui
**Status**: in_progress
**Branch**: feat/assess-discussion-ui
**Created**: 2026-11-01T14:49:50Z
**Updated**: 2026-11-01T20:58:19Z
**Approved**: 2026-11-01 (coordinator)

## Traceability

**Source**: ARCH-076, INT-001 (GET /api/v1/assess/ui, fetch-based event streaming)
**Source Type**: architecture

## References

### Requirements
- REQ-068: Student entry point, button states and pre-session screen (pre-flight announcement, hand-off) @.s2s/requirements.md
- REQ-072: Finish action and closing screen @.s2s/requirements.md
- REQ-102: Student in-session controls (minimal set) @.s2s/requirements.md
- REQ-103: 'Segnala un errore dell'assistente' marker (could) @.s2s/requirements.md
- NFR-016: Accessibility of the discussion UI and transcript (WCAG 2.1 AA) @.s2s/requirements.md

### Architecture
- ARCH-076: Discussion UI as a Vektra-origin page in a cross-origin iframe on a distinct origin @.s2s/architecture.md
- INT-001: Assess REST API (plugin and iframe to Vektra) @.s2s/architecture.md
- ARCH-067: Token contract (pre-session vs session-bound tokens, transcript download token) @.s2s/architecture.md
- COMP-002: vektra_assess API @.s2s/architecture.md

### Decisions
- ADR-0030: Token contract and UI embedding, cross-origin iframe with fetch streaming @.s2s/decisions/ADR-0030-discussion-token-contract-and-ui-embedding-separat.md

### Dependencies
- 20261101-154950-assess-session-lifecycle (Start, GET session, turns, events SSE, retry, finish, flag endpoints; session-token re-mint)
- 20261101-154950-assess-foundations (vektra_assess package, router mount, settings, ERR_ASSESS_* codes)
- Consumed by plugin plan 20261101-154950-discuss-student (P2: iframe embedding, pre-session screen, postMessage parent side)

## Overview

The student never talks to Vektra from the Moodle DOM. The plugin renders the button and the
pre-session screen; once the student presses "Inizia" it embeds an iframe pointing at
`GET /api/v1/assess/ui` on the Vektra origin and hands the freshly minted pre-session token to it
by `postMessage`. Everything after that (Start, the opening message, turns, streaming replies,
timer, warnings, outage, Fine, closing screen, transcript download) runs inside the iframe, on an
origin that teacher-authored HTML and third-party Moodle plugins cannot script.

This plan delivers the page, its static bundle, its response headers (strict CSP with
`frame-ancestors` per site), the client state machine, the fetch()+ReadableStream SSE consumer,
the accessibility work for NFR-016, and the deployment pieces that today make the iframe
impossible (the nginx example sends `X-Frame-Options: DENY` on every response).

## Design Notes

- **Origin model.** `VEKTRA_ASSESS_UI_ORIGIN` (scheme+host+port) is the public origin that serves
  the page. Each registered site carries its Moodle origin (`moodle_origin` on the site record of
  plan 1; added in the next revision in the chain if plan 1 did not create it). The page is
  `GET /api/v1/assess/ui?site=<site_key>`; `site_key` is an opaque public identifier, never a
  credential. Unknown site → 404 with `frame-ancestors 'none'`. Exactly one origin is placed in
  `frame-ancestors`; no wildcards.
- **Distinct origin is enforced, not assumed.** The REQ-079 setup check gains an item
  `ui_origin_distinct`: MISSING when the UI origin equals the Moodle origin, or either is not
  `https` (dev override `VEKTRA_ENV=development` only). The `allow-same-origin` sandbox token the
  plugin sets is safe only because the origins differ, so this check is a precondition.
- **Headers on the page** (asserted byte-for-byte in tests):
  `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; object-src 'none'; frame-ancestors <moodle_origin>; require-trusted-types-for 'script'; trusted-types 'none'`,
  `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`,
  `Permissions-Policy: clipboard-write=(self), camera=(), microphone=(), geolocation=()`,
  `Cross-Origin-Resource-Policy: same-origin`. No `X-Frame-Options` on this route (CSP governs).
  Every other route keeps `X-Frame-Options: DENY`.
- **No HTML from data.** Bot and student text are inserted with `textContent` and DOM
  construction only; `trusted-types 'none'` makes any `innerHTML` assignment throw. Line breaks
  and the system facts block (REQ-094) are rendered by building elements. Math in the live UI is
  shown as escaped TeX in a `<code>` element with an `aria-label`; static MathML is a transcript
  concern (plan 10).
- **Token handling.** Tokens live in a closure variable only: no `localStorage`,
  `sessionStorage`, cookies, URL fragments or query strings. A reload loses the token; the plugin
  re-mints a pre-session token and the iframe resumes through `GET /sessions/{sid}` (which
  re-mints the session token, plan 8). Tokens are never logged to the console.
- **postMessage protocol (v1).** Messages are `{v:1, type, ...}`. Parent→iframe: `handoff`
  (`{token, mode:"start"|"resume", sid?}`), `download-token` (`{tid, token}`). Iframe→parent:
  `ready`, `resize` (`{height}`), `download-token-request` (`{tid}`), `closed` (`{reason}`). The
  iframe accepts a message only if `event.origin === config.moodleOrigin` AND
  `event.source === window.parent`; it posts with an explicit `targetOrigin` (never `"*"`).
  The parent side (P2) mirrors this against `VEKTRA_ASSESS_UI_ORIGIN`. The allowed origin is
  injected into the page as a `<script type="application/json" id="cfg">` data block (not
  executed, so `script-src 'self'` holds).
- **Streaming.** `EventSource` cannot send `Authorization`, so the events endpoint is consumed with
  `fetch(url, {headers:{Authorization:"Bearer …", "Last-Event-ID": id}})` and a
  `ReadableStream` reader feeding an incremental SSE parser (handles CRLF/LF, events split across
  chunks, multi-line `data:`, `:` heartbeats, `id:`, `retry:`). Reconnect with jittered backoff
  (1, 2, 4, 8 s cap) and `Last-Event-ID`; 401/403/409 stop reconnecting and move to a terminal
  state. Wire format unchanged from plan 8.
- **Transcript download from the closing screen.** The iframe asks the parent for a download
  token (`download-token-request`); the plugin mints it server-side (ARCH-067: `aud=transcript`,
  single-use, TTL ≤ 2 min) and posts it back; the iframe `fetch`es `GET /transcripts/{tid}` with
  the bearer header and saves the Blob through an `<a download>` (iframe sandbox needs
  `allow-downloads`, P2). The link enables as soon as the session payload reports the transcript
  as generated, independent of delivery status (REQ-072).
- **Timer.** Remaining time is derived from the server deadline and a server-time offset taken
  from the `Date` of the last response; the server stays authoritative (plan 8 closes on timeout).
  Time warnings are the server's SSE `warning` events, announced once in a polite live region.
- **Build tooling.** A separate esbuild project at `vektra-assess/ui/` (same pattern as
  `vektra-learn/widget/esbuild.config.mjs`, TypeScript, no framework, no runtime deps) emitting a
  content-hashed `assess-ui.<hash>.js` and `assess-ui.<hash>.css` plus `manifest.json` into
  `vektra-assess/src/vektra_assess/ui/static/`. Assets are served by the UI router from the
  manifest allowlist at `/api/v1/assess/ui/assets/{name}` (not a generic `StaticFiles` mount),
  with `nosniff`, `CORP: same-origin`, immutable caching. No source maps in the image.
- **Language.** UI copy is Italian (`lang="it"`), centralised in `ui/src/messages.it.ts`; the
  conversation language attribute from the session payload is set on each turn element (REQ-100).

## Tasks

### Server side
- [x] Add `vektra-assess/src/vektra_assess/ui/router.py`: `GET /api/v1/assess/ui` (unauthenticated, `site` query param = public site key only), page template `ui/templates/page.html.j2` (autoescape) with the JSON config block, asset route `/api/v1/assess/ui/assets/{name}` resolving only names in `manifest.json`; include the router in `vektra_app/main.py create_app()`
- [x] Implement the header builder `ui/headers.py` producing the CSP and companion headers listed in Design Notes; per-site `frame-ancestors` read from the site record on every request (no cache); unknown/disabled site → 404 with `frame-ancestors 'none'`
- [x] Add settings `VEKTRA_ASSESS_UI_ORIGIN` (alias-style in the assess settings class); startup step in `_run_startup()` refuses a non-https origin outside development and an origin equal to any registered site's `moodle_origin`
- [x] Add `moodle_origin` to the site record if absent (next revision in the chain) and to the site registration/update path, validated as a bare origin (no path, no wildcard)
- [x] Add setup-check item `ui_origin_distinct` to the REQ-079 check (plan 4's check runner), reported by name as OK/MISSING
- [x] Extended `GET /api/v1/assess/state` contract (agreed with P2, session vektra-moodle-cf), every field optional and ABSENT when the state does not call for it: `active{session_no, minutes_left, deadline_at}` (states active and starting); `selected_attempt{attempt_no}` (the attempt shown on the page is not the selected one, state 9); `pre_session{duration_min, recipients[{role,name}], course_material_used, limits{max_message_chars, messages_per_min}, privacy{notice_url, notice_version}, previous_sessions, files[{name, predicted: readable|partial|unreadable}]}` (states eligible and reset_granted); `closed_at` (state 6); `ended_at` (state 11). Data, URLs and versions only, the texts stay in the plugin; `transcripts[...]` is plan 10's. A test per field and per absence; documented in `docs/reference/api.md`
- [x] postMessage contract plugin↔UI documented as a stable contract in `docs/reference/api.md`: protocol v1 (ready/handoff with the token, resize, download-token request/response, `closed`), plus `{type:"vektra-assess-unavailable"}` (the parent tells the UI origin the mint failed: the UI shows a generic text) and `vektra-assess-closed`; origin AND source checked on both sides, never `"*"`
- [x] Accessibility of the UI content (NFR-016, WCAG 2.1 AA) tested HERE (the plugin cannot run axe inside the cross-origin iframe): axe on the UI with 0 violations, the whole flow keyboard-only, reflow at 320 px
- [x] Route-inventory test `vektra-assess/tests/test_route_inventory.py`: enumerate `app.routes`; assert no assess route declares a query/cookie parameter named `token|access_token|jwt|bearer`; assert `GET .../events?token=<valid>` without header → 401; assert REQ-102 inventory: no regenerate, edit, delete-turn or upload route under `/api/v1/assess`

### Client bundle (`vektra-assess/ui/`)
- [x] Scaffold `package.json`, `esbuild.config.mjs`, `tsconfig.json`, `src/main.ts`; `npm run build` writes hashed assets + manifest; commit `package-lock.json`
- [x] `src/sse.ts`: incremental SSE parser + `fetch`/`ReadableStream` consumer with bearer header, `Last-Event-ID`, backoff and terminal-status handling
- [x] `src/bridge.ts`: postMessage protocol v1 with origin + source checks and explicit `targetOrigin`; `ResizeObserver` → `resize` messages
- [x] `src/state.ts`: explicit state machine — `awaiting_handoff` → `preparing` (Start 202; "Preparazione della discussione…" announced once) → `active` ⇄ `outage` → `closed(reason)`; plus `capacity_refused`, `start_rolled_back`, `unavailable` (non-technical texts, REQ-071/REQ-068 state 7), `resume`; one pure reducer unit-tested per transition
- [x] `src/views/chat.ts`: opening message (system facts block as a semantic list + LLM orientation, REQ-094), turn list (`<ol>` of `<article>` with role label and timestamp), composer (`<textarea>` + Invia), streaming reply rendered into a non-live element and announced once on completion via a polite live region
- [x] `src/views/controls.ts` (REQ-102): Invia, per-message Copia (`navigator.clipboard.writeText`, fallback select-text), Riprova (visible in OUTAGE; POST retry; never ends OUTAGE client-side), Fine with confirmation dialog (`<dialog>`, focus trapped, Annulla returns focus; Annulla leaves state untouched), remaining-time display (`role="timer"`, `aria-live="off"`; minute-level text for AT)
- [ ] Item picker (SHOULD): list of server-generated item labels from the session payload; selecting inserts `Spiegami: <label>` into the composer and focuses it; never sends; listbox keyboard pattern
- [x] Warnings and outage: SSE `warning` → polite live region once; `outage_start/outage_end` → banner with credited-time text; no stack traces or codes shown
- [x] `src/views/closing.ts` (REQ-072): per close reason texts (finished: "Sessione conclusa il <data>; trascrizione inviata al docente"; terminated_by_teacher: "Sessione chiusa dal docente; trascrizione disponibile"; timed_out, service_failure, budget_exhausted, access_revoked), REQ-093 hint "Ti consigliamo di scaricare una copia della trascrizione", "Visualizza / scarica la tua trascrizione" button driving the download-token flow; input disabled
- [x] REQ-103 marker (COULD; last task, skip and record if plan 8 did not ship the flag endpoint): "Segnala un errore dell'assistente" on bot turns while ACTIVE/OUTAGE only; one confirmation; marker label "segnalato dallo studente"; hidden after CLOSED
- [x] `src/messages.it.ts` with every user-facing string; no string concatenation of server text into markup

### Deployment
- [x] Fix `deploy/nginx/vektra.conf.example`: keep `X-Frame-Options DENY` at server level, add `location /api/v1/assess/ui` (and its `/assets/`) that proxies identically and re-declares HSTS + nosniff WITHOUT `X-Frame-Options` (nginx drops inherited `add_header` when a location declares its own — comment this); events location keeps `proxy_buffering off`
- [x] Check `deploy/traefik/docker-compose.traefik.yml.example` for an equivalent frame-deny middleware and exempt the UI route the same way
- [x] Dockerfile node stage: build `vektra-assess/ui` alongside the widget; copy only the built assets; extend `test_image_content_boundary.py` to assert the manifest assets are present and `ui/src`, `node_modules`, `*.map` are absent

### Tests, CI, docs
- [x] Vitest unit tests `vektra-assess/ui/test/`: SSE parser fuzz on random chunk boundaries (1-byte splits, CRLF), bridge rejects wrong origin / wrong source / `"*"` never used, reducer transitions, no `innerHTML` usage (lint rule `no-restricted-properties`)
- [x] Python tests `vektra-assess/tests/test_ui_router.py`: exact CSP string per site, unknown site 404, no XFO on UI route, XFO DENY on a sample other route, asset allowlist (path traversal `../` → 404), `Cache-Control: no-store`
- [x] Playwright e2e `vektra-assess/ui/e2e/` with a host page served from a second origin (`http://127.0.0.1:<p1>` parent vs `http://localhost:<p2>` Vektra) against the running API with the fake LLM provider and seeded session fixtures from plan 8: start → opening → 2 turns → outage (fake provider fails) → Riprova → Fine → download; resume after reload; a framing attempt from a third origin is blocked by CSP (frame not rendered, CSP violation observed)
- [x] Accessibility: `@axe-core/playwright` scan on every state (awaiting, preparing, active, streaming, outage, confirm dialog, closed) with 0 violations at WCAG 2.1 A/AA tags; keyboard-only run of the whole e2e flow (no mouse API calls); reflow check at 320 px width and at 1280 px with 200% zoom asserting `scrollWidth <= clientWidth`; live-region announcement counter (one per reply, one per warning)
- [x] CI: `ci-unit.yml` add `assess-ui` path filter + `test-assess-ui` job (npm ci, build, vitest) and add it to `ci-gate` needs; `integration.yml` add the Playwright+axe job against the compose stack; `make test-assess-ui` target; ensure the structural guard `test_suite_execution_coverage.py` recognises the JS suite or document the exemption with the CI job name
- [x] Docs: `docs/reference/api.md` section "Assess UI" (route, headers, postMessage protocol v1, no query tokens); `docs/guides/` frame-ancestors/nginx note; `CHANGELOG.md` [Unreleased] Added + Security (nginx example change)

## State & Data Lifecycle

| State element | Created by | Updated by | Invalidated/Deleted by |
|---------------|-----------|------------|------------------------|
| Session/pre-session token (iframe closure variable) | `bridge.ts` on `handoff`; Start/resume response (plan 8 re-mint) | resume re-mint, epoch bump response | page unload; terminal 401/403/409; `closed` state clears it |
| Transcript download token (iframe memory) | `download-token` message from parent | never (single use) | discarded immediately after the one `fetch`; server consumes jti (plan 10) |
| SSE cursor (`Last-Event-ID`) | first event received | every event | page unload; terminal state |
| Composer draft text | user typing | user typing | send; page unload (deliberately not persisted) |
| Per-site `moodle_origin` (site record) | site registration (plan 1/4) | site update path (this plan validates) | site removal; read uncached per page request |
| Built UI assets + `manifest.json` | Docker node stage build | each image build (new hashes) | replaced by next image; old hashes 404 |

## Acceptance Criteria

- [x] The UI route response carries the exact CSP with the site's single `frame-ancestors` origin and no `X-Frame-Options`; other routes still return `X-Frame-Options: DENY` (header bytes asserted) (ARCH-076) — evidence: vektra-assess/tests/integration/test_ui_router.py::test_the_page_headers_byte_for_byte, ::test_an_unknown_site_may_be_framed_by_nobody, vektra-app/tests/test_assess_startup.py::test_every_route_but_the_discussion_ui_denies_framing
- [x] A page on a third origin cannot render the iframe (browser CSP violation recorded in Playwright) (ARCH-076) — evidence: vektra-assess/tests/integration/test_ui_e2e.py (specs vektra-assess/ui/e2e/flow.e2e.ts "a page on a third origin cannot frame the UI": Chromium logs the frame-ancestors violation, no frame holds the UI's main#app, the UI never posts ready)
- [x] Setup check reports `ui_origin_distinct` MISSING for a same-origin or non-https configuration, OK otherwise; startup refuses the same configuration outside development (ARCH-076, REQ-079) — evidence: vektra-assess/tests/test_moodle_setup_check.py::test_ui_origin_distinct_item, vektra-app/tests/test_assess_startup.py::test_startup_refuses_a_ui_origin_not_distinct_or_not_https, ::test_the_ui_origin_setting_is_a_bare_origin
- [x] No assess route accepts a token from the query string: route inventory passes and `?token=` alone returns 401 (ADR-0030) — evidence: vektra-assess/tests/test_route_inventory.py, vektra-assess/tests/integration/test_route_credentials.py::test_no_route_accepts_a_query_string_or_cookie_credential
- [x] The iframe ignores postMessage from a non-configured origin (handoff with a valid token from a wrong origin → no network request issued, verified via Playwright request log) (ARCH-076) — evidence: vektra-assess/tests/integration/test_ui_e2e.py (specs vektra-assess/ui/e2e/flow.e2e.ts "a valid token posted from the wrong origin is ignored: no request": a third-origin frame posts a freshly minted valid token ten times, the request log has no assess API call, no session row exists); unit: ui/test/bridge.test.ts
- [x] Full e2e flow works with streaming over fetch: the reply text in the DOM equals the persisted turn text returned by `GET /sessions/{sid}` byte-for-byte (REQ-102) — evidence: vektra-assess/tests/integration/test_ui_e2e.py (specs vektra-assess/ui/e2e/flow.e2e.ts "start, two turns, warning, outage and Riprova, flag, Annulla and Fine: keyboard only": after each reply the last bot turn's text equals the stored text exactly, and every turn's text equals the stored text as rendered (one element per line); resume with and without sid in "resume after a reload")
- [x] Fine confirm → `GET /sessions/{sid}` reports CLOSED(finished) and exactly one transcript row exists; Annulla → state ACTIVE and deadline value unchanged (REQ-072) — evidence: vektra-assess/tests/integration/test_ui_e2e.py (specs vektra-assess/ui/e2e/flow.e2e.ts, same spec: Annulla leaves ACTIVE with the same deadline_at and the focus on Fine; Concludi gives CLOSED/finished and exactly one transcript_generate job, the transcript row itself is plan 10's); server side also test_session_finish.py
- [ ] The closing-screen download returns bytes whose SHA-256 equals the stored transcript hash while delivery is still PENDING (REQ-072, REQ-088) — BLOCKED (plan 10): the transcript download endpoint and download tokens do not exist; the spec "download returns the stored transcript bytes" (vektra-assess/tests/integration/test_ui_e2e.py (specs vektra-assess/ui/e2e/flow.e2e.ts) is SKIPPED as BLOCKED and the harness fails if it ever ends otherwise. What is proven now: the unanswered request times out to a generic text, asked once ("an unanswered download-token request ends in a generic text, asked once"; unit ui/test/app.test.ts)
- [ ] Selecting a picker item creates 0 turn rows (row count before/after) until Invia (REQ-102) — NOT BUILT, SHOULD deferred: it needs item labels in `GET /sessions/{sid}` (a new field on a plan-8 route); decision for plan 13 or the coordinator (see Notes)
- [x] Every control is reachable and operable keyboard-only; axe reports 0 WCAG 2.1 AA violations on every state; no horizontal scroll at 320 px / 200% zoom; exactly one live-region announcement per completed reply and per warning (NFR-016) — evidence: vektra-assess/tests/integration/test_ui_e2e.py (specs vektra-assess/ui/e2e/a11y.e2e.ts "axe: 0 WCAG 2.1 A/AA violations in every state" (awaiting, unavailable, preparing, active, streaming, outage, confirm dialog, flag dialog, closed; the run fails if a state is not scanned or the frame was not analysed), "reflow: no horizontal scroll at 320 px and at 1280 px with 200% zoom"; flow.e2e.ts drives the whole flow with Tab/Enter/Escape only (no mouse call) and counts one announcement per reply (3) and one per warning (the 5-minute warning, still 1 after 3 s)). The manual NVDA/VoiceOver checklist stays a release-plan artifact
- [x] If REQ-103 is built: one flag → exactly one marker row; flag after CLOSED → rejected and the control is absent (REQ-103) — evidence: vektra-assess/tests/integration/test_session_flag.py (one flag, one marker; after CLOSED 409), vektra-assess/ui/test/app.test.ts ('REQ-103 flag': one confirmation, one POST, marker, no control after closed)

## Testing Approach

Three layers. (1) Python route tests assert headers as raw bytes and the route inventory, so a
future route that reads `?token=` fails CI. (2) Vitest covers the parser, bridge and reducer as
pure functions, including adversarial chunk splitting. (3) Playwright drives a real browser
across two origins against the compose stack with a deterministic fake LLM, and every
user-visible claim is cross-checked against server state (`GET /sessions/{sid}`, DB row counts),
never against what the UI displays alone. Screen-reader behaviour is checked manually (NVDA +
Firefox, VoiceOver + Safari) with a recorded checklist, because no automated tool proves it.

### Test Infrastructure

- **Required**: Node 20 + npm for the bundle; Playwright browsers (Chromium, Firefox, WebKit); compose stack (postgres + vektra API + assess worker) with the fake/recording `LLMProvider` and seeded eligible-session fixtures; two distinct local origins
- **Provided by**: this plan (bundle, Playwright harness, two-origin host page); fake provider and session fixtures from 20261101-154950-assess-llm-runner and 20261101-154950-assess-session-lifecycle; compose assess profile from 20261101-154950-assess-foundations
- **False-pass guard**: the Playwright job fails (not skips) if the stack health wait or fixture seeding fails; each e2e test asserts a server-side row count before passing; axe job fails when the number of scanned states is lower than the declared state list; manual screen-reader checklist is a required artifact for the release plan, reported BLOCKED if absent

### NFR Verification

| NFR | Dataset/Load | Tool | Environment | Pass criterion |
|-----|--------------|------|-------------|----------------|
| NFR-016 | 1 quiz + 1 assignment fixture session, all UI states incl. outage and closing, citation tags present | @axe-core/playwright + keyboard-only Playwright run + viewport/zoom check + manual NVDA/VoiceOver checklist | CI integration job (compose) + manual on staging | 0 axe WCAG 2.1 AA violations; full flow keyboard-only; no horizontal scroll at 320 px / 200%; 1 announcement per reply and per warning; citation tags read as short labels |

## Integration Notes

- P2 (`discuss-student`) owns the parent side: the `<iframe>` element with
  `sandbox="allow-scripts allow-same-origin allow-downloads"` and `allow="clipboard-write"`, the
  pre-session screen, the server-side mint of pre-session and download tokens, and the parent
  postMessage handler mirroring protocol v1. The protocol doc in `docs/reference/api.md` is the
  contract both sides test against.
- Plan 8 owns the event names and payloads; this plan must not add events. Plan 10 owns the
  download-token and download endpoints.
- `create_app()` middleware order is unchanged; the CORS middleware is irrelevant to the iframe
  (same-origin calls from the Vektra origin) and must not gain the UI origin.

## Notes

- Drift: `deploy/nginx/vektra.conf.example` sets `X-Frame-Options DENY` globally; any deployment
  copying it blocks the iframe. Operators who already copied it must update their config — call
  this out in CHANGELOG Security and in the plan 13 runbook.
- Risk: residual token exposure on the Moodle origin during the hand-off (the parent briefly holds
  the pre-session token). Accepted in ARCH-067 (TTL ≤ 2 min, single jti).
- Risk: Trusted Types is Chromium-only; on Firefox/Safari the guarantee rests on the lint rule
  and code review. Keep the `no-innerHTML` lint rule mandatory.
- The structural guards in `tests/` were written for Python suites; confirm how a JS suite is
  registered so it is not silently unrun.
- OQ-026 (reference model) affects only latency feel, not this plan's acceptance.

> Note (independent review of W1, PR 146, 2026-11-03): the review-HTML sanitiser output (`SanitizeResult.text`, type `PlainText`, `vektra_assess.parser_protocol`) is TEXT, not markup, and is NOT escaped: it can contain literal `<img onerror=...>` (decoded entities) or `<math>...</math>` source. **Every consumer of `PlainText` in this plan must HTML-escape it at the render site** (`parser_protocol.html_escape`, or a contextual escaper) **and must have a test that proves it** (feed `&lt;img src=x onerror=alert(1)&gt;`, assert nothing in the rendered output parses as an element). The same holds for file text returned by `parse_file` (`ParseResult.text`) when it is rendered.

- **Implementation choices (2026-11-07, plan 9 in progress)**:
  - `site` is the site's public id (`assess_sites.id`, the `site` claim of every token): an
    opaque UUID, never a credential. A malformed, unknown or disabled site is 404.
  - The UI router is mounted with the assess router (`include_assess_router`), so it is dormant
    like every assess route while `VEKTRA_ASSESS_ENABLED=false`, instead of unconditionally in
    `create_app()`. It carries a `public_assess_ui` marker dependency: the route-scope
    structural test accepts it only under `/api/v1/assess/ui`.
  - The built bundle is never committed: without `static/manifest.json` the page is 503; tests
    point `app.state.assess_ui_static_dir` at a fixture bundle. The router tests need the site
    table, so they live in `tests/integration/test_ui_router.py` (not `tests/`).
  - `X-Frame-Options: DENY` is set by the API itself on every response but the UI page and its
    assets (`FrameDenyMiddleware`, pure ASGI so SSE is not buffered), not only by the nginx
    example: a deployment without the proxy keeps the protection. One middleware is added to
    `create_app()`; the existing order is unchanged.
  - `VEKTRA_ASSESS_UI_ORIGIN` already existed (plan 4, setup-check item `ui_not_same_origin`):
    plan 9 reuses it, now validated as a bare origin at load (a malformed value no longer
    reaches the setup check as UNVERIFIABLE: settings refuse it), and adds the startup refusal.
  - `moodle_origin` (revision 0033): there is no site registration or update path in code yet
    (sites are inserted by the operator; the teacher-ops plan 11 owns the admin surface), so the
    validation lives in the database: a CHECK accepts bare `http(s)://host[:port]` only, and
    existing rows are backfilled from their `base_url`. A row without it falls back to the
    origin of `base_url` (page and startup check alike).
  - Extended `/state` (sub-task agreed with P2): `active`, `pre_session` (duration, course
    material, limits, privacy, previous sessions), `closed_at` and `ended_at` are computed by the
    API from its own rows and settings (`VEKTRA_ASSESS_PRIVACY_NOTICE_URL` is new).
    `selected_attempt`, `pre_session.recipients` and `pre_session.files` are passed through from
    the render worker's record (`assess_gate_inputs.inputs`: `selected_attempt_no`,
    `recipients`, `files`) and stay ABSENT until the render worker writes them. Assigned
    follow-ups (decided 2026-11-07):
    - `pre_session.recipients` → plan 10 (it owns the transcript recipient resolution).
    - `selected_attempt` and `pre_session.files` → an explicit follow-up for the W3 gate, NOT a
      plan-9 task. Not small: the render worker would have to run, per student page view and
      before any session exists, the capture's attempt selection (plan 6 quiz readers) and the
      submission file listing with a per-file readability prediction (plan 6 readers + parser
      sidecar), i.e. Moodle reads and parser calls on the pre-session path with their own
      recordings, budgets and failure states; that is a capture-side change to plans 6/8, and a
      UI plan would add it without the owner's review. Until then the plugin shows those two
      parts as absent (the contract already makes every field optional).
  - postMessage contract: every type is prefixed `vektra-assess-` (the plugin's
    `vektra-assess-unavailable` and `vektra-assess-closed`, agreed with P2), so the plan's
    `handoff` is `vektra-assess-token` (the ready/token handshake) and the others are
    `vektra-assess-ready`, `-resize`, `-download-token-request`, `-download-token`. The table in
    `docs/reference/api.md` ("Assess UI") is the contract both sides test against.
  - Item picker (SHOULD): NOT built, deferred. It needs the server-generated item labels in
    `GET /sessions/{sid}` (a new field on a plan-8 route; today the payload has turns only), and
    this plan must not add payload fields to plan 8's routes. The task and its acceptance
    criterion stay `[ ]`; whether to add the field (and build the picker) is a decision for
    plan 13 or the coordinator.
  - The flag confirmation is an in-page modal `<dialog>` (`ConfirmDialog`, like Fine's), not the
    native `confirm()`: the plugin's iframe sandbox (`allow-scripts allow-same-origin
    allow-downloads`, no `allow-modals`) makes `window.confirm()` return false without asking,
    so the flag would never have been sent inside Moodle. Annulla/Escape return the focus to the
    control; after the flag the focus moves to the marker.
  - Traefik example checked (2026-11-07): it defines no headers middleware (no frameDeny, no
    customResponseHeaders), so there is nothing to exempt; the API's own FrameDenyMiddleware
    gives every non-UI route `X-Frame-Options: DENY` behind Traefik too.
  - The no-`innerHTML` rule is `npm run lint` (`scripts/no-html-sinks.mjs`: innerHTML,
    outerHTML, insertAdjacentHTML, document.write, DOMParser), not an ESLint
    `no-restricted-properties` rule: same guarantee without an ESLint toolchain. CI runs it in
    the new `test-assess-ui` job (ci-unit.yml, in `ci-gate`) with the type check, the build and
    vitest; `make test-assess-ui` does the same locally.
  - The router tests live in `tests/integration/test_ui_router.py` (they read the site table):
    exact CSP bytes per site and after an origin change, unknown/disabled site 404 with
    `frame-ancestors 'none'`, no X-Frame-Options on the UI route, asset allowlist and traversal
    404, `Cache-Control: no-store`; X-Frame-Options DENY on another route is asserted on the
    real `create_app()` stack in `vektra-app/tests/test_assess_startup.py`.

  - Two contract adjustments agreed with P2 (2026-11-07): (1) resume without `sid`: the plugin
    sends `vektra-assess-token` with `mode: "resume"` and no `sid`; the UI reads `session_id`
    from `GET /state` with the pre-session token, then resumes with `GET /sessions/{sid}` (no
    `session_id` → generic unavailable text). (2) download-token timeout: one request at a time,
    button disabled while waiting, after 10 s without an answer a generic text, the button back,
    no automatic retry, a late token ignored (until P2 ships the download part). Both in the
    api.md contract; unit tests in `ui/test/bridge.test.ts` and `ui/test/app.test.ts`.
  - Browser e2e (2026-11-07): `vektra-assess/tests/integration/test_ui_e2e.py` is the harness,
    the specs are `vektra-assess/ui/e2e/*.e2e.ts` (Playwright 1.56.1 + @axe-core/playwright
    4.11.3, `playwright-core` pinned by an npm override so axe and the runner share one).
    Instead of the compose stack the harness serves, in the test's event loop, the production
    assess + UI routers over the testcontainers Postgres (restricted API role) on
    `http://localhost:<p2>` and a Moodle stub on `http://127.0.0.1:<p1>` (host page with the
    plugin's exact sandbox, server-side mint, no `sid` on resume, no answer to a download-token
    request); the third origin is `http://localhost:<p1>`. Worker side in-process: the
    production pre-flight handler with the recorded Moodle 4.3 responses, the worker's
    `outage_tick` with a probe that follows the fake model's outage switch, the API's
    `PendingTurnDriver`, a heartbeat each tick. Same code paths as the stack, deterministic,
    and runnable by `make test-assess-e2e`. Chromium only (Firefox/WebKit: the plan's
    Test Infrastructure lists them; one engine is what the axe/keyboard/reflow criteria need,
    the others would triple the job for no extra criterion). The fixture recordings cover
    student `stu1` only, so each spec uses one of the five assignments whose pre-flight commits
    (rubric_vis, scale, wf, simple, rubric_hidden).
  - False-pass guards: the harness fails (never skips) without Node/Chromium when
    `VEKTRA_ASSESS_E2E_REQUIRED=1` (CI, make); it passes only when the 7 declared specs passed
    AND the download spec ended SKIPPED (BLOCKED, plan 10); the axe spec fails if a declared
    state was not scanned or if no node inside the UI frame was analysed; every UI claim is
    checked against server state (`/control/session`: rows and `GET /sessions/{sid}`).
  - Two real defects found by framing the UI with the plugin's sandbox (`allow-scripts
    allow-same-origin allow-downloads`), both fixed: without `allow-modals` `window.confirm()`
    returned false silently (the flag could never be sent: now an in-page `<dialog>`), and
    without `allow-forms` Chromium blocked the composer's form submission before the submit
    handler ran (Invia did nothing: now a plain button). Recorded in the api.md contract so P2
    keeps the sandbox as it is.
  - CI: the vitest suite runs in `test-assess-ui` (ci-unit.yml, in `ci-gate`); the browser e2e
    runs in the new `assess-ui-e2e` job (integration.yml, in `integration-gate`), with a
    junit guard (1 collected, 0 skipped). The structural guard
    `vektra-shared/tests/test_suite_execution_coverage.py` recognises the e2e through its
    pytest harness (collected by `package-integration`, where it is listed with the live-Moodle
    tests as an expected skip, and by `assess-ui-e2e`); the vitest suite is not pytest and is
    covered by the `test-assess-ui` job, which is the documented exemption.
- **Code review fixes (2026-11-07, `/code-review` high on develop...feat/assess-discussion-ui)**:
  - #2 One origin normalisation: `ui.origins.bare_origin` (lower-case scheme and host, default
    port dropped, IPv6 compressed in brackets, trailing dot/slash dropped; credentials, paths,
    wildcards, underscores and bad ports refused) is the only one. `url_origin(base_url)` and
    `site_origin(moodle_origin, base_url)` are built on it and replace the router's `origin_of`
    and the SQL `site_origin_sql()` (the column choice is made in Python, not with a regex in
    SQL); `ui_origin_problem` normalises both sides, so `HTTPS://M.Example:443` equals
    `https://m.example`.
  - #3 `ui_origin_distinct` judges the UI origin against the same Moodle origin the router puts
    in `frame-ancestors`: the run reads `moodle_origin` with the site row and passes it to
    `evaluate()` (`CheckContext.moodle_origin`); `site_origin()` picks the column, else the
    origin of `base_url`. `ui_not_same_origin` (same-site with base_url and the API) is
    unchanged.
  - #8 Revision 0033's backfill is done in Python with `url_origin` (the code's normalisation)
    and writes NULL when `base_url` cannot be normalised (credentials, an underscore host); its
    CHECK uses `ui.origins.ORIGIN_RE` (lower-case DNS names or a bracketed IPv6, optional port),
    the shape every `bare_origin` output has. The migration imports `vektra_assess.ui.origins`
    (pure standard library) instead of copying it: one normalisation, as asked; the price is
    that a later change to `bare_origin` changes what a fresh upgrade writes, which is the
    intended behaviour for a normalisation. Test: `vektra-assess/tests/test_migration_0033.py`
    (0032 -> 0033 with underscore, IPv6, credentials, case and default-port rows).
  - #7 `VEKTRA_ASSESS_UI_ORIGIN` is normalised by a model validator: malformed is refused only
    with `VEKTRA_ASSESS_ENABLED=true`; disabled, a `UserWarning` and the value is kept as given
    (never used). The setup check case `origin_invalid` (`ui_not_same_origin`) is restored on a
    dormant configuration, and `ui_origin_distinct` reports it as `ui_origin_invalid`.
  - #1 The page answers on `VEKTRA_ASSESS_UI_ORIGIN` only: unset, or requested with another
    scheme+`Host` (normalised), it is 503 (before any database read). The scheme is the one the
    server saw: the repo's pattern for a trusted proxy is uvicorn's own ProxyHeadersMiddleware,
    which applies `X-Forwarded-Proto` only for `FORWARDED_ALLOW_IPS` (default 127.0.0.1); no
    `X-Forwarded-Host` is read, the proxy forwards the client's `Host` (nginx example). With
    assess enabled, startup refuses an unset UI origin outside development (development starts,
    the page is 503 until set). Documented in configuration.md.
  - #9 The manifest is read once per directory (`router.manifest_of`, `functools.cache`), a
    missing bundle included: the bundle is part of the image, a new bundle is a new process.
    The directory is `STATIC_DIR`; only tests override it (`app.state.assess_ui_static_dir`).
  - #4 The public page cannot flood the database: an assess-local `ui_page` limiter (one more
    `LIMITER_NAMES` entry) keyed by client address (`VEKTRA_ASSESS_UI_PAGE_RPM`, default 60;
    429 with `Retry-After` before any lookup), and a per-process `SiteOriginCache` (30 s TTL,
    1024 sites, negative results cached too) so a repeated request costs no connection. The
    trade-off: a disabled site or a new `moodle_origin` takes effect within 30 s, not at once.
    Per address rather than per site: random site ids are the flood, so a per-site key would
    not bound it.
  - #6 Origin model, ONE backend and TWO public origins: the API origin (Moodle server-side
    mint, admin, everything else; `X-Frame-Options: DENY`) and the UI origin
    (`VEKTRA_ASSESS_UI_ORIGIN`: the page, its assets and only the student routes the page calls
    on its own origin, `connect-src 'self'`: `/state`, `/sessions...`, `/transcripts/...` for
    plan 10; everything else 404; never `X-Frame-Options`). The two rules do not conflict: the
    distinctness the setup check enforces (`ui_not_same_origin`: another site than Moodle and
    the API; `ui_origin_distinct`: another origin than every Moodle origin) is about the
    browser-visible origin, not the backend. ADR-0030 (ARCH-076: "a TLS Vektra origin reachable
    from student browsers") already describes exactly this, so no ADR change. The nginx example
    has a separate UI `server` (headers declared once at server level, no `add_header` in its
    locations); the API server returns 404 for `/api/v1/assess/ui`. Documented in the
    service-account guide; `nginx -t` passes on the example (nginx 1.27).
