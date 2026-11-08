# API reference

All examples use `$VEKTRA_API_KEY` as the Bearer token. Replace with your actual API key.

Base URL: `http://localhost:8000` (configurable via `VEKTRA_PORT`).

Interactive API documentation is available at `/docs` (Swagger UI).

## Authentication

All endpoints except `GET /health` (shallow) require a Bearer token:

```text
Authorization: Bearer <api-key>
```

API keys have scopes: `admin`, `ingest`, `query`. Each endpoint requires specific scopes as noted below. `admin` satisfies any of these three.

The assessment discussion feature adds four **site-bound** scopes for the Moodle plugin: `assess:mint`, `assess:teacher`, `assess:privacy-export`, `assess:privacy-erase`. They are exact: `admin` does **not** satisfy them, and a key holding any `assess:*` scope cannot also hold `admin`, `ingest` or `query` (refused with `422 ERR-ADMIN-001`, and by the database constraint `ck_api_keys_assess_scope_exclusive`). No assess route exists while `VEKTRA_ASSESS_ENABLED=false`.

## Health

### GET /health

Shallow health check (unauthenticated).

```bash
curl -s http://localhost:8000/health | python3 -m json.tool
```

```json
{
    "status": "healthy",
    "timestamp": "2026-01-01T00:00:00Z"
}
```

Status values: `healthy`, `degraded`, `unhealthy`. Returns HTTP 200 for healthy/degraded, 503 for unhealthy.

### GET /health?detail=full

Deep health check with component breakdown. Requires any valid Bearer token.

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/health?detail=full" | python3 -m json.tool
```

### GET /health/memory

Process memory statistics. Requires any valid Bearer token.

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/health/memory | python3 -m json.tool
```

### GET /health/{component}

Single component health. Requires any valid Bearer token.

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/health/llm | python3 -m json.tool
```

```json
{
    "name": "llm",
    "status": "healthy",
    "latency_ms": 42,
    "message": null
}
```

Components: `llm`, `embedding`, `vector_store`, `assess`.

`/health/assess` is the readiness of the assessment discussion feature. It reports `disabled` when
`VEKTRA_ASSESS_ENABLED=false`, and is never `healthy` without evidence from the worker: it reads the
worker heartbeat row. Like `llm`, it is left
out of the shallow `GET /health`, so a stopped assess worker (a separate container) does not make the API
container look unhealthy; it is included in `?detail=full`.

### Assess setup check (assess)

Only mounted when `VEKTRA_ASSESS_ENABLED=true`. Callable by an `admin` key or by the site's own `assess:teacher` key (a narrow key bound to another site gets the same `404` as an unknown site; no cross-site probing).

| Method | Path | Success | Errors |
|--------|------|---------|--------|
| POST | `/api/v1/assess/sites/{site_id}/setup-check` | `202 {"report_id": "<uuid>"}` | `401 ERR-AUTH-001`, `403 ERR-AUTH-003`, `404 ERR-ASSESS-003`, `429 ERR-ASSESS-002` (one check per site per 5 minutes, `Retry-After` set), `503 ERR-ASSESS-001` (no fresh worker heartbeat: the check would otherwise stay queued forever) |
| GET | `/api/v1/assess/sites/{site_id}/setup-check/{report_id}` | `200` content-free report | `401`, `403`, `404 ERR-ASSESS-003` |

The report is `{"report_id", "site_id", "status": "queued|running|done|failed", "moodle_release", "items": [{"name", "result": "OK|MISSING|UNVERIFIABLE", "reason_code"}], "created_at", "finished_at"}`. It carries no Moodle text, no emails and no ids beyond `site_id`. The feature is available for a site only if its latest finished report for the current Moodle release has every item `OK`.

### Assess model activation (NFR-023)

Only mounted when `VEKTRA_ASSESS_ENABLED=true`. Exact `admin` scope. Without a passing activation for the (model, `prompt_version`) the API still starts (degraded, `assess_llm_unactivated` logged, so this endpoint is reachable) and the session pre-flight refuses every discussion until a passing row exists; any change to a prompt template, a shipped string or the post-check lexicon is a new `prompt_version` and needs a new activation.

| Method | Path | Success | Errors |
|--------|------|---------|--------|
| POST | `/api/v1/assess/admin/model-activations` | `201 {"id", "model", "prompt_version", "report_sha256", "activated_at"}` | `401 ERR-AUTH-001`, `403 ERR-AUTH-003`, `422 ERR-ASSESS-004` (the remediation lists the failing check codes), `503 ERR-ASSESS-001` |

The body is the JSON report written by `make eval-assess`. The endpoint never trusts its `status`: it recomputes the SHA-256 over the canonical body, requires every HARD class (deterministic and judge-based) at 0, a judge calibration of `PASS` and a declared data basis. Every attempt, accepted or refused, writes an audit event with codes only; rows are append-only.

### Assess student discussion: sessions, turns and events (iframe client)

Only mounted when `VEKTRA_ASSESS_ENABLED=true` (plan assess-session-lifecycle). The Moodle plugin
mints a pre-session token server-side with its site-bound `assess:mint` key; every other route
takes a discussion token in `Authorization: Bearer` only (a token in a query string or a cookie is
refused, ADR-0030). Request bodies forbid unknown fields. The one exception to "no Moodle id" is
`POST /tokens`, called server-side by the trusted plugin: it must send `cmid` (and `sub`) and may
send `courseid` as an optional hint (never trusted: the activity is resolved inside the student's
own courses). No student-facing route takes a Moodle, course or attempt id: the attempt is
selected server-side at Start.

| Method | Path | Token | Success | Errors |
|--------|------|-------|---------|--------|
| POST | `/api/v1/assess/tokens` | `assess:mint` API key | `201 {"token", "expires_in"}` pre-session token | `401 ERR-AUTH-001`, `403 ERR-ASSESS-006` (login-as), `403 ERR-ASSESS-007` (key not bound to the site), `400 ERR-ASSESS-014`, `403 ERR-ASSESS-016` (plugin too old) |
| GET | `/api/v1/assess/state` | pre-session | `200 {"state", "reason", ...}` the student's render state | `401 ERR-ASSESS-005`, `429 ERR-ASSESS-012` (`state_rate`) |
| POST | `/api/v1/assess/sessions` | pre-session | `202 {"session_id", "state": "STARTING", "token", "session_no"}` | `401 ERR-ASSESS-005` (also a jti never minted by the site's current mint key: `mint_correlation`), `409 ERR-ASSESS-015` (gate code, `session_already_open`, `already_discussed`, `activity_not_configured`), `429 ERR-ASSESS-012` (`start_rate_cap`, `retry_after`), `503 ERR-ASSESS-013` (capacity), `503 ERR-ASSESS-001` (worker not ready, `start_breaker_open`) |
| GET | `/api/v1/assess/sessions/{sid}` | session (or pre-session: resume, re-mints) | `200 {"session_id", "state", "close_reason", "session_no", "deadline_at", "server_now", "remaining_s" (frozen at the outage start while in OUTAGE), "committed_at", "reply_pending", "turns": [{"turn_no", "role", "kind", "text", "flagged"}]}` | `401`, `404 ERR-ASSESS-008`, `409 ERR-ASSESS-009` (closed), `409 ERR-ASSESS-011`, `503 ERR-ASSESS-001` |
| GET | `/api/v1/assess/sessions/{sid}/events` | session | `200 text/event-stream` (below) | as GET; a CLOSED session answers a content-free `state` + `closed` |
| POST | `/api/v1/assess/sessions/{sid}/turns` | session | `202 {"session_id", "turn_no"}`; the reply streams on the events | `400 ERR-ASSESS-014` (`message_empty`, `message_too_long`), `409 ERR-ASSESS-010` (reply pending), `409 ERR-ASSESS-011` (deadline passed, OUTAGE), `429 ERR-ASSESS-012` (`message_rate` "Attendi qualche secondo"), `503 ERR-ASSESS-001` (`enrolment_freshness`) |
| POST | `/api/v1/assess/sessions/{sid}/retry` | session | `202 {"session_id", "state", "reply_pending"}`; asks the server to answer a pending turn now, never ends OUTAGE | `409`, `429 ERR-ASSESS-012` (`retry_rate`) |
| POST | `/api/v1/assess/sessions/{sid}/turns/{turn_no}/flag` | session | `201` (or `200`: already flagged) `{"session_id", "turn_no", "flagged": true}` | `400 ERR-ASSESS-014` (`not_a_bot_turn`), `409 ERR-ASSESS-009` after the close, `429 ERR-ASSESS-012` (`flag_rate`) |
| POST | `/api/v1/assess/sessions/{sid}/finish` | session | `200 {"session_id", "state": "CLOSED", "close_reason"}`, idempotent | `401`, `404`, `409` |

**Transcripts** (plan assess-transcript-delivery; REQ-074, REQ-082, REQ-088, BR-016). One transcript
per CLOSED session, generated by the worker within a minute of the close; its id is the session id.
The plugin mints a download token SERVER-SIDE and hands it to the browser, which fetches the file
with the token in `Authorization: Bearer` only.

| Method | Path | Credential | Success | Errors |
|--------|------|------------|---------|--------|
| POST | `/api/v1/assess/transcripts/{tid}/download-token` | site-bound `assess:mint` key with `{"sub"}` (the owner) or `assess:teacher` key with `{"actor_userid"}` (a grader of the activity, checked against the worker's fresh grader list); `admin` never applies | `201 {"token", "expires_in"}`: single use, `aud=transcript`, at most 120 s (`VEKTRA_ASSESS_DOWNLOAD_TOKEN_TTL_S`) | `400 ERR-ASSESS-014`, `403 ERR-AUTH-003` (another student's transcript, audited), `403 ERR-ASSESS-007`, `403 ERR-ASSESS-021` (not a grader, or the grader list is stale: a refresh is queued, retry), `404 ERR-ASSESS-017` (unknown, other site, not generated yet), `429 ERR-ASSESS-012` (`download_rate`) |
| GET | `/api/v1/assess/transcripts/{tid}` | download token | `200` the exact generated bytes, `Content-Type: text/html; charset=utf-8`, `Content-Disposition: attachment; filename="<course>_<cmid>_<userid>_s<N>_<yyyymmdd>.html"; filename*=UTF-8''...`, `Content-Security-Policy: sandbox; default-src 'none'`, `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`, `Referrer-Policy: no-referrer` | `401 ERR-ASSESS-018` (invalid, expired, already used, other transcript), `403` (ownership or grader re-check on the request), `404 ERR-ASSESS-017`, `500 ERR-ASSESS-020` (fails its stored SHA-256: never served) |
| POST | `/api/v1/assess/transcripts/{tid}/resend` | `assess:teacher` key with `{"actor_userid"}` (a grader) or an operator `admin` key with `{}` | `202 {"transcript_id", "round"}`: round n+1 to the recipients of the last round; the worker re-reads the activity's configured recipients at send time (a removed address is `REPLACED`, an added one gets its own row, at most 3 per round) | `400 ERR-ASSESS-019` (any recipient-like field in the body or the query: recipients cannot be named), `400 ERR-ASSESS-014`, `403 ERR-ASSESS-021`, `404 ERR-ASSESS-017`, `429 ERR-ASSESS-012` (`resend_interval`: a round less than `VEKTRA_ASSESS_RESEND_MIN_INTERVAL_S` ago, `Retry-After`; `resend_rate`: per key) |

**Teacher configuration** (plan assess-teacher-ops; REQ-077, REQ-082 as amended, BR-013,
ARCH-071, ARCH-072). Server-to-server from the plugin's activity settings page. The credential is
the site-bound `assess:teacher` key (exact scope: an `admin` key is `403 ERR-AUTH-003`); the
acting Moodle user must be an EDITOR of the activity (`moodle/course:manageactivities`) in the
worker's fresh facts for (site, cmid). The course id and activity type the plugin sends are hints:
the worker resolves the cmid inside that course only. Missing or stale facts: `403 ERR-ASSESS-021`
(`editor_check_stale`) and a refresh is queued; retry after a few seconds.

| Method | Path | Credential | Success | Errors |
|--------|------|------------|---------|--------|
| GET | `/api/v1/assess/activities/{cmid}/config` | `assess:teacher` key; headers `X-Assess-Actor` (Moodle user id), `X-Assess-Course`, `X-Assess-Activity-Type` (`quiz`/`assign`) | `200` the view below, `Cache-Control: no-store` | `400 ERR-ASSESS-014` (`cmid`, `x_assess_actor`), `403 ERR-ASSESS-007`, `403 ERR-ASSESS-021` (`not_an_editor`, `editor_check_stale`; audited `assess_teacher_unverified`) |
| PUT | `/api/v1/assess/activities/{cmid}/config` | `assess:teacher` key; body below (at most 256 KiB) | `200` the view below (new `version`) | `400 ERR-ASSESS-014` (malformed body, unknown field, wrong type), `400 ERR-ASSESS-022` (field errors, all at once, nothing saved), `403 ERR-ASSESS-007`, `403 ERR-ASSESS-021`, `409 ERR-ASSESS-023` (`expected_version` is not `details.current_version`: reload, nothing saved), `503 ERR-ASSESS-001` (`teacher_config_key`) |

PUT body: `actor_userid`, `courseid`, `activity_type`, `expected_version` (0 for an activity never
saved) are required. Every other field is optional; ABSENT means unchanged.
`enabled` (bool), `cap_minutes` (5-120), `rubric_source` (`none`/`pasted`, plus `moodle` for an
assignment), `show_solution` (bool, default off), `rag_enabled` (only `false` is accepted: `true`
is the field error `rag_unavailable` with `reason: operator_flag_off`), `available_until`
(ISO 8601 with a time zone, or `null` for no limit; after the effective opening).
Write-only fields (OQ-034): `solution` and `pasted_rubric` (text, at most 20,000 code points after
NFC normalisation with control characters other than newline and tab removed; text that is empty
after that is a clear), `transcript_recipients` (1 to 3 addresses, each in
`VEKTRA_ASSESS_RECIPIENT_DOMAINS`; case-insensitive duplicates collapse). For these, `null` clears
and a value replaces. They are sealed to the worker's key; no response, log or audit row ever
contains them (the audit records `set`/`replaced`/`cleared` and, for recipients, the
`sha256(site|address)` refs old/new).
Enable (`enabled: true`) is refused, as field errors, for: a quiz without a close date or an
assignment without a cut-off (BR-007), team submission, marking-guide grading (EX-023), the
Moodle-rubric source without a rubric, no recipients, no cap, privacy settings unset (REQ-089:
always, until plan assess-privacy-retention lands) and a setup check that is not green.

`details.fields` items: `{"field", "code"}` plus the rule's bounds: `range` (`"5-120"`,
`"0-20000"`, `"1-20000"`, `"1-3"`), `allowed_domains`, `index` (the recipient's position, never
the address), `reason`, `opening_at`. Codes: `out_of_range`, `too_long`, `empty`, `required`,
`invalid_address`, `domain_not_allowed`, `too_many`, `rag_unavailable`, `not_for_activity_type`,
`timezone_required`, `opening_unknown`, `not_after_opening`, `moodle_rubric_missing`, and on
`enabled`: `quiz_without_close_date`, `assignment_without_cutoff`, `team_submission`,
`marking_guide`, `moodle_facts_unreadable`, `privacy_unset`, `setup_check_red`.

View (GET and PUT): `cmid`, `activity_type`, `version`, `configured`, `enabled`, `cap_minutes`,
`rubric_source`, `show_solution`, `rag_enabled` (always `false`), `available_until`,
`solution_state` / `pasted_rubric_state` / `transcript_recipients_state` (`impostata` or
`non impostata`), `transcript_recipients_count`, `opening` (`{"at", "source"}`, source
`activity_close`, `attested` or `unknown`), `warnings`
(`available_until_not_after_opening`, `review_options_hide_content`), `active_sessions`
(ACTIVE + OUTAGE), `updated_at`. A change never reaches a session that has already started.

**`GET /state`, extended fields** (plan 9, agreed with the plugin). Every field is optional and
ABSENT when the state does not call for it; Vektra sends data, URLs and versions only, the
plugin owns every text.

| Field | Present in | Content |
|-------|-----------|---------|
| `active` | `active`, `starting` | `{session_no, minutes_left?, deadline_at?}`: `deadline_at` once committed; `minutes_left` whole minutes rounded UP (1 until the very end, 0 only once the time is over; the UI's timer rounds the same way), frozen at the outage start while in OUTAGE, the frozen duration while starting |
| `selected_attempt` | when the render worker recorded the selected attempt | `{attempt_no}`: the plugin compares it with the attempt its page shows (REQ-068 state 9) |
| `pre_session` | `eligible`, `reset_granted` | `{duration_min, course_material_used, limits{max_message_chars, messages_per_min}, privacy{notice_url?, notice_version}, previous_sessions, recipients?[{role, name}], files?[{name, predicted: readable\|partial\|unreadable}]}` |
| `closed_at` | `closed` (state 6) | the last close of the student's session, ISO 8601 |
| `ended_at` | `ended` (state 11) | the activity's end date, ISO 8601 |
| `transcripts` | any state, once the student has a CLOSED session (plan 10) | `[{tid, n, available, purged}]`, one per CLOSED session in session order: `tid` the transcript id (equal to the session id, so the discussion UI, which knows only its session, can ask for it), `n` the session number, `available` generated and downloadable, `purged` always `false` in this release: the purge (plan assess-privacy-retention) deletes the row and will set it from its tombstone; until then a purged transcript reads as `available: false` |

`recipients`, `files` and `selected_attempt` come from the render worker's record and are
omitted while it has not recorded them. `recipients` (REQ-082 as amended) are the transcript recipients
the teacher configured for the activity (1 to 3 addresses in the sealed teacher config), as the
render worker read them: `role` is `recipient` and `name` the address with its local part masked
(`m***@unical.it`). `/state` never carries a full address or another user's id; the plugin, which
holds the configuration, may show the full addresses on its own pre-session screen.

**Tokens** (HS256, `aud=discussion`). Pre-session: `sub`, `site`, `cmid`, `stage: "pre"`, `jti`,
`loginas: false`, `iat`, `exp` (at most 120 s, single use at Start). Session-bound: `sub`, `site`,
`cmid`, `sid`, `seq` (the session number), `epoch`, `jti`, `loginas: false`, `exp` (never past
deadline plus `VEKTRA_ASSESS_REPLY_GRACE_S`). Every close bumps the session's epoch: an older-epoch
token of the owner is answered as closed (`ERR-ASSESS-009`), on a live session any epoch mismatch
is a revocation (`401 ERR-ASSESS-005`). A token without `loginas: false` is refused with `403
ERR-ASSESS-006` on every route.

**Events** (`text/event-stream`, `Last-Event-ID` replays the in-memory ring). Payloads are
content-free except `reply_delta`, which carries post-checked text only and is delivered only while
the row is live at the token's epoch. `state` (snapshot on connect), `committed`,
`start_rolled_back` (`reason`, `technical_problem_persistent` after 3 consecutive system
rollbacks), `capacity_refused`, `turn_accepted`, `reply_delta`, `reply_done`, `reply_interrupted`
(`reason`: `provider_refused`, `internal_error`, `append_refused`, `persist_failed`), `warning`
(`minutes`: 10 / 5 / 1 against the current deadline), `budget_warning` (`percent: 80`),
`outage_start`, `outage_end` (with the moved `deadline_at`), `closed` (`close_reason`,
`message_code`: `closed_finished`, `closed_timed_out`, `closed_service_failure`,
`closed_budget_exhausted`, `closed_terminated_by_teacher`, `closed_access_revoked`; the plugin
renders the Italian text).

**Timing.** Deadline = commit + duration (cap + extra time, frozen at Start) + credited outage.
Input locks at the deadline; an in-flight reply may finish until deadline + grace, otherwise it is
persisted cut and marked "[risposta interrotta]". A provider outage moves the session to OUTAGE
(timer frozen, input locked, the pending turn kept); the worker's probe ends it and credits the
interval when at least G seconds, the cumulative credit capped at C = min(15 min, half the
duration); beyond C, or after 3 consecutive failed calls, the session closes `service_failure` and
the close queues a `teacher_notify` job in the same transaction (delivery is plan
transcript-delivery's). Limits and their sizing: `docs/reference/configuration.md`.

### Assess UI: the discussion page and the postMessage contract (plugin <-> UI)

`GET /api/v1/assess/ui?site=<site id>` is the student discussion page, on
`VEKTRA_ASSESS_UI_ORIGIN`, framed by the site's Moodle origin only (`frame-ancestors`, the
site's `moodle_origin`). It is public: it carries no data and no credential, and never reads a
token from its URL (no route does). Response headers: the strict CSP (`default-src 'none'`,
`'self'` for scripts, styles, connections, images and fonts, `frame-ancestors <moodle origin>`,
trusted types with no policy), `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`,
`Cache-Control: no-store`, `Permissions-Policy: clipboard-write=(self), camera=(), microphone=(),
geolocation=()`, `Cross-Origin-Resource-Policy: same-origin`, and no `X-Frame-Options` (every
other API response has `X-Frame-Options: DENY`). An unknown or disabled site is 404 with
`frame-ancestors 'none'`. Every refusal carries the standard error envelope with the page or asset headers:
`404 ERR-ASSESS-003` (unknown site or asset), `429 ERR-ASSESS-012` (`ui_page_rate`, `Retry-After`),
`503 ERR-ASSESS-001` (remediation names `ui_origin`, `site_origin` or `ui_bundle`). Assets: `GET /api/v1/assess/ui/assets/{name}`, content-hashed names
from the build manifest only.
The page carries a JSON data block (never executed) with `site`, `moodleOrigin`, `apiBase` and
`maxMessageChars` (`VEKTRA_ASSESS_MAX_MESSAGE_CHARS`, the composer's limit).

The page works inside the plugin's iframe sandbox `allow-scripts allow-same-origin
allow-downloads` (plus `allow="clipboard-write"`): it never submits a form (no `allow-forms`:
Invia is a plain button) and never calls `alert`/`confirm`/`prompt` (no `allow-modals`: the
Fine and flag confirmations are in-page `<dialog>`s). The browser e2e
(`vektra-assess/tests/integration/test_ui_e2e.py`, `make test-assess-e2e`) frames it with
exactly that sandbox.

**postMessage contract, version 1 (stable; the plugin P2 and the UI both test against it).**
Every message is a plain object `{v: 1, type: "vektra-assess-<name>", ...}`. A receiver acts on
a message only when ALL hold: `event.origin` is exactly the configured peer origin (the UI:
the site's Moodle origin from its page config; the plugin: `VEKTRA_ASSESS_UI_ORIGIN`),
`event.source` is the expected window (the UI: `window.parent`; the plugin: the iframe's
`contentWindow`), `v === 1` and `type` is known. Anything else is ignored silently. Senders
always pass the explicit peer origin as `targetOrigin`; `"*"` is never used. Tokens never
appear in a URL, in storage or in a log, on either side.

| Direction | `type` | Fields | Meaning |
|-----------|--------|--------|---------|
| UI -> plugin | `vektra-assess-ready` | none | the page is loaded and listening; the handshake starts here |
| plugin -> UI | `vektra-assess-token` | `token`, `mode: "start" \| "resume"`, `sid?` (optional, the plugin does not send it) | the freshly minted pre-session token (server-side mint, single use, TTL <= 2 min); the UI Starts or resumes with it |
| plugin -> UI | `vektra-assess-unavailable` | none | the mint failed; the UI shows a generic, non-technical text and makes no request |
| UI -> plugin | `vektra-assess-resize` | `height` (CSS px) | the content height, so the parent can size the iframe |
| UI -> plugin | `vektra-assess-download-token-request` | `tid` | the closing screen asks for a transcript download token |
| plugin -> UI | `vektra-assess-download-token` | `tid`, `token` | the single-use download token (`aud=transcript`, plan 10) |
| UI -> plugin | `vektra-assess-closed` | `reason` (a close reason) | the session is closed; the plugin may refresh its button state |

Handshake: the UI posts `vektra-assess-ready` once it listens; the parent answers with either
`vektra-assess-token` or `vektra-assess-unavailable`. The UI holds the token in memory only; a
reload loses it and the parent mints a new pre-session token for a resume (`GET
/sessions/{sid}` re-mints the session token).

Resume without `sid`: the plugin sends `mode: "resume"` and no `sid`. The UI then reads the
open session's `session_id` from `GET /state` with the same pre-session token and resumes with
`GET /sessions/{sid}`; when `/state` has no `session_id` (or fails) the UI shows the generic
unavailable text. A `sid` that is present but not a string makes the message invalid (ignored).

Download-token timeout: the UI sends one `vektra-assess-download-token-request` at a time and
disables the download button while it waits. If no `vektra-assess-download-token` for that `tid`
arrives within 10 s (a plugin that does not implement the download part yet), the UI shows a
generic text ("Non è stato possibile scaricare la trascrizione. Riprova più tardi."), enables the
button again and does not retry by itself; a token arriving after the timeout is ignored.

### Assess discussion context: Part A and item references (iframe client)

The opening message and the item references the session routes above serve.

- **Part A** of the opening message is system text, never generated by the model: activity, "Tentativo #k di M (metodo: …)" for a quiz, the fixed statement "Voto registrato: <X> (letto il <data>). Il voto NON è stato modificato da questa discussione; decide l'orale" with the grade exactly as Moodle shows it to the student, "Sessione #N", duration, "Materiale del corso: usato / non usato", "Soluzione: mostrata / non mostrata", the overview of items, one REQ-101 status per submitted file, and how to proceed. A screen-reader summary "Discussione avviata: tentativo #k, N elementi, durata X minuti" is provided separately. Part A comes as plain text (not escaped: student-controlled file names flow through it) and as an HTML-escaped variant; a client that renders the plain text into HTML must escape it.
- **Item labels** are snapshot-local and the only way to refer to an item: "Domanda n" (numbered as on the student's review page), "Criterio n" (rubric order), "Valutazione complessiva", "Consegna". The server resolves a label, an ordinal ("la terza", "il secondo criterio") or keywords against the frozen snapshot. An ambiguous reference returns at most 3 candidates and the bot asks; an unknown label returns "Non trovo <label> in questo tentativo; ecco l'elenco: …". Anything shaped like a Moodle id (`slot`, `cmid`, `attemptid`, a bare number that is not a label) is rejected and audited `assess_item_ref_rejected`, and so is a picker request carrying a label that is not in the snapshot.
- Hidden marks never appear: the overview states that the review options hide them. Items the assistant cannot read (question types extracted structurally only) are flagged in the overview.

### GET /api/v1/health

Vector store health as seen by `vektra-index` (unauthenticated). Distinct from `GET /health`: this one checks only the **active** vector store, so it turns unhealthy when the store actually backing the index is unreachable.

```bash
curl -s http://localhost:8000/api/v1/health | python3 -m json.tool
```

```json
{
    "status": "healthy",
    "component": "vektra-index",
    "latency_ms": 1
}
```

## API keys

### POST /api/v1/api-keys

Create a new API key. Requires `admin` scope or the bootstrap key (first use only).

**Scopes**: `admin` (or bootstrap key)

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"label":"my-key","scopes":["admin","ingest","query"]}' \
  http://localhost:8000/api/v1/api-keys | python3 -m json.tool
```

Request body:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `label` | string | no | Human-readable label |
| `scopes` | string[] | no | Permissions: `admin`, `ingest`, `query`, or the site-bound `assess:mint`, `assess:teacher`, `assess:privacy-export`, `assess:privacy-erase` (never mixed with the first three). Defaults to `["admin"]`. Invalid scopes and mixed sets are rejected with `422 ERR-ADMIN-001`. |
| `expires_at` | datetime | no | Expiry timestamp. Must be timezone-aware and in the future, otherwise `422 ERR-ADMIN-004`. Omit for a non-expiring key. |

Response (HTTP 201):

```json
{
    "id": "550e8400-...",
    "key": "vk_abc123...",
    "key_preview": "vk_abc...xyz",
    "label": "my-key",
    "scopes": ["admin", "ingest", "query"],
    "created_at": "2026-01-01T00:00:00Z"
}
```

The `key` field is shown only once. Store it securely.

### GET /api/v1/api-keys

List all API keys (metadata only). Requires `admin` scope.

**Scopes**: `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/api/v1/api-keys | python3 -m json.tool
```

### DELETE /api/v1/api-keys/{key_id}

Revoke an API key. Requires `admin` scope.

**Scopes**: `admin`

```bash
curl -s -X DELETE \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/api/v1/api-keys/550e8400-...
```

Returns HTTP 204 (no body).

## Namespaces

### GET /api/v1/admin/namespaces/{namespace_id}/config

Read a namespace's stored config and the effective values after env-default fallback. Symmetric to the PATCH endpoint below.

**Scopes**: `admin`

```bash
curl -s -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/api/v1/admin/namespaces/default/config | python3 -m json.tool
```

Response (HTTP 200):

```json
{
    "namespace_id": "default",
    "config": {"grounding_mode": "hybrid"},
    "resolved": {
        "grounding_mode": "hybrid",
        "show_sources": true
    }
}
```

- `config` mirrors the raw JSONB stored in `namespaces.config` (`{}` when no key has ever been set).
- `resolved` is the value queries actually observe at runtime, after the namespace > env > hardcoded-default chain. Always includes every key in the whitelist. Use this for "Use default" / "Override" form rendering in upstream plugins (e.g. the Moodle block edit form).

Errors:
- `404 ERR-ADMIN-005` if the namespace does not exist.

### PATCH /api/v1/admin/namespaces/{namespace_id}/config

Partially update a namespace's behavioral config (JSONB). Requires `admin` scope.

**Scopes**: `admin`

Used by upstream plugins (e.g. the Moodle block) to toggle per-course settings such as the grounding mode without touching environment variables. The read path is `resolve_grounding_mode()` in `vektra-shared/src/vektra_shared/namespace.py`.

```bash
curl -s -X PATCH \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"grounding_mode":"hybrid"}' \
  http://localhost:8000/api/v1/admin/namespaces/default/config | python3 -m json.tool
```

Request body (flat dict, one entry per config key):

| Field | Type | Allowed values | Description |
|-------|------|----------------|-------------|
| `grounding_mode` | string or null | `"strict"`, `"hybrid"`, `null` | RAG grounding policy. `null` removes the key and falls back to `VEKTRA_PROMPT_GROUNDING_MODE`. |
| `show_sources` | bool or null | `true`, `false`, `null` | Widget citation visibility (FEAT-014). `null` removes the key and falls back to `VEKTRA_LEARN_SHOW_SOURCES`. The API always returns the full sources list; the flag only instructs the widget whether to render them. Resolution chain: client `data-show-sources` attr > `namespaces.config.show_sources` > `VEKTRA_LEARN_SHOW_SOURCES` env > hardcoded `true`. |
| `citations_enabled` | bool or null | `true`, `false`, `null` | Inline source citations (FEAT-021, advanced pipeline only). When `true`, the LLM is instructed to add `[n]` markers matching the context sources, and each returned source carries a `title` ("filename, p.N") for tooltip rendering. No env var: `null` (or absent) means disabled. Default-off leaves prompts unchanged. |

Behavior:
- **Partial update**: keys not present in the body are preserved.
- **Unknown keys rejected** with `400 ERR-ADMIN-006` (not silently ignored — surfaces typos early).
- **Invalid values rejected** with `400 ERR-ADMIN-007` (includes type mismatches, e.g. a JSON string passed for a boolean key).
- **Missing namespace** returns `404 ERR-ADMIN-005`.

Response (HTTP 200):

```json
{
    "namespace_id": "default",
    "config": {"grounding_mode": "hybrid", "show_sources": false}
}
```

## Ingest

### POST /api/v1/ingest

Upload and process a document. Requires `ingest` or `admin` scope.

**Scopes**: `ingest`, `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -F "file=@document.pdf;type=application/pdf" \
  "http://localhost:8000/api/v1/ingest?namespace=default" | python3 -m json.tool
```

Query parameters:

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `namespace` | string | `default` | Target namespace for the document |

Form fields:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file` | file | yes | The document to ingest |
| `namespace` | string | no | Overrides the query parameter when present |
| `metadata` | string (JSON) | no | Flat JSON object merged into every chunk of this document |

Supported formats: PDF, DOCX, PPTX, Markdown (`text/markdown`). Maximum file size: 50 MB (configurable via `VEKTRA_MAX_FILE_SIZE_MB`).

#### Per-document metadata

`metadata` carries attributes that belong to the document rather than to its text. They are merged into every chunk's metadata, stored in the vector store payload, and available for filtering.

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -F "file=@chapter-04.pdf;type=application/pdf" \
  -F 'metadata={"hidden_from_students": true, "course_id": "INF-2026"}' \
  "http://localhost:8000/api/v1/ingest?namespace=INF-2026"
```

Rules, all enforced with `ERR-INGEST-005` (HTTP 422):

- a JSON **object**, not an array or a scalar
- **flat**: values must be strings, numbers or booleans (no nested objects or arrays, no `null`)
- keys match `[a-z][a-z0-9_]{0,63}`
- 4 KB maximum

**Reserved key — `hidden_from_students`** (boolean, default `false`): the document's content stays fully retrievable and is used to answer questions, but its **source is never returned**. Chunks marked this way are absent from the `sources` array of `POST /api/v1/query` and `POST /api/v1/learn/query`, and from the `sources` SSE event when streaming; with citations enabled they are handed to the model marked as non-citable so no `[n]` marker can point at them. This is for material that may be consulted but not attributed or redistributed — licensed textbook chapters, for instance.

It must be a JSON boolean. The string `"true"` is rejected rather than coerced: `"false"` is truthy in Python, and a parser that guessed would hide every source in the namespace on a quoting mistake.

**Changing the flag on an already ingested document does not work by re-ingesting it.** Deduplication is by content hash: an unchanged file returns `status: "exists"` (or `"alias"` under a new filename) without touching the stored chunks or their metadata. To flip visibility, `DELETE /api/v1/documents/{document_id}` first, then ingest again. If the file's **content** also changed, no delete is needed: that path creates a new version and rewrites the chunks with the metadata of the new request.

Sync response (HTTP 200, files <= 10 MB):

```json
{
    "document_id": "550e8400-...",
    "chunk_count": 24,
    "status": "new"
}
```

Sync status values: `new` (extracted, embedded and stored), `exists` (exact duplicate: same content hash and same filename, no work done), `alias` (same content under a different filename: an alias is added to the existing document).

Async response (HTTP 202, files > 10 MB):

```json
{
    "job_id": "e5f6a7b8-...",
    "status": "processing"
}
```

Error responses: 409 (duplicate filename with different content), 413 (file too large), 422 (unsupported format, scanned PDF, or invalid `metadata`).

### GET /api/v1/ingest/jobs/{job_id}/status

Poll async ingest job status. Requires `ingest` or `admin` scope.

**Scopes**: `ingest`, `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/api/v1/ingest/jobs/e5f6a7b8-.../status | python3 -m json.tool
```

```json
{
    "id": "e5f6a7b8-...",
    "status": "indexed",
    "phase": "complete",
    "document_id": "550e8400-...",
    "chunk_count": 24,
    "error_code": null,
    "error_message": null,
    "percentage": 100
}
```

Status values: `processing`, `indexed`, `failed`.

### POST /api/v1/ingest/batch

Ingest multiple documents in one multipart request. Every file is processed asynchronously, so the response is always HTTP 202 — there is no synchronous fast path here, unlike `POST /api/v1/ingest`.

**Scopes**: `ingest`, `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -F "files=@lecture-07.pdf;type=application/pdf" \
  -F "files=@lecture-08.pdf;type=application/pdf" \
  "http://localhost:8000/api/v1/ingest/batch?namespace=default" | python3 -m json.tool
```

Query parameters: `namespace` (default `default`). Files must be sent as repeated `files` fields.

An optional `metadata` form field applies to every file in the request, with the same shape and rules as `POST /api/v1/ingest`. Invalid metadata rejects the whole batch with `ERR-INGEST-005` (422): ingesting half the files without the visibility their caller asked for is worse than ingesting none.

Response (HTTP 202): one entry per submitted file, in submission order.

```json
[
    {"job_id": "e5f6a7b8-...", "filename": "lecture-07.pdf", "status": "pending", "error": null},
    {"job_id": null, "filename": "huge.pdf", "status": "rejected", "error": "File size 73400320 bytes exceeds maximum 52428800 bytes."}
]
```

Per-file status: `pending` (job created, poll it via `GET /api/v1/ingest/jobs/{job_id}/status`), `rejected` (over `VEKTRA_MAX_FILE_SIZE_MB`, no job created), `failed` (job creation failed). A rejected or failed file does not fail the request: the batch still returns 202, so callers must inspect each entry rather than trust the status code. An empty file list returns 422.

### DELETE /api/v1/documents/batch

Delete several documents in one call. Chunks are hard-deleted from the active vector store; the document records are soft-deleted with `deletion_reason='user_request'`.

**Scopes**: `admin`

```bash
curl -s -X DELETE \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"document_ids":["550e8400-...","6f1c2d3e-..."],"namespace":"default"}' \
  http://localhost:8000/api/v1/documents/batch | python3 -m json.tool
```

Request body:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `document_ids` | UUID[] | (required) | Documents to delete |
| `namespace` | string | `default` | Namespace the documents belong to |

Response (HTTP 200):

```json
{
    "deleted": ["550e8400-..."],
    "not_found": ["6f1c2d3e-..."]
}
```

Unknown ids are reported in `not_found` rather than failing the request.

### Granular pipeline endpoints

`POST /api/v1/ingest/extract`, `POST /api/v1/ingest/chunk` and `POST /api/v1/ingest/embed` run the ingestion pipeline **without storing anything**. They exist to inspect and tune each stage in isolation: what the extractor produced, how the chunker split it, what got embedded. Nothing reaches the vector store, so they are safe to run against production data.

**Scopes**: `ingest`, `admin` (all three)

Each takes a single `file` field (multipart), is synchronous, and rejects files above 10 MB with HTTP 413. Unsupported content types return 422.

| Endpoint | Runs | Returns (JSON array) |
|----------|------|----------------------|
| `POST /api/v1/ingest/extract` | extraction | `{text, element_type, content_format, metadata}` per element |
| `POST /api/v1/ingest/chunk` | extraction + chunking | the same shape, after the chunking strategy |
| `POST /api/v1/ingest/embed` | extraction + chunking + embedding | `{text, dense, element_type, content_format, metadata}` per chunk, `dense` being the embedding vector |

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -F "file=@lecture-07.pdf;type=application/pdf" \
  http://localhost:8000/api/v1/ingest/chunk | python3 -m json.tool
```

## Query

### POST /api/v1/query

RAG query with LLM-generated answer. Requires `query` or `admin` scope.

**Scopes**: `query`, `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What is RAG?",
    "namespace": "default",
    "top_k": 5
  }' \
  http://localhost:8000/api/v1/query | python3 -m json.tool
```

Request body:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `question` | string | (required) | Query text (max 10,000 characters) |
| `namespace` | string | `default` | Namespace to search |
| `top_k` | int | `5` | Number of chunks to retrieve (1-100) |
| `stream` | bool | `false` | Enable Server-Sent Events streaming |
| `conversation_id` | UUID | - | Continue an existing conversation |

Response:

```json
{
    "response_id": "9f8e7d6c-...",
    "answer": "RAG is ...",
    "sources": [
        {
            "doc_id": "550e8400-...",
            "chunk_id": "c1a2b3...",
            "score": 0.912,
            "snippet": "...",
            "citation_id": "d4e5f6-...",
            "document_version": 1,
            "document_name": "lecture-07.pdf"
        }
    ],
    "conversation_id": null,
    "context_only": false,
    "no_relevant_context": false
}
```

| Field | Description |
|-------|-------------|
| `response_id` | Unique response identifier |
| `answer` | LLM-generated answer grounded in sources |
| `sources` | Ranked list of source chunks. Chunks ingested with `hidden_from_students: true` are **omitted**: their content is still retrieved and used to build the answer, but nothing identifying them (chunk id, snippet, filename) is returned. The filter applies to every caller, including admin-scope keys; use the QueryTrace or `GET /api/v1/documents/{id}/chunks` to inspect what was retrieved. |
| `sources[].document_name` | Filename of the source document (e.g. `lecture-07.pdf`), or `null` when the document join returns no row. Soft-deleted documents (REQ-057) keep their citation with an `(archived)` suffix so traceability is preserved. |
| `sources[].title` | FEAT-021: human-readable citation label ("filename, p.N") matching the `[n]` markers in the answer. Set only when the namespace has `citations_enabled`; `null` otherwise. |
| `conversation_id` | Echoed back if provided in request |
| `context_only` | `true` if LLM failed and raw sources returned |
| `no_relevant_context` | `true` if no chunks exceeded relevance threshold |

#### Streaming

Set `stream: true` or send `Accept: text/event-stream`:

```bash
curl -s -N \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"question":"What is RAG?","namespace":"default","stream":true}' \
  http://localhost:8000/api/v1/query
```

### GET /api/v1/providers

List registered LLM providers with health status. Requires `query` or `admin` scope.

**Scopes**: `query`, `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/api/v1/providers | python3 -m json.tool
```

## Conversations

Conversation **content** is never served by these endpoints (REQ-051): they return metadata only. The decrypted turns are available to students through `GET /api/v1/learn/conversations/{id}/turns` (JWT, own course only) and to operators through `GET /api/v1/admin/conversations/{id}/turns` (admin).

Both endpoints require persistent conversation storage. With the in-memory fallback store they return `503`.

### GET /api/v1/conversations/{conversation_id}

Conversation metadata.

**Scopes**: `query`, `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/api/v1/conversations/550e8400-... | python3 -m json.tool
```

Response (HTTP 200):

```json
{
    "id": "550e8400-...",
    "namespace_id": "default",
    "created_at": "2026-07-14T10:00:00Z",
    "updated_at": "2026-07-14T10:05:00Z",
    "turn_count": 3,
    "title": null
}
```

Errors: `404` if the conversation does not exist **or has been soft-deleted**; `503` if conversations are not persisted.

### DELETE /api/v1/conversations/{conversation_id}

Soft-delete a conversation and all its turns (REQ-057). Returns HTTP 204 (no body).

**Scopes**: `query`, `admin`

```bash
curl -s -X DELETE \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/api/v1/conversations/550e8400-...
```

Errors: `404` if the conversation does not exist or was already deleted.

A namespace-bound key can only read and delete conversations in its own namespace.

## Feedback

Feedback on an answer (REQ-055). Two levels: the whole response, or one citation within it. Both record the submitting key and namespace.

The `namespace` field in the body is ignored for namespace-bound keys, which always write to their own namespace.

### POST /api/v1/feedback/{response_id}

Rate a response. `response_id` is the value returned by `POST /api/v1/query`.

**Scopes**: `query`, `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"rating":5,"comment":"Accurate and well sourced"}' \
  http://localhost:8000/api/v1/feedback/9f8e7d6c-... | python3 -m json.tool
```

Request body:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `rating` | int | (required) | 1 to 5. Outside that range: 422. |
| `comment` | string | `null` | Free-text comment |
| `namespace` | string | `default` | Namespace to attribute the feedback to |

Response (HTTP 201):

```json
{"id": "b7c8d9e0-..."}
```

### POST /api/v1/feedback/citation/{citation_id}

Rate a single citation. `citation_id` is `sources[].citation_id` from the query response; `response_id` is required in the **body** here (the path carries the citation, not the response).

**Scopes**: `query`, `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"response_id":"9f8e7d6c-...","rating":2,"comment":"Wrong page"}' \
  http://localhost:8000/api/v1/feedback/citation/d4e5f6-... | python3 -m json.tool
```

Request body:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `response_id` | UUID | (required) | The response the citation belongs to |
| `rating` | int | (required) | 1 to 5 |
| `comment` | string | `null` | Free-text comment |
| `namespace` | string | `default` | Namespace to attribute the feedback to |

Response (HTTP 201): `{"id": "<feedback-id>"}`.

## Learn

The e-learning vertical (`vektra-learn`) exposes course-scoped endpoints authenticated via short-lived JWT dashboard tokens issued by `POST /api/v1/learn/tokens`. The `course_id` claim drives namespace isolation; the LMS-side integration (e.g. the Moodle plugin) is responsible for upstream auth before requesting a token.

Two trust tiers:
- **API key** (called server-side by the LMS): `/tokens`, `/enrollments`, `/content/ingest`. Each endpoint declares the minimum required scope below; `admin` is a project-wide superscope and always satisfies any required scope.
- **JWT** (called from the browser by the widget, with the token issued by `/tokens`): `/query`, `/conversations/{id}/turns`. Namespace is derived from the JWT claims.

### POST /api/v1/learn/tokens

Issue a short-lived JWT dashboard token bound to a `(student_id, course_id)` pair. Called server-side by the LMS once it has authenticated the student.

**Scopes**: `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"student_id":"u123","course_id":"CS101","expires_in":3600}' \
  http://localhost:8000/api/v1/learn/tokens | python3 -m json.tool
```

Request body:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `student_id` | string | (required) | Stable student identifier (1–255 chars). |
| `course_id` | string | (required) | Stable course identifier (1–255 chars). |
| `namespace` | string | (course_id) | Namespace embedded in the JWT. Defaults to the course id when omitted. |
| `expires_in` | int | `3600` | Token lifetime in seconds (max `86400`, i.e. 24h). |

Response (HTTP 201):

```json
{
    "token": "eyJhbGciOi...",
    "expires_at": "2026-04-25T15:00:00Z"
}
```

The token is an HS256 JWT bound to its audience: header `kid: "learn-1"`, claims `aud:
"vektra-learn-widget"`, `iss: "vektra"`, `sub`, `course_id`, `iat`, `exp` (and `namespace` when given).
The learn endpoints accept only tokens with exactly that audience and issuer; a token minted by a
version before 0.8 (no `aud`/`iss`/`kid`) is answered `401`, and the widget re-mints through
`data-token-refresh-url`.

### POST /api/v1/learn/enrollments

Register a student in a course. Optional when `VEKTRA_LEARN_REQUIRE_ENROLLMENT=false` (the LMS is the source of truth for enrollment).

**Scopes**: `ingest`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"student_id":"u123","course_id":"CS101","namespace":"CS101"}' \
  http://localhost:8000/api/v1/learn/enrollments | python3 -m json.tool
```

Request body:

| Field | Type | Description |
|-------|------|-------------|
| `student_id` | string | Stable student identifier (1–255 chars). |
| `course_id` | string | Stable course identifier (1–255 chars). |
| `namespace` | string | Target namespace for course content (1–64 chars). Auto-created if absent. |
| `metadata` | object | Optional free-form metadata. |

Response (HTTP 201): full enrollment record (`id`, `student_id`, `course_id`, `namespace`, `enrolled_at`, `metadata`).

Errors: `409 ERR-LEARN-004` if the same `(student_id, course_id)` is already enrolled.

### GET /api/v1/learn/enrollments

List enrollments. Filter by `course_id`, `student_id`, or both via query parameters.

**Scopes**: `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/learn/enrollments?course_id=CS101&limit=50" | python3 -m json.tool
```

Query parameters: `course_id`, `student_id`, `limit` (1–500, default 50), `offset` (default 0).

Response: `{"items": [...], "count": <int>}`.

### DELETE /api/v1/learn/enrollments/{enrollment_id}

Remove a single enrollment.

**Scopes**: `admin`

Returns HTTP 204 (no body). `404` if the enrollment id does not exist.

### POST /api/v1/learn/content/ingest

Trigger course-scoped ingestion. Used by automation (e.g. an n8n workflow watching a course folder) to push new material into the course namespace without going through the generic `/api/v1/ingest` endpoint.

**Scopes**: `ingest`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"course_id":"CS101","namespace":"CS101","document_url":"https://lms.example/files/lecture-07.pdf"}' \
  http://localhost:8000/api/v1/learn/content/ingest | python3 -m json.tool
```

Request body: `course_id`, `namespace`, optional `document_url`, optional `metadata`. Response echoes the ingestion `status`, `namespace`, `course_id`, `metadata`, and (when synchronous) `document_id` + `chunk_count`.

### POST /api/v1/learn/query

Course-scoped RAG query. Authenticated via JWT (not API key). The namespace is derived from the token's `namespace` (or `course_id` fallback) claim.

```bash
curl -s \
  -H "Authorization: Bearer $LEARN_JWT" \
  -H "Content-Type: application/json" \
  -d '{"question":"What did we cover in lecture 7?"}' \
  http://localhost:8000/api/v1/learn/query | python3 -m json.tool
```

Request body:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `question` | string | (required) | Query text |
| `conversation_id` | UUID | - | Continue an existing conversation |
| `top_k` | int | `5` | Number of chunks to retrieve (1-100) |
| `stream` | bool | `false` | Enable Server-Sent Events streaming |

A `conversation_id` that already exists must belong to the token's `sub`: otherwise the request is refused with `403 ERR-LEARN-007` before any history is loaded, and a conversation with no recorded owner is refused the same way. If ownership cannot be checked because the conversation store is unreachable, the request is refused with `503 ERR-LEARN-008` (transient, retry) rather than let through. This holds for the in-memory store too, where it applies to conversations seen since the last restart.

Response (HTTP 200, JSON):

```json
{
    "response_id": "9f8e7d6c-...",
    "answer": "Lecture 7 covered ...",
    "sources": [
        {
            "doc_id": "550e8400-...",
            "chunk_id": "c1a2b3...",
            "score": 0.912,
            "snippet": "...",
            "document_name": "lecture-07.pdf"
        }
    ],
    "conversation_id": "550e8400-...",
    "no_relevant_context": false,
    "show_sources": true
}
```

| Field | Description |
|-------|-------------|
| `show_sources` | Server-resolved citation-visibility hint for the widget (FEAT-014). The full `sources` list is always returned regardless; the widget uses the flag to decide whether to render the citations block. See the resolution chain in [Namespaces PATCH](#patch-apiv1adminnamespacesnamespace_idconfig). |
| `sources[].document_name` | Filename of the source document. Soft-deleted documents (REQ-057) keep an `(archived)` suffix so traceability is preserved. The field is `null` when the document join returns no row. |
| `sources[].title` | FEAT-021: citation label ("filename, p.N") for the `[n]` markers, `null` unless the namespace has `citations_enabled`. |

#### Streaming

Set `stream: true` or send `Accept: text/event-stream`:

```bash
curl -s -N \
  -H "Authorization: Bearer $LEARN_JWT" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"question":"What did we cover in lecture 7?","stream":true}' \
  http://localhost:8000/api/v1/learn/query
```

The `sources` SSE event payload carries the same `show_sources` flag alongside the sources list.

### GET /api/v1/learn/conversations/{conversation_id}/turns

Return the decrypted turns of a conversation belonging to the token's course. Used by the widget to restore history after a page reload (FEAT-004).

**Auth**: JWT dashboard token (same as `/learn/query`). Two checks, both required: the conversation must belong to the token's namespace (403 `ERR-LEARN-006`, so the widget can distinguish "wrong course" from "deleted conversation" and reset its local state), **and** it must belong to the token's `sub` (403 `ERR-LEARN-007`). Namespace alone is not authorization: every student of a course holds a token for the same namespace, so before FEAT-027 any of them could read any conversation of that course given its id. A conversation with no recorded owner — created before ownership existed, or by an API-key caller that has no subject — is refused rather than shared.

```bash
curl -s \
  -H "Authorization: Bearer $LEARN_JWT" \
  http://localhost:8000/api/v1/learn/conversations/550e8400-.../turns | python3 -m json.tool
```

Response (HTTP 200):

```json
{
    "conversation_id": "550e8400-...",
    "namespace": "course-101",
    "turns": [
        {
            "turn_number": 1,
            "question": "What is RAG?",
            "answer": "RAG is ...",
            "created_at": "2026-04-25T14:00:00Z",
            "sources": []
        }
    ]
}
```

The `turns[].sources` array is empty in v0.5.0 (admin-only metadata such as model, prompt tokens, and source enrichment from `query_traces` is intentionally not exposed to students). The `turns[].answer` field may be `null` for a turn that is still in-flight (the question is recorded immediately, the answer is filled in when the LLM completes).

Errors:
- `404 ERR-LEARN-005` if the conversation does not exist or has been deleted.
- `403 ERR-LEARN-006` if the conversation belongs to a different course/namespace.
- `403 ERR-LEARN-007` if the conversation belongs to another student, or has no recorded owner.
- `401 ERR-LEARN-003` if the token carries no `sub` claim, which means it can own no conversation.
- `503 ERR-LEARN-001` if the conversation store is not yet initialized or does not support decryption (e.g. in-memory store; production deployments must set `VEKTRA_CONVERSATION_KEY`).

### GET /api/v1/learn/conversations

List the caller's own conversations in the token's course, most recently used first. Metadata only, never content.

**Auth**: JWT dashboard token. Scoped to `jwt.namespace` **and** `jwt.sub`, so the list is one student's own history and nobody else's.

```bash
curl -s \
  -H "Authorization: Bearer $LEARN_JWT" \
  "http://localhost:8000/api/v1/learn/conversations?limit=5" | python3 -m json.tool
```

```json
{
    "namespace": "course-101",
    "conversations": [
        {
            "conversation_id": "550e8400-...",
            "title": null,
            "turn_count": 6,
            "created_at": "2026-09-01T09:12:00Z",
            "updated_at": "2026-09-03T18:40:00Z"
        }
    ]
}
```

Query parameters: `limit` (default 20, 1-100). Soft-deleted conversations are never listed.

This is what lets the widget restore history on a device that has never seen the conversation id: ownership lives on the server, not in the browser. Requires a persistent conversation store — with the in-memory store (no `VEKTRA_CONVERSATION_KEY`) the endpoint returns `500 ERR-LEARN-001` rather than an empty list, because "no history" and "history is not being stored" are different answers.

### DELETE /api/v1/learn/conversations/{conversation_id}

Let a student erase one of their own conversations (REQ-057). Soft delete: the row keeps its retention trail, its turns stop being readable through the API, and it is no longer offered for resumption.

**Auth**: JWT dashboard token, scoped to the caller's own `sub`.

Returns `204` on success and `404 ERR-LEARN-005` when the conversation does not exist, is already deleted, **or belongs to someone else** — deliberately the same answer, so the endpoint cannot be used to discover which conversation ids exist in a course.

**Audit (NFR-007)**: writes a `learn_conversation_deleted` row carrying `namespace`, `conversation_id`, `student_id` and `course_id`.

**Audit (NFR-007)**: every successful read writes a `learn_conversation_turns_read` audit row carrying `namespace`, `conversation_id`, `turn_count`, `student_id`, and `course_id`. The audit row is generated even if the upstream middleware did not set a request id.

## Index

### POST /api/v1/search

Semantic search over indexed chunks (vector store only, no LLM). Requires `query` scope.

**Scopes**: `query`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "machine learning",
    "namespace": "default",
    "top_k": 10
  }' \
  http://localhost:8000/api/v1/search | python3 -m json.tool
```

Request body:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `query` | string | (required) | Search text |
| `namespace` | string | `default` | Namespace to search |
| `top_k` | int | `5` | Number of results (1-100) |
| `search_mode` | string | `dense` | Search mode |
| `filters` | object | - | JSONB metadata filters |

Response:

```json
{
    "results": [
        {
            "chunk_id": "...",
            "document_id": "550e8400-...",
            "score": 0.912,
            "text_snippet": "...",
            "document_version": 1,
            "metadata": {}
        }
    ],
    "total": 10
}
```

### POST /api/v1/documents/{document_id}/chunks

Store embeddings for document chunks (used by ingestion pipeline). Requires `ingest` scope.

**Scopes**: `ingest`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "chunks": [
      {
        "chunk_id": "c1",
        "text": "Example chunk text",
        "dense": [0.1, 0.2, 0.3],
        "metadata": {}
      }
    ],
    "namespace": "default"
  }' \
  http://localhost:8000/api/v1/documents/550e8400-.../chunks | python3 -m json.tool
```

### GET /api/v1/documents/{document_id}/chunks

List a document's stored chunks at the active index version, ordered by position. Reads from the
active vector store, which is the only source of truth for chunk text. Includes parent chunks
(FEAT-017), which search excludes. Accepts any valid scope.

`namespace` is optional: a namespace-bound key resolves to its own namespace when it is omitted,
and naming a different one returns 403.

**Scopes**: any

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/documents/550e8400-.../chunks?namespace=default" | python3 -m json.tool
```

```json
{
    "document_id": "550e8400-...",
    "namespace": "default",
    "chunks": [
        {
            "chunk_id": "9f1c...",
            "text": "Example chunk text",
            "position": 0,
            "parent_id": null,
            "metadata": {"chunk_level": "parent"}
        }
    ],
    "total": 1
}
```

### DELETE /api/v1/documents/{document_id}

Remove a document's chunks from the active vector store, then soft-delete the document record.
`chunks_removed` is the number of chunks actually removed. Requires `admin` scope.

**Scopes**: `admin`

```bash
curl -s -X DELETE \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/documents/550e8400-...?namespace=default" | python3 -m json.tool
```

```json
{
    "document_id": "550e8400-...",
    "chunks_removed": 24
}
```

### GET /api/v1/stats

Document and chunk counts, at the **active index version**. Accepts any valid scope.

**Scopes**: any

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/stats?namespace=default" | python3 -m json.tool
```

```json
{
    "document_count": 3,
    "chunk_count": 72,
    "namespace": "default"
}
```

Omit `namespace` to get totals across all namespaces.

## Reindex

Re-embed a namespace under a **new index version** without downtime (REQ-064, ARCH-045, [ADR-0026](../../.s2s/decisions/ADR-0026-document-chunks-pgvector-internal.md)). Use it after changing the embedding model, the chunking strategy, or any setting that invalidates existing vectors.

The mechanism: chunks are versioned. Every read (search, stats, chunk listing) filters on the **active** index version, set by `VEKTRA_ACTIVE_INDEX_VERSION` (default `1`). A reindex writes a second copy of the chunks under the target version, **alongside** the live ones, which keep serving traffic. The new version becomes live only when the operator changes the env var and restarts. Nothing is deleted along the way, so a bad reindex is rolled back by simply not switching.

The switch is manual: there is **no** API to activate a version, it is an env var plus a restart. The cleanup afterwards is an API call, `DELETE /api/v1/index-versions/{version}` (see [Step 5](#step-5-clean-up-the-old-version)), which refuses to delete whichever version is currently being served.

### POST /api/v1/reindex

Start a reindex job. Returns immediately (HTTP 202); the work runs in the background.

**Scopes**: `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"namespace":"default","target_index_version":2}' \
  http://localhost:8000/api/v1/reindex | python3 -m json.tool
```

Request body:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `namespace` | string | `default` | Namespace to reindex. Ignored for a namespace-bound key, which always reindexes its own. |
| `target_index_version` | int | (required) | Version to write. Must be `>= 1` and **differ** from the active version. |

Response (HTTP 202):

```json
{
    "job_id": "0500a873-0c7a-4e42-a8d2-0bd7d77591be",
    "status": "pending",
    "namespace": "default",
    "target_index_version": 2
}
```

The source version is not a parameter: the job always reads the active version, since that is the only version the store exposes for reading.

Errors: `400` `ERR-INDEX-003` if `target_index_version` equals the active version (reindexing a version onto itself would overwrite the live chunks instead of writing beside them).

### GET /api/v1/reindex/{job_id}/status

Poll a reindex job.

Errors: `404` `ERR-INDEX-004` if no job with that id is visible to the key.

**Scopes**: `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/api/v1/reindex/0500a873-.../status | python3 -m json.tool
```

Response (HTTP 200):

```json
{
    "job_id": "0500a873-0c7a-4e42-a8d2-0bd7d77591be",
    "status": "completed",
    "namespace": "default",
    "source_index_version": 1,
    "target_index_version": 2,
    "total_documents": 3,
    "processed_documents": 3,
    "chunks_reindexed": 12,
    "error_message": null,
    "created_at": "2026-07-14T16:13:21.085723+00:00",
    "completed_at": "2026-07-14T16:13:21.248801+00:00"
}
```

| Field | Description |
|-------|-------------|
| `status` | `pending`, `running`, `completed`, `failed` |
| `source_index_version` | Version read from (the active one at trigger time) |
| `total_documents` | Documents in the namespace, excluding soft-deleted ones |
| `processed_documents` | Progress counter |
| **`chunks_reindexed`** | **Chunks actually written under the target version. This is the number that tells a real reindex from an empty one — check it, not just `status`.** |
| `error_message` | Failure reason, `null` on success |

Errors: `404` if the job id is unknown, or belongs to a different namespace than a namespace-bound key.

**Why `chunks_reindexed` matters.** Until BUG-023 the job read chunks from a Postgres table that is empty in Qdrant mode, re-embedded nothing, wrote nothing, and still reported `completed`. An operator polling `status` alone would have seen a green result and switched the active version to an **empty index**. The job now refuses to report success when it wrote nothing: a namespace with documents but zero reindexed chunks fails with `error_message` explaining that the store returned no chunks at the source version. `chunks_reindexed` makes the distinction visible rather than implied — treat a completed job whose `chunks_reindexed` is 0 (against a non-empty namespace) as a bug, not a no-op.

### DELETE /api/v1/index-versions/{index_version}

Reclaim the storage held by a superseded index version (REQ-064). Reindex leaves both versions in the store; this is the only thing that removes one.

**Scopes**: `admin`

```bash
curl -s -X DELETE \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/index-versions/1?namespace=default" | python3 -m json.tool
```

| Parameter | In | Default | Description |
|-----------|-----|---------|-------------|
| `index_version` | path | (required) | The version to delete. Must be `>= 1`, and must **not** be the active one. |
| `namespace` | query | `default` | Namespace to clean up. A namespace-bound key may only name its own; naming another returns `403`. |

```json
{
  "namespace": "default",
  "index_version": 1,
  "chunks_removed": 12
}
```

Errors (all in the REQ-010 envelope):

- **`409` `ERR-INDEX-001`** if `index_version` is the version the system is currently serving. **This is the guard that matters.** The store refuses before deleting anything, so the request is a no-op, not a partial wipe. The failure mode it exists to prevent is not "an old version survives", it is "the live index is emptied": get the version number wrong by one and every query stops finding anything. `details` carries the `namespace` and `index_version` that were refused.
- `400` `ERR-INDEX-002` if `index_version` is below 1.
- `403` `ERR-AUTH-003` for a namespace-bound key naming another namespace. The delete is by filter, so a silently retargeted namespace would drop an entire version of a namespace the caller never named.

The call is **idempotent**: deleting a version that is already gone returns `200` with `chunks_removed: 0`. That is the cheapest way to confirm a cleanup really happened, and it is safe to repeat.

It is also the one **irreversible** step in the whole flow. Everything before it can be undone by not switching, or by switching back. Once the old version is deleted, rolling back means reindexing again from scratch.

### End-to-end operator flow

`scripts/reindex.sh TARGET_VERSION [NAMESPACE]` automates steps 1 and 2; `scripts/reindex.sh --cleanup OLD_VERSION [NAMESPACE]` does step 5. They are two separate runs on purpose: between them sits the switch, which only a human can do, and until it happens the API will (correctly) refuse the cleanup.

#### Step 1: trigger

Note the chunk count you expect to move, so you can check it afterwards:

```bash
curl -s -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/stats?namespace=default"
# {"document_count":3,"chunk_count":12,"namespace":"default"}

JOB_ID=$(curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"namespace":"default","target_index_version":2}' \
  http://localhost:8000/api/v1/reindex | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
```

#### Step 2: poll until it settles

```bash
curl -s -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/reindex/$JOB_ID/status" | python3 -m json.tool
```

Poll until `status` is `completed` or `failed`. Live traffic is still being served by the old version throughout.

#### Step 3: verify `chunks_reindexed` before switching

This is the gate. **Do not switch on `status: completed` alone.**

```bash
curl -s -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/reindex/$JOB_ID/status" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['status'], d['chunks_reindexed'], 'chunks')"
# completed 12 chunks
```

`chunks_reindexed` should be consistent with the `chunk_count` you noted in step 1. If it is `0`, or far below what you expected, the new version is empty or partial: **do not switch**, investigate. The old version is untouched, so there is nothing to roll back.

#### Step 4: switch the active version

Set the env var and recreate the container (a restart does not reload `.env`):

```bash
# .env
VEKTRA_ACTIVE_INDEX_VERSION=2
```

```bash
docker compose up -d vektra
```

Every read now resolves to version 2. Confirm with a query and with `GET /api/v1/stats`, whose counts are version-scoped and should now reflect the new index.

To roll back, set the variable back to `1` and recreate again: version 1 is still in the store.

#### Step 5: clean up the old version

Until this step, the old version is still in the store: invisible to reads, but occupying exactly as much space as the live one. Every reindex you never clean up doubles the namespace again.

Do it **after** step 4, once the new version has served real traffic and you are no longer going to roll back:

```bash
curl -s -X DELETE \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/index-versions/1?namespace=default"
# {"namespace":"default","index_version":1,"chunks_removed":12}
```

Or: `scripts/reindex.sh --cleanup 1 default`.

Ordering is not left to your memory. If you run this **before** the switch, version 1 is still the active one and the API refuses with `409` — the request deletes nothing:

```json
{
  "error": {
    "category": "PERMANENT",
    "code": "ERR-INDEX-001",
    "message": "Refusing to delete index version 1 of namespace 'default': it is the version currently being served. Switch VEKTRA_ACTIVE_INDEX_VERSION to the new version and restart before cleaning up the old one.",
    "remediation": "Switch VEKTRA_ACTIVE_INDEX_VERSION to the new version and restart, then delete the old one. To check which version is live, see VEKTRA_ACTIVE_INDEX_VERSION in the running configuration.",
    "request_id": "550e8400-e29b-41d4-a716-446655440000",
    "details": {"namespace": "default", "index_version": 1}
  }
}
```

That refusal is the whole reason this is an endpoint rather than a store-level delete. Reclaiming space is the small half of the job; the large half is that the obvious hand-written version of it — a Qdrant filter delete, or `DELETE FROM document_chunks WHERE index_version = 1` — has no idea which version is live, and is run by an operator at precisely the moment they are least sure. One wrong number and the live index is empty, with no error and nothing to roll back to.

To confirm it is really gone, run it again: a second call returns `chunks_removed: 0`.

This is the one irreversible step in the flow. Everything before it is undone by not switching, or by switching back.

## Analytics

Operational data, not user-facing (ARCH-041, [ADR-0017](../../.s2s/decisions/ADR-0017-audit-analytics-separation.md)). Every endpoint here requires `admin`.

A `QueryTrace` records what the RAG pipeline did for one response: each step with its duration, the chunks it retrieved with their scores, the model and prompt version. It is written by the query pipeline and is the tool for answering "why was this answer bad?".

### GET /api/v1/traces

List traces, most recent first.

**Scopes**: `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/traces?namespace=default&min_duration_ms=3000&limit=20" \
  | python3 -m json.tool
```

Query parameters:

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `namespace` | string | all | Filter by namespace |
| `from` | datetime | - | Start of the time window (ISO 8601) |
| `to` | datetime | - | End of the time window (ISO 8601) |
| `model` | string | all | Filter by LLM model |
| `min_duration_ms` | int | - | Only traces slower than this. Useful for hunting latency outliers. |
| `limit` | int | `50` | Page size (1 to 500) |
| `offset` | int | `0` | Pagination offset |

Note the parameter names: `from`, `to` and `model` (not `from_dt`, `to_dt`, `llm_model` — those are the internal Python names).

Response (HTTP 200):

```json
{
    "items": [
        {
            "response_id": "c1d7ffce-...",
            "steps": [
                {"name": "pre_query_safeguard", "duration_ms": 0, "metadata": {"allowed": true}},
                {"name": "embed_query", "duration_ms": 11, "metadata": {}},
                {"name": "llm_call", "duration_ms": 3421, "metadata": {}}
            ],
            "total_duration_ms": 3652,
            "chunks_retrieved": [{"chunk_id": "9f1c...", "score": 0.912}],
            "llm_model": "openai/qwen35-27b-fp8",
            "prompt_version": "v1",
            "created_at": "2026-07-12T09:17:40.010407Z"
        }
    ],
    "count": 1
}
```

`count` is the number of items **on the current page**, not the total number of matching traces. Paginate with `offset` until a page comes back short; do not treat `count` as a total.

### GET /api/v1/traces/{response_id}

A single trace, by the `response_id` returned from `POST /api/v1/query`. Same object as an entry in `items` above.

**Scopes**: `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/api/v1/traces/c1d7ffce-... | python3 -m json.tool
```

Errors: `404 ERR-ANALYTICS-002` if no trace exists for that response id (traces are stored only when trace storage is enabled). `503 ERR-ANALYTICS-001` if the analytics service is still starting.

### GET /api/v1/metrics

Aggregated metrics over a time window. Not to be confused with `GET /metrics`, which is the unauthenticated Prometheus endpoint at the root.

**Scopes**: `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  "http://localhost:8000/api/v1/metrics?namespace=default" | python3 -m json.tool
```

Query parameters: `namespace`, `from`, `to` (all optional; default is all namespaces over all time).

Response (HTTP 200):

```json
{
    "total_queries": 67,
    "avg_latency_ms": 3652.23,
    "p95_latency_ms": 5985.2,
    "avg_retrieval_score": 0.6675,
    "queries_per_hour": 0.03,
    "model_distribution": {"openai/qwen35-27b-fp8": 7, "qwen36-35b-a3b-fp8": 38},
    "period_start": "2026-03-28T10:38:28.227883Z",
    "period_end": "2026-07-12T09:17:40.010407Z"
}
```

| Field | Description |
|-------|-------------|
| `avg_retrieval_score` | Mean similarity score of retrieved chunks. A drop here is a retrieval-quality regression, and it moves before answer quality visibly does. |
| `queries_per_hour` | Throughput over the window |
| `model_distribution` | Query count per model. Entries can look duplicated (`qwen35-27b-fp8` and `openai/qwen35-27b-fp8`) because the model string is recorded as configured; a provider prefix change produces a new bucket. |
| `period_start` / `period_end` | Actual window covered by the data, not the requested one |

## Admin

### GET /api/v1/admin/conversations/{conversation_id}/turns

Decrypted conversation turns with full metadata (DEBT-011). Conversation content is encrypted at rest (pgcrypto, [ADR-0011](../../.s2s/decisions/ADR-0011-conversation-encryption.md)); this is the operator path to read it. Students read their own history through the JWT-authenticated `GET /api/v1/learn/conversations/{id}/turns`, which returns no metadata.

**Scopes**: `admin`

```bash
curl -s \
  -H "Authorization: Bearer $VEKTRA_API_KEY" \
  http://localhost:8000/api/v1/admin/conversations/550e8400-.../turns | python3 -m json.tool
```

Response (HTTP 200): a JSON array (not an object).

```json
[
    {
        "turn_number": 1,
        "question": "What is RAG?",
        "answer": "RAG is ...",
        "response_id": "9f8e7d6c-...",
        "model": null,
        "prompt_tokens": null,
        "completion_tokens": null,
        "created_at": "2026-07-14T10:00:00Z"
    }
]
```

`answer` is `null` for a turn still in flight (the question is recorded before the LLM responds). `model`, `prompt_tokens` and `completion_tokens` are currently always `null`: the columns exist but nothing populates them yet (DEBT-012).

Errors: `404` if the conversation does not exist. `501` if the conversation store cannot decrypt (in-memory store: set `VEKTRA_CONVERSATION_KEY` for a real deployment). `503` if the store is unavailable.

**Audit (NFR-007)**: every read writes a `conversation_turns_read` audit row with the namespace, conversation id and turn count.

### GET /admin

Redirects (HTTP 308) to `/admin/`, the HTMX dashboard ([ADR-0024](../../.s2s/decisions/ADR-0024-admin-ui-server-side.md)).

The dashboard and its routes (`/admin/keys`, `/admin/namespaces`, `/admin/audit`, `/admin/config`) are a **browser UI, not an API**: they authenticate with a session cookie obtained from `/admin/login`, not with a Bearer token. Do not script against them — use the `/api/v1/*` endpoints above.

## Observability

### GET /metrics

Prometheus metrics (unauthenticated). For aggregated RAG analytics as JSON, see [`GET /api/v1/metrics`](#get-apiv1metrics) instead.

```bash
curl -s http://localhost:8000/metrics
```

## Error responses

Every endpoint returns errors in the same envelope (REQ-010):

```json
{
    "error": {
        "category": "PERMANENT",
        "code": "ERR-AUTH-001",
        "message": "Token is missing, malformed, unrecognized, or revoked.",
        "remediation": "Include a valid API key as Bearer token.",
        "request_id": "550e8400-...",
        "details": {}
    }
}
```

The `error` object is at the JSON document root for every error response, so
`error.code` is read at the top level with no `detail` wrapper. Clients can
branch on `error.code`, which is stable, rather than parsing `message`. See
[error codes reference](error-codes.md) for the complete list.
