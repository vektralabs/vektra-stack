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
- [ ] Accessibility of the UI content (NFR-016, WCAG 2.1 AA) tested HERE (the plugin cannot run axe inside the cross-origin iframe): axe on the UI with 0 violations, the whole flow keyboard-only, reflow at 320 px
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
- [ ] REQ-103 marker (COULD; last task, skip and record if plan 8 did not ship the flag endpoint): "Segnala un errore dell'assistente" on bot turns while ACTIVE/OUTAGE only; one confirmation; marker label "segnalato dallo studente"; hidden after CLOSED
- [ ] `src/messages.it.ts` with every user-facing string; no string concatenation of server text into markup

### Deployment
- [ ] Fix `deploy/nginx/vektra.conf.example`: keep `X-Frame-Options DENY` at server level, add `location /api/v1/assess/ui` (and its `/assets/`) that proxies identically and re-declares HSTS + nosniff WITHOUT `X-Frame-Options` (nginx drops inherited `add_header` when a location declares its own — comment this); events location keeps `proxy_buffering off`
- [ ] Check `deploy/traefik/docker-compose.traefik.yml.example` for an equivalent frame-deny middleware and exempt the UI route the same way
- [ ] Dockerfile node stage: build `vektra-assess/ui` alongside the widget; copy only the built assets; extend `test_image_content_boundary.py` to assert the manifest assets are present and `ui/src`, `node_modules`, `*.map` are absent

### Tests, CI, docs
- [ ] Vitest unit tests `vektra-assess/ui/test/`: SSE parser fuzz on random chunk boundaries (1-byte splits, CRLF), bridge rejects wrong origin / wrong source / `"*"` never used, reducer transitions, no `innerHTML` usage (lint rule `no-restricted-properties`)
- [ ] Python tests `vektra-assess/tests/test_ui_router.py`: exact CSP string per site, unknown site 404, no XFO on UI route, XFO DENY on a sample other route, asset allowlist (path traversal `../` → 404), `Cache-Control: no-store`
- [ ] Playwright e2e `vektra-assess/ui/e2e/` with a host page served from a second origin (`http://127.0.0.1:<p1>` parent vs `http://localhost:<p2>` Vektra) against the running API with the fake LLM provider and seeded session fixtures from plan 8: start → opening → 2 turns → outage (fake provider fails) → Riprova → Fine → download; resume after reload; a framing attempt from a third origin is blocked by CSP (frame not rendered, CSP violation observed)
- [ ] Accessibility: `@axe-core/playwright` scan on every state (awaiting, preparing, active, streaming, outage, confirm dialog, closed) with 0 violations at WCAG 2.1 A/AA tags; keyboard-only run of the whole e2e flow (no mouse API calls); reflow check at 320 px width and at 1280 px with 200% zoom asserting `scrollWidth <= clientWidth`; live-region announcement counter (one per reply, one per warning)
- [ ] CI: `ci-unit.yml` add `assess-ui` path filter + `test-assess-ui` job (npm ci, build, vitest) and add it to `ci-gate` needs; `integration.yml` add the Playwright+axe job against the compose stack; `make test-assess-ui` target; ensure the structural guard `test_suite_execution_coverage.py` recognises the JS suite or document the exemption with the CI job name
- [ ] Docs: `docs/reference/api.md` section "Assess UI" (route, headers, postMessage protocol v1, no query tokens); `docs/guides/` frame-ancestors/nginx note; `CHANGELOG.md` [Unreleased] Added + Security (nginx example change)

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

- [ ] The UI route response carries the exact CSP with the site's single `frame-ancestors` origin and no `X-Frame-Options`; other routes still return `X-Frame-Options: DENY` (header bytes asserted) (ARCH-076)
- [ ] A page on a third origin cannot render the iframe (browser CSP violation recorded in Playwright) (ARCH-076)
- [ ] Setup check reports `ui_origin_distinct` MISSING for a same-origin or non-https configuration, OK otherwise; startup refuses the same configuration outside development (ARCH-076, REQ-079)
- [ ] No assess route accepts a token from the query string: route inventory passes and `?token=` alone returns 401 (ADR-0030)
- [ ] The iframe ignores postMessage from a non-configured origin (handoff with a valid token from a wrong origin → no network request issued, verified via Playwright request log) (ARCH-076)
- [ ] Full e2e flow works with streaming over fetch: the reply text in the DOM equals the persisted turn text returned by `GET /sessions/{sid}` byte-for-byte (REQ-102)
- [ ] Fine confirm → `GET /sessions/{sid}` reports CLOSED(finished) and exactly one transcript row exists; Annulla → state ACTIVE and deadline value unchanged (REQ-072)
- [ ] The closing-screen download returns bytes whose SHA-256 equals the stored transcript hash while delivery is still PENDING (REQ-072, REQ-088)
- [ ] Selecting a picker item creates 0 turn rows (row count before/after) until Invia (REQ-102)
- [ ] Every control is reachable and operable keyboard-only; axe reports 0 WCAG 2.1 AA violations on every state; no horizontal scroll at 320 px / 200% zoom; exactly one live-region announcement per completed reply and per warning (NFR-016)
- [ ] If REQ-103 is built: one flag → exactly one marker row; flag after CLOSED → rejected and the control is absent (REQ-103)

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
    `recipients`, `files`) and stay ABSENT until the render worker writes them: that needs the
    capture's attempt selection and file listing (plan 6 readers) and the transcript recipient
    resolution (plan 10), so it is OPEN, for plan 10 or a plan-9 follow-up, not done here.
  - postMessage contract: every type is prefixed `vektra-assess-` (the plugin's
    `vektra-assess-unavailable` and `vektra-assess-closed`, agreed with P2), so the plan's
    `handoff` is `vektra-assess-token` (the ready/token handshake) and the others are
    `vektra-assess-ready`, `-resize`, `-download-token-request`, `-download-token`. The table in
    `docs/reference/api.md` ("Assess UI") is the contract both sides test against.

